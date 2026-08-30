"""The agent-facing face of the Flight Recorder.

``EventStore`` is deliberately dumb: it appends what it is handed. The Recorder is
what agents actually use, and it exists for one reason. The Phase 1 failure mode
is ``caused_by`` being left null on most events, which silently destroys the
causal tree and makes the Time Machine impossible later. Rather than trusting
every call site to pass the right parent, the Recorder keeps a cursor to the
current cause and threads it through automatically.

The cursor moves in one of two ways:

- ``record(...)`` appends under the current cause and does not move the cursor,
  so sibling events share a parent.
- ``under(event_id)`` is a context manager that makes an event the parent of
  everything recorded inside it, then restores the previous cause on exit.

The root event of a case is the only event with a null ``caused_by``, and
``assert_causally_complete`` checks exactly that.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

from .config import get_settings
from .event_store import EventStore
from .fold import CaseState, fold_events
from .opentelemetry_setup import get_tracer
from .schema import Event, EventType

if TYPE_CHECKING:  # pragma: no cover
    from .wake import WakeCondition


class Recorder:
    """Writes one case's events, keeping the causal tree intact."""

    def __init__(
        self,
        case_id: str,
        actor: str,
        store: Optional[EventStore] = None,
        project_id: Optional[str] = None,
    ):
        settings = get_settings()
        self.case_id = case_id
        self.actor = actor
        self.store = store or EventStore(project_id=project_id or settings.project_id)
        self._tracer = get_tracer()
        self._cause: Optional[str] = None

    # ------------------------------------------------------------------
    # Causal cursor
    # ------------------------------------------------------------------

    @property
    def current_cause(self) -> Optional[str]:
        """The event id that new events will be attributed to."""
        return self._cause

    def set_cause(self, event_id: Optional[str]) -> None:
        """Move the cursor. Used on resume, to reattach to a prior event."""
        self._cause = event_id

    @contextmanager
    def under(self, event_id: str) -> Iterator[str]:
        """Record everything in this block as caused by ``event_id``."""
        previous = self._cause
        self._cause = event_id
        try:
            yield event_id
        finally:
            self._cause = previous

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def record(
        self,
        event_type: EventType,
        payload: Dict[str, Any],
        actor: Optional[str] = None,
        caused_by: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Append one event under the current cause.

        Args:
            event_type: The kind of event.
            payload: Must match the schema for this event type.
            actor: Overrides the recorder's default actor.
            caused_by: Overrides the causal cursor for this one event.
            labels: Invisible Ink labels. Phase 4 fills these.

        Returns:
            The new event id.
        """
        return self.store.append_event(
            case_id=self.case_id,
            event_type=event_type,
            payload=payload,
            actor=actor or self.actor,
            caused_by=caused_by if caused_by is not None else self._cause,
            labels=labels,
        )

    def thought(
        self,
        reasoning: str,
        decision: str,
        confidence: float,
        context_summary: str,
        **kwargs: Any,
    ) -> str:
        """Record Gemini's stated rationale.

        Reasoning is a first-class artifact, not a discarded intermediate. This
        stores what the model said, not a summary of it.
        """
        return self.record(
            EventType.THOUGHT,
            {
                "reasoning": reasoning,
                "decision": decision,
                "confidence": confidence,
                "context_summary": context_summary,
            },
            **kwargs,
        )

    def tool_call(
        self, tool_name: str, parameters: Dict[str, Any], intended_outcome: str, **kwargs: Any
    ) -> str:
        """Record a tool being called, and what the agent expected from it."""
        return self.record(
            EventType.TOOL_CALL,
            {
                "tool_name": tool_name,
                "parameters": parameters,
                "intended_outcome": intended_outcome,
            },
            **kwargs,
        )

    def tool_result(
        self,
        tool_name: str,
        success: bool,
        result: Any,
        error_message: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Record what a tool returned, success or failure."""
        return self.record(
            EventType.TOOL_RESULT,
            {
                "tool_name": tool_name,
                "success": success,
                "result": result,
                "error_message": error_message,
            },
            **kwargs,
        )

    def memory_write(self, memory_key: str, content: Any, reason: str, **kwargs: Any) -> str:
        """Record a write to the Wiki."""
        return self.record(
            EventType.MEMORY_WRITE,
            {"memory_key": memory_key, "content": content, "reason": reason},
            **kwargs,
        )

    def memory_read(self, memory_key: str, content: Any, reason: str, **kwargs: Any) -> str:
        """Record a read from the Wiki."""
        return self.record(
            EventType.MEMORY_READ,
            {"memory_key": memory_key, "content": content, "reason": reason},
            **kwargs,
        )

    def escalate(
        self,
        reason: str,
        escalation_type: str,
        context: Dict[str, Any],
        urgency: str,
        **kwargs: Any,
    ) -> str:
        """Record an escalation the agent decided on."""
        return self.record(
            EventType.ESCALATE,
            {
                "reason": reason,
                "escalation_type": escalation_type,
                "context": context,
                "urgency": urgency,
            },
            **kwargs,
        )

    def suspend(
        self,
        reason: str,
        condition: "WakeCondition",
        state_snapshot: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """Record that an agent is stopping, and what would wake it.

        Writing this event is what makes the wait survivable. After it, no
        process needs to remain: the condition is in the log, and any instance
        can find it.
        """
        from .wake import build_suspend_payload

        return self.record(
            EventType.SUSPEND,
            build_suspend_payload(reason, condition, state_snapshot),
            **kwargs,
        )

    def resume(
        self,
        suspend_event_id: str,
        reason: str,
        wake_trigger: Dict[str, Any],
        state_restored: bool = True,
        **kwargs: Any,
    ) -> str:
        """Record that a suspended agent has picked the work back up.

        Always caused by the SUSPEND it answers. That link is what closes the
        wait: an open suspension is defined as a SUSPEND with no RESUME pointing
        at it, so getting this wrong would leave the case waiting forever.
        """
        event_id = self.record(
            EventType.RESUME,
            {
                "reason": reason,
                "wake_trigger": wake_trigger,
                "state_restored": state_restored,
            },
            caused_by=suspend_event_id,
            **kwargs,
        )
        # New work continues from the resumption, not from wherever the cursor
        # happened to be left when the process that suspended went away.
        self._cause = event_id
        return event_id

    def policy_check(
        self,
        policy_id: str,
        check_type: str,
        input_data: Dict[str, Any],
        decision: str,
        reasoning: str,
        **kwargs: Any,
    ) -> str:
        """Record a governance decision and the reasoning behind it."""
        return self.record(
            EventType.POLICY_CHECK,
            {
                "policy_id": policy_id,
                "check_type": check_type,
                "input_data": input_data,
                "decision": decision,
                "reasoning": reasoning,
            },
            **kwargs,
        )

    def message_sent(
        self, recipient: str, channel: str, content: str, purpose: str, **kwargs: Any
    ) -> str:
        """Record something the customer or a regulator actually received."""
        return self.record(
            EventType.MESSAGE_SENT,
            {
                "recipient": recipient,
                "channel": channel,
                "content": content,
                "purpose": purpose,
            },
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Reading. For inspection and replay, not for agents mid-workflow.
    # ------------------------------------------------------------------

    def events(self) -> List[Event]:
        """Every event for this case, in creation order."""
        return self.store.list_events(self.case_id)

    def state(self) -> CaseState:
        """Fold this case's events into current state."""
        return fold_events(self.events())

    def assert_causally_complete(self) -> None:
        """Verify the case has exactly one root and no orphans.

        Raises AssertionError if the causal tree is broken. Called by the tests
        and by the trace endpoint, so a regression surfaces as a failure rather
        than as a log that merely looks fine.
        """
        events = self.events()
        if not events:
            raise AssertionError(f"Case {self.case_id} has no events")

        known = {e.event_id for e in events}
        roots = [e for e in events if e.caused_by is None]
        orphans = [e for e in events if e.caused_by is not None and e.caused_by not in known]

        if len(roots) != 1:
            raise AssertionError(
                f"Case {self.case_id} has {len(roots)} root events, expected exactly 1: "
                f"{[e.event_id for e in roots]}"
            )
        if orphans:
            raise AssertionError(
                f"Case {self.case_id} has events whose caused_by points nowhere: "
                f"{[(e.event_id, e.caused_by) for e in orphans]}"
            )

    def causal_tree(self) -> Dict[str, Any]:
        """Return this case's events as a nested tree, root first.

        This is the shape the Phase 10 Split Screen renders, and it is what makes
        the log legible as a causal tree rather than a flat list.
        """
        events = self.events()
        by_parent: Dict[Optional[str], List[Event]] = {}
        for event in events:
            by_parent.setdefault(event.caused_by, []).append(event)

        def build(event: Event) -> Dict[str, Any]:
            return {
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "actor": event.actor,
                "timestamp": event.timestamp.isoformat(),
                "payload": event.payload,
                "labels": event.labels,
                "caused": [build(child) for child in by_parent.get(event.event_id, [])],
            }

        roots = by_parent.get(None, [])
        return {
            "case_id": self.case_id,
            "event_count": len(events),
            "tree": [build(root) for root in roots],
        }
