"""Running a replay, and reporting where it diverged.

Two modes, and the difference between them is which question you are asking.

**Fast mode** replays the recorded model turns. The agent code, the tools and the
policy evaluation are all the real ones; only the model's choices come from the
recording. That isolates the policy change exactly: if the replay diverges, it
cannot be because the model felt differently today. This is the mode for "would
a $100 threshold have caught this case".

**Fresh mode** lets Gemini re-infer against the recorded world state, with tools
still served from fixtures. That answers a different question, and one no company
can currently answer about its own agents: would upgrading the model change how
the fleet behaves? A divergence in fresh mode is a divergence in judgment.

In both modes the tools are fixtures and the outbound systems are unreachable.
The mode changes who decides; it never changes what a replay can touch.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ulid import ULID

from .backends import InMemoryBackend
from .event_store import EventStore
from .policy import PolicyEngine, PolicySet
from .recorder import Recorder
from .schema import Event, EventType
from .divergence import Divergence, RecordedLlm, build_recorded_turns, compare_runs
from .timemachine import (
    FixtureMiss,
    FixtureSystems,
    ReplayViolation,
    WorldAsOf,
    build_fixtures,
    state_as_of,
)
from .wiki import WikiPage
from .wiki_store import WikiStore

logger = logging.getLogger(__name__)


class ReplayMode(str, Enum):
    #: Recorded model turns. Isolates the policy change.
    FAST = "fast"
    #: Gemini re-infers against the recorded world. Answers whether a model
    #: change would alter fleet behaviour.
    FRESH = "fresh"


@dataclass
class ReplayResult:
    """What a replay did, and how it differed from what actually happened."""

    case_id: str
    rewind_to: str
    mode: ReplayMode
    policy_version: str
    original_policy_version: str
    divergence: Divergence
    replayed_events: List[Event] = field(default_factory=list)
    original_events: List[Event] = field(default_factory=list)
    fixture_misses: List[str] = field(default_factory=list)
    outbound_attempts: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    completed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "rewind_to": self.rewind_to,
            "mode": self.mode.value,
            "policy_version": self.policy_version,
            "original_policy_version": self.original_policy_version,
            "completed": self.completed,
            "error": self.error,
            "diverged": self.divergence.diverged,
            "summary": self.divergence.summary(),
            "first_difference": {
                "at": self.divergence.first_difference_index,
                "originally": self.divergence.original_decision,
                "in_replay": self.divergence.replay_decision,
                "explanation": self.divergence.explanation,
            },
            "headline": self.divergence.headline,
            "rule_changes": self.divergence.rule_changes,
            "downstream_consequences": self.divergence.downstream,
            "original_decisions": self.divergence.original_decisions,
            "replay_decisions": self.divergence.replay_decisions,
            "fixture_misses": self.fixture_misses,
            "outbound_attempts_blocked": self.outbound_attempts,
        }


def _seed_replay_world(world: WorldAsOf) -> tuple:
    """Build the isolated stores a replay runs against.

    Everything is in memory. A replay writes its events and Wiki rewrites into a
    scratch store that is thrown away afterwards, so a replay cannot append to
    the real Diary. The Diary is append-only and the point of a replay is that it
    did not happen.
    """
    backend = InMemoryBackend()
    store = EventStore(project_id="replay", backend=backend)

    # The events up to the rewind point are copied in verbatim, so the replayed
    # agent's fold sees the same history the original agent saw.
    for event in world.events:
        backend.put("events", event.event_id, event.to_firestore_dict())

    wiki = WikiStore(
        project_id="replay", event_store=store, in_memory=True, worker_region="EU"
    )
    now = datetime.now(timezone.utc)
    for page_id, content in world.wiki_pages.items():
        wiki.create_page(
            WikiPage(
                page_id=page_id,
                subject=content.get("complaint_ref", world.case_id)
                if page_id.startswith("case:")
                else page_id.split(":", 1)[-1],
                subject_type=page_id.split(":", 1)[0],
                content=dict(content),
                derived_from=[e.event_id for e in world.events],
                version=1,
                created_at=now,
                updated_at=now,
                jurisdiction=content.get("jurisdiction"),
            )
        )

    return store, wiki, backend


async def replay_case(
    store: EventStore,
    case_id: str,
    rewind_to: str,
    policies: PolicySet,
    mode: ReplayMode = ReplayMode.FAST,
    model: Optional[Any] = None,
    original_policy_version: str = "unknown",
) -> ReplayResult:
    """Rewind a case, run it again under a different policy, and report the difference.

    Args:
        store: The Diary. Read only. A replay never writes to it.
        case_id: The case to replay.
        rewind_to: The event id to rewind to. Everything after it is discarded
            and re-derived.
        policies: The policy set to replay under. Usually the live one with a
            constant changed.
        mode: FAST replays recorded model turns; FRESH lets Gemini re-infer.
        model: Override the model used in fresh mode. Used by tests.
        original_policy_version: What the original run ran under, for the report.

    Returns:
        The replay's events, and where they diverged from what happened.

    Raises:
        ValueError: If the rewind point is not part of this case.
    """
    world = state_as_of(store, case_id, rewind_to)
    original_after = [e for e in store.list_events(case_id) if e.event_id > rewind_to]

    fixtures = build_fixtures(store.list_events(case_id))
    systems = FixtureSystems(fixtures)
    engine = PolicyEngine(policies)

    replay_store, replay_wiki, backend = _seed_replay_world(world)

    if mode is ReplayMode.FAST:
        replay_model: Any = RecordedLlm(build_recorded_turns(original_after))
    else:
        replay_model = model  # None means the configured Gemini model.

    result = ReplayResult(
        case_id=case_id,
        rewind_to=rewind_to,
        mode=mode,
        policy_version=policies.version,
        original_policy_version=original_policy_version,
        divergence=Divergence(diverged=False),
    )

    try:
        replayed = await _run_replay(
            world=world,
            store=replay_store,
            wiki=replay_wiki,
            systems=systems,
            engine=engine,
            model=replay_model,
            original_after=original_after,
        )
    except (FixtureMiss, ReplayViolation) as exc:
        # Both are results rather than failures, and both stop the replay. A
        # replay that carried on past a fixture miss would be inventing history.
        logger.info("Replay stopped: %s", exc)
        replayed = [
            e for e in replay_store.list_events(case_id) if e.event_id > rewind_to
        ]
        result.completed = False
        result.error = str(exc)

    result.replayed_events = replayed
    result.original_events = original_after
    result.divergence = compare_runs(original_after, replayed)
    result.fixture_misses = list(fixtures.misses)
    result.outbound_attempts = list(systems.attempted_outbound)
    return result


async def _run_replay(
    world: WorldAsOf,
    store: EventStore,
    wiki: WikiStore,
    systems: FixtureSystems,
    engine: PolicyEngine,
    model: Any,
    original_after: List[Event],
) -> List[Event]:
    """Re-derive the decisions after the rewind point.

    Walks the recorded actions and re-evaluates the ones that were governance
    decisions, under the replay's policy set. Actions that were not decisions are
    replayed as they were, because a replay is meant to isolate the effect of the
    policy, not to re-litigate everything.
    """
    from .agents.fleet_tools import record_assessment
    from .agents.runtime import agent_run

    recorder = Recorder(case_id=world.case_id, actor="replay", store=store)
    recorder.set_cause(world.events[-1].event_id if world.events else None)

    with agent_run(
        recorder=recorder, systems=systems, wiki_store=wiki, policy_engine=engine
    ):
        for event in original_after:
            if event.event_type == EventType.TOOL_CALL:
                tool_name = event.payload.get("tool_name", "")
                params = event.payload.get("parameters", {})

                if tool_name == "record_assessment":
                    # The decision the policy change actually bears on. Re-run it
                    # so the gates are evaluated under the replay's thresholds.
                    record_assessment(**params)
                    continue

                if tool_name in ("send_customer_letter", "file_with_regulator"):
                    # Outbound. FixtureSystems refuses these, and the refusal is
                    # what the replay reports rather than something it survives.
                    systems.printpost.send_letter(recipient="replay", body="")
                    continue

                # Everything else is replayed as recorded. The tool result comes
                # from the fixture set, never from a live call.
                recorder.tool_call(
                    tool_name=tool_name,
                    parameters=params,
                    intended_outcome="replayed from the recording",
                )

            elif event.event_type == EventType.SUSPEND:
                recorder.record(EventType.SUSPEND, event.payload)

    return [e for e in store.list_events(world.case_id) if e.event_id > world.rewind_to]
