"""The Time Machine: rewind, alter a rule, replay, and see what would have happened.

Two things in this module are dangerous enough to state before anything else.

## A replay must never touch a live system

The spec calls a replay that can reach production the most dangerous defect
possible in this build, and it is right. A replay of a refund case that reached
CoreBank would issue the refund again.

The defence here is not discipline, it is capability. A replay does not run
against ``SourceSystems`` with a flag set. It runs against ``FixtureSystems``,
which is a different class holding a dictionary. It has no clients, no network
code, and no way to reach anything. There is nothing to disable because there is
nothing there.

**A fixture miss raises.** It does not fall through, retry, or return an empty
result. ``FixtureMiss`` propagates and the replay stops, because a replay that
silently substituted a blank answer for a missing recording would produce a
confident, wrong divergence report, and a report you cannot trust is worse than
no report.

## State as-of, not state now

The other way a replay lies is by reading current state. Rewind to day six of a
case that has since closed, and if the agent reads today's Wiki page it sees the
outcome it is supposed to be deciding.

So ``state_as_of`` reconstructs the world at the rewind point from the event log
alone. Event ids are ULIDs, whose leading bits are a millisecond timestamp, so
"every event at or before the rewind point" is a lexical comparison on the id.
The Wiki is rebuilt the same way, by replaying the MEMORY_WRITE events up to that
point, which is why those events carry the resulting page content. Nothing in
this module reads a live Wiki page.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .event_store import EventStore
from .fold import CaseState, fold_events
from .policy import PolicyEngine, PolicySet
from .schema import Event, EventType
from .wiki import WikiPage

logger = logging.getLogger(__name__)


class FixtureMiss(RuntimeError):
    """A replay asked for something that was never recorded.

    Raised, never swallowed. See the module docstring.
    """


class ReplayViolation(RuntimeError):
    """A replay attempted something a replay is not allowed to do."""


# ----------------------------------------------------------------------
# State as-of
# ----------------------------------------------------------------------


@dataclass
class WorldAsOf:
    """The world at a point in the past, rebuilt from the log alone."""

    case_id: str
    rewind_to: str
    events: List[Event]
    wiki_pages: Dict[str, Dict[str, Any]]
    state: CaseState

    @property
    def rewind_at(self) -> Optional[datetime]:
        return self.events[-1].timestamp if self.events else None

    def page(self, page_id: str) -> Optional[Dict[str, Any]]:
        return self.wiki_pages.get(page_id)


def state_as_of(store: EventStore, case_id: str, rewind_to: str) -> WorldAsOf:
    """Rebuild the world as it stood immediately after a given event.

    Args:
        store: The Diary.
        case_id: The case to rebuild.
        rewind_to: The event id to rewind to. That event is included; everything
            after it is not.

    Returns:
        The events up to that point, the Wiki as it stood, and the folded state.

    Raises:
        ValueError: If the case has no events, or the rewind point is not one of
            them. Rewinding to an event that does not belong to this case would
            produce a plausible-looking world assembled from the wrong history.
    """
    all_events = store.list_events(case_id)
    if not all_events:
        raise ValueError(f"Case {case_id} has no events to rewind through")

    if not any(e.event_id == rewind_to for e in all_events):
        raise ValueError(
            f"Event {rewind_to} is not part of case {case_id}. Rewinding to it would "
            f"build a world out of the wrong history."
        )

    # ULIDs sort lexically in creation order, so this is "everything up to and
    # including the rewind point".
    events = [e for e in all_events if e.event_id <= rewind_to]

    return WorldAsOf(
        case_id=case_id,
        rewind_to=rewind_to,
        events=events,
        wiki_pages=wiki_as_of(events),
        state=fold_events(events),
    )


def wiki_as_of(events: List[Event]) -> Dict[str, Any]:
    """Rebuild the Wiki from the MEMORY_WRITE events in a window.

    The Wiki is current state and would be contaminated ground for a replay, so
    it is reconstructed rather than read. Each MEMORY_WRITE carries the page
    content that resulted from it, and replaying them in order lands on the page
    as it stood at the rewind point.

    A MEMORY_WRITE with no content recorded is skipped and logged. That is a gap
    in the recording rather than an empty page, and treating it as an empty page
    would hand a replayed agent a blank sheet.
    """
    pages: Dict[str, Dict[str, Any]] = {}
    for event in events:
        if event.event_type != EventType.MEMORY_WRITE:
            continue
        key = event.payload.get("memory_key", "")
        page_id = key.removeprefix("wiki:")
        content = event.payload.get("content")
        if not isinstance(content, dict) or not content:
            logger.debug(
                "MEMORY_WRITE %s carries no page content, skipping in reconstruction",
                event.event_id,
            )
            continue
        # Version bookkeeping travels alongside the content under a reserved key
        # and is not part of the page.
        page_content = {k: v for k, v in content.items() if k != "_version"}
        if not page_content:
            continue
        pages[page_id] = page_content
    return pages


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
    """A stable key for one tool call.

    Arguments are sorted so that a call made with the same values in a different
    order finds its recording.
    """
    try:
        rendered = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(sorted(args.items()))
    return f"{tool_name}({rendered})"


@dataclass
class FixtureSet:
    """Everything a replay is allowed to know, taken from the recording."""

    by_call: Dict[str, List[Any]] = field(default_factory=dict)
    by_tool: Dict[str, List[Any]] = field(default_factory=dict)
    model_turns: List[Dict[str, Any]] = field(default_factory=list)
    #: Every lookup that missed, for the report. A replay with misses is a replay
    #: whose divergence cannot be attributed to the policy change.
    misses: List[str] = field(default_factory=list)

    def tool_result(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """The recorded answer for a tool call.

        Matches on the exact call first, then falls back to the next unused
        recording for that tool. The fallback exists because a policy change can
        legitimately alter an argument (a different threshold in a message) while
        the underlying answer is the same. It never falls back to a live call.

        Raises:
            FixtureMiss: If nothing was recorded for this tool.
        """
        key = _fingerprint(tool_name, args)
        exact = self.by_call.get(key)
        if exact:
            return exact.pop(0)

        loose = self.by_tool.get(tool_name)
        if loose:
            return loose.pop(0)

        self.misses.append(key)
        raise FixtureMiss(
            f"No recorded result for {key}. A replay may not call a live system to "
            f"fill the gap, so it stops here. Either the rewind point is before this "
            f"tool was ever called, or the replay has diverged far enough to be doing "
            f"something the original run never did."
        )


def build_fixtures(events: List[Event]) -> FixtureSet:
    """Turn a recorded window into the fixtures a replay may draw on.

    Tool results come from TOOL_RESULT events. Model turns come from THOUGHT
    events paired with the TOOL_CALL events they caused, which is what lets fast
    mode replay the model's decisions without calling a model.
    """
    fixtures = FixtureSet()

    calls_by_id: Dict[str, Event] = {
        e.event_id: e for e in events if e.event_type == EventType.TOOL_CALL
    }

    for event in events:
        if event.event_type != EventType.TOOL_RESULT:
            continue
        tool_name = event.payload.get("tool_name", "")
        result = event.payload.get("result")

        parent = calls_by_id.get(event.caused_by or "")
        args = parent.payload.get("parameters", {}) if parent else {}

        fixtures.by_call.setdefault(_fingerprint(tool_name, args), []).append(result)
        fixtures.by_tool.setdefault(tool_name, []).append(result)

    for event in events:
        if event.event_type != EventType.THOUGHT:
            continue
        fixtures.model_turns.append(
            {
                "event_id": event.event_id,
                "actor": event.actor,
                "reasoning": event.payload.get("reasoning", ""),
                "decision": event.payload.get("decision", ""),
            }
        )

    return fixtures


class FixtureSystems:
    """A stand-in for the source systems that cannot reach anything.

    Deliberately not a subclass of ``SourceSystems`` and deliberately holding no
    clients. A replay handed this object could not call CoreBank if it wanted to,
    because there is no code here that knows how.

    The outbound systems raise rather than record. A replay that reached the
    point of sending a letter has told you what you needed to know; actually
    queueing it would be the defect this whole design exists to prevent.
    """

    def __init__(self, fixtures: FixtureSet):
        self._fixtures = fixtures
        self.attempted_outbound: List[Dict[str, Any]] = []

        self.crm360 = _FixtureFacade("CRM360", fixtures, self)
        self.corebank = _FixtureFacade("CoreBank", fixtures, self)
        self.commsvault = _FixtureFacade("CommsVault", fixtures, self)
        self.printpost = _OutboundBlocked("PrintPost", self)
        self.regportal = _OutboundBlocked("RegPortal", self)


class _FixtureFacade:
    """Serves recorded answers for one source system, and nothing else."""

    def __init__(self, name: str, fixtures: FixtureSet, owner: FixtureSystems):
        self.name = name
        self._fixtures = fixtures
        self._owner = owner

    def __getattr__(self, method: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            payload = {"args": list(args), **kwargs}
            return self._fixtures.tool_result(f"{self.name}.{method}", payload)

        return call


class _OutboundBlocked:
    """An outbound system a replay may not use.

    Records the attempt and raises. The attempt is the interesting part of a
    replay, and it is reported; the sending is the part that must not happen.
    """

    def __init__(self, name: str, owner: FixtureSystems):
        self.name = name
        self.region = "US" if name == "PrintPost" else "EU"
        self._owner = owner

    def __getattr__(self, method: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            self._owner.attempted_outbound.append(
                {"system": self.name, "method": method, "kwargs": kwargs}
            )
            raise ReplayViolation(
                f"A replay tried to call {self.name}.{method}. Outbound systems are "
                f"unreachable during a replay: the attempt has been recorded and "
                f"nothing was sent."
            )

        return call
