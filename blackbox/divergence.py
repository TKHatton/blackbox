"""Replaying recorded model turns, and finding where two runs parted company.

Split from ``timemachine`` because it answers a different question. That module
is about what a replay is allowed to see and touch. This one is about what a
replay decided, and how that differs from what actually happened.

The comparison rule worth stating: **runs are compared on decisions, not on
text.** Two runs that reached the same conclusions in different words have not
diverged. Comparing payloads would report a difference every time a timestamp or
a reason string changed, and bury the decisions that actually differ under noise.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from google.adk.models.base_llm import BaseLlm

from .schema import Event, EventType
from .timemachine import FixtureMiss

logger = logging.getLogger(__name__)


class RecordedLlm(BaseLlm):
    """Replays a recording's model turns instead of calling Gemini.

    This is what fast mode runs on. The agent code, the tools, and the policy
    evaluation are all the real ones; only the model's choices come from the
    recording. That is exactly what isolates a policy change: if the replay
    diverges, it cannot be because the model felt differently today.

    Running out of recorded turns is not padded with a blank response. A replay
    that has gone further than the original run is telling you something, and
    inventing a turn to keep it going would hide that.
    """

    turns: List[Any] = []
    consumed: int = 0

    def __init__(self, turns: List[Any], model: str = "recorded-replay"):
        super().__init__(model=model)
        self.turns = list(turns)
        self.consumed = 0

    @staticmethod
    def supported_models() -> List[str]:
        return ["recorded-replay"]

    async def generate_content_async(self, llm_request: Any, stream: bool = False):
        from google.adk.models.llm_response import LlmResponse

        if not self.turns:
            raise FixtureMiss(
                f"The replay asked for model turn {self.consumed + 1} and the "
                f"recording has none left. The replay has gone further than the "
                f"original run did, which is a result rather than an error, but it "
                f"cannot be continued from a recording."
            )
        self.consumed += 1
        yield LlmResponse(content=self.turns.pop(0))


def build_recorded_turns(events: List[Event]) -> List[Any]:
    """Turn a recorded window into the model turns a replay can serve.

    Each THOUGHT becomes one turn. The TOOL_CALL events it caused become function
    calls on that turn, carrying the arguments the model originally chose, so the
    replayed run reaches the same tools with the same inputs.
    """
    from google.genai import types

    calls_by_parent: Dict[str, List[Event]] = {}
    for event in events:
        if event.event_type == EventType.TOOL_CALL and event.caused_by:
            calls_by_parent.setdefault(event.caused_by, []).append(event)

    turns: List[Any] = []
    for event in events:
        if event.event_type != EventType.THOUGHT:
            continue
        parts = [types.Part(text=event.payload.get("reasoning", ""))]
        for call in calls_by_parent.get(event.event_id, []):
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        name=call.payload.get("tool_name", ""),
                        args=dict(call.payload.get("parameters", {})),
                    )
                )
            )
        turns.append(types.Content(role="model", parts=parts))
    return turns


#: The event types whose differences constitute a change in behaviour. Different
#: wording in a THOUGHT is not a divergence. A different tool call, a different
#: policy decision, or a letter that did or did not go out is.
DECISION_TYPES = (
    EventType.TOOL_CALL,
    EventType.POLICY_CHECK,
    EventType.MESSAGE_SENT,
    EventType.ESCALATE,
    EventType.SUSPEND,
    EventType.RESUME,
)


def decision_signature(event: Event) -> Tuple[str, str]:
    """What an event decided, reduced to something comparable."""
    payload = event.payload
    if event.event_type == EventType.TOOL_CALL:
        return ("TOOL_CALL", str(payload.get("tool_name")))
    if event.event_type == EventType.POLICY_CHECK:
        policy_id = payload.get("policy_id")
        decision = payload.get("decision")
        return ("POLICY_CHECK", f"{policy_id}={decision}")
    if event.event_type == EventType.MESSAGE_SENT:
        return ("MESSAGE_SENT", str(payload.get("purpose")))
    if event.event_type == EventType.ESCALATE:
        return ("ESCALATE", str(payload.get("escalation_type")))
    if event.event_type == EventType.SUSPEND:
        condition = payload.get("wake_condition") or {}
        return ("SUSPEND", str(condition.get("type")))
    if event.event_type == EventType.RESUME:
        return ("RESUME", "resumed")
    return (event.event_type.value, "")


@dataclass
class Divergence:
    """Where two runs parted company, and what followed."""

    diverged: bool
    first_difference_index: Optional[int] = None
    original_decision: Optional[Tuple[str, str]] = None
    replay_decision: Optional[Tuple[str, str]] = None
    explanation: str = ""
    #: Everything after the split. Surfacing only the divergent decision without
    #: its consequences undersells what a replay is for.
    downstream: List[Dict[str, Any]] = field(default_factory=list)
    original_decisions: List[Tuple[str, str]] = field(default_factory=list)
    replay_decisions: List[Tuple[str, str]] = field(default_factory=list)
    #: Rules that reached a different verdict, paired by rule rather than by
    #: position. Index alignment alone reports a structural difference (the
    #: replay not re-emitting a tool call) as though it were the interesting one,
    #: and buries the rule that actually changed its mind.
    rule_changes: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def headline(self) -> str:
        """The difference worth leading with."""
        if self.rule_changes:
            change = self.rule_changes[0]
            return (
                f"{change['rule']} reached a different verdict: "
                f"{change['originally']} became {change['in_replay']}."
            )
        return self.explanation

    def summary(self) -> str:
        if not self.diverged:
            return "The replay made the same decisions as the original run."
        return (
            f"The runs part at decision {self.first_difference_index}: originally "
            f"{self.original_decision}, in the replay {self.replay_decision}. "
            f"{len(self.downstream)} downstream decisions differ."
        )


def policy_verdicts(events: List[Event]) -> Dict[str, str]:
    """The verdict each governance rule reached in a run, keyed by rule."""
    verdicts: Dict[str, str] = {}
    for event in events:
        if event.event_type != EventType.POLICY_CHECK:
            continue
        rule = event.payload.get("policy_id")
        if rule:
            verdicts[str(rule)] = str(event.payload.get("decision"))
    return verdicts


def compare_rule_verdicts(
    original: List[Event], replayed: List[Event]
) -> List[Dict[str, Any]]:
    """Rules that decided differently, paired by rule rather than by position.

    This is what a policy replay is actually asking. Pairing by index would
    report the replay not re-emitting a tool call as the first difference, which
    is true and uninteresting, and would bury the gate that changed its mind.
    """
    was, now = policy_verdicts(original), policy_verdicts(replayed)
    changes = []
    for rule in sorted(set(was) | set(now)):
        before, after = was.get(rule), now.get(rule)
        if before != after:
            changes.append(
                {
                    "rule": rule,
                    "originally": before or "not evaluated",
                    "in_replay": after or "not evaluated",
                }
            )
    return changes


def compare_runs(original: List[Event], replayed: List[Event]) -> Divergence:
    """Find where two runs differ, and everything that followed.

    Reports two things: the rules that reached different verdicts, paired by
    rule, and the first positional difference with its downstream consequences.

    Args:
        original: The recorded events from the rewind point onwards.
        replayed: The events the replay produced.

    Returns:
        Where the runs split, and every decision that differs after it.
    """
    rule_changes = compare_rule_verdicts(original, replayed)
    original_decisions = [
        decision_signature(e) for e in original if e.event_type in DECISION_TYPES
    ]
    replay_decisions = [
        decision_signature(e) for e in replayed if e.event_type in DECISION_TYPES
    ]
    longest = max(len(original_decisions), len(replay_decisions))

    for index in range(longest):
        left = original_decisions[index] if index < len(original_decisions) else None
        right = replay_decisions[index] if index < len(replay_decisions) else None
        if left == right:
            continue

        downstream: List[Dict[str, Any]] = []
        for offset in range(index, longest):
            was = original_decisions[offset] if offset < len(original_decisions) else None
            now = replay_decisions[offset] if offset < len(replay_decisions) else None
            if was != now:
                downstream.append({"at": offset, "originally": was, "in_replay": now})

        return Divergence(
            diverged=True,
            first_difference_index=index,
            original_decision=left,
            replay_decision=right,
            explanation=explain_difference(left, right),
            downstream=downstream,
            original_decisions=original_decisions,
            replay_decisions=replay_decisions,
            rule_changes=rule_changes,
        )

    return Divergence(
        diverged=bool(rule_changes),
        original_decisions=original_decisions,
        replay_decisions=replay_decisions,
        rule_changes=rule_changes,
        explanation=(
            "No decision differed under the amended policy."
            if not rule_changes
            else "The same actions were taken, but a rule reached a different verdict."
        ),
    )


def explain_difference(
    left: Optional[Tuple[str, str]], right: Optional[Tuple[str, str]]
) -> str:
    """Say what a difference means, in the terms a reader cares about."""
    if left is None:
        return f"The replay did something the original run never did: {right}."
    if right is None:
        return f"The replay stopped short. The original run went on to {left}."
    if left[0] == "POLICY_CHECK" and right[0] == "POLICY_CHECK":
        return (
            f"The same rule reached a different decision: originally {left[1]}, "
            f"under the amended policy {right[1]}."
        )
    if left[0] == "TOOL_CALL" and right[0] == "TOOL_CALL":
        return f"The fleet called {right[1]} where it originally called {left[1]}."
    return f"Originally {left}, in the replay {right}."
