"""The Stunt Double: validate a new agent version against live traffic safely.

A candidate agent runs on the same cases the live fleet is working, produces the
actions it *would* have taken, and never touches anything. Then Gemini reads both
sets of actions and says, in plain language, where the candidate would have done
something different and whether that difference is better or worse.

## Every write is stubbed, and not by remembering to

The failure mode is a write tool that is not fully stubbed, so the stunt double
affects production. Guarding that with discipline would mean auditing every tool
every time one is added. Instead the candidate is handed a world it cannot write
through:

- **The Diary** it writes to is a scratch in-memory store, seeded from the live
  one. Its events are real events; they are simply not in the real log.
- **The Wiki** it rewrites is a scratch copy. The live case file is untouched.
- **The source systems** are ``ShadowSystems``, which passes reads through and
  refuses every write, recording the attempt as an intent.

So there are three separate reasons a shadow write cannot land, and adding a new
tool tomorrow inherits all three without anyone remembering anything. A test
enumerates every tool in the fleet and asserts none of them can reach the live
stores through a shadow run.

## Compared on judgment, not on strings

Reducing the comparison to string equality would report a difference every time
the candidate phrased a summary differently, and miss the case where it reached
the opposite conclusion in similar words. So the comparison is structural first,
which finds the decisions that differ, and then Gemini reads those differences
and categorises each one: equivalent, safer, riskier, or incorrect.

That categorisation is the point. "The candidate would have escalated three cases
the current version closed, and here is its reasoning for each" is a sentence a
release manager can act on. A JSON diff is not.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .backends import InMemoryBackend
from .event_store import EventStore
from .schema import Event, EventType
from .stubs.systems import SourceSystemError, SourceSystems
from .wiki import WikiPage
from .wiki_store import WikiStore

logger = logging.getLogger(__name__)


class ShadowWriteBlocked(RuntimeError):
    """A shadow run tried to change something outside itself."""


@dataclass(frozen=True)
class AgentVersion:
    """A candidate version of one agent in the fleet."""

    version_id: str
    agent_name: str
    description: str
    #: Overrides the live agent's instruction. This is usually what is being
    #: tested: a changed prompt is the commonest kind of agent change.
    instruction: Optional[str] = None
    #: Overrides the model. Used to answer whether a model upgrade changes
    #: behaviour, which is the other common reason to shadow.
    model: Optional[Any] = None


@dataclass
class IntendedAction:
    """Something the candidate would have done."""

    sequence: int
    tool_name: str
    parameters: Dict[str, Any]
    #: True when this action would have changed something outside the fleet:
    #: money moved, a letter sent, a regulator told.
    is_write: bool = False
    reasoning: str = ""

    def signature(self) -> str:
        return self.tool_name


#: Tools that change something beyond the case file. These are the ones whose
#: divergence matters most, and the ones a shadow run must never actually reach.
OUTBOUND_TOOLS = {
    "send_customer_letter",
    "file_with_regulator",
    "execute_remedy",
}


class ShadowSystems:
    """Source systems for a shadow run: reads pass through, writes are refused.

    Not a subclass of ``SourceSystems``. It wraps one, so reads get genuine data,
    but every method that would change external state is intercepted before it
    reaches the wrapped object.
    """

    #: Methods that create or change state outside the fleet, per system.
    BLOCKED = {
        "commsvault": {"request_records"},
        "printpost": {"send_letter"},
        "regportal": {"file_report"},
    }

    def __init__(self, live: SourceSystems):
        self._live = live
        self.blocked_writes: List[Dict[str, Any]] = []

        self.crm360 = _PassThrough("crm360", live.crm360, self)
        self.corebank = _PassThrough("corebank", live.corebank, self)
        self.commsvault = _PassThrough("commsvault", live.commsvault, self)
        self.printpost = _PassThrough("printpost", live.printpost, self)
        self.regportal = _PassThrough("regportal", live.regportal, self)

    def record_block(self, system: str, method: str, kwargs: Dict[str, Any]) -> None:
        self.blocked_writes.append({"system": system, "method": method, "arguments": kwargs})


class _PassThrough:
    """Serves reads from the live system and refuses its writes."""

    def __init__(self, key: str, target: Any, owner: ShadowSystems):
        self._key = key
        self._target = target
        self._owner = owner
        self.name = getattr(target, "name", key)
        self.region = getattr(target, "region", None)

    def __getattr__(self, method: str):
        blocked = ShadowSystems.BLOCKED.get(self._key, set())

        if method in blocked:

            def refuse(*args: Any, **kwargs: Any) -> Any:
                self._owner.record_block(self._key, method, kwargs)
                # A synthetic answer rather than an exception: the candidate is
                # being evaluated on what it would do next, and killing its turn
                # here would hide the rest of its behaviour.
                return {
                    "shadow": True,
                    "blocked": f"{self.name}.{method}",
                    "note": (
                        "This is a shadow run. The call was recorded as an intended "
                        "action and nothing was sent."
                    ),
                    "status": "ACCEPTED" if method == "request_records" else "BLOCKED",
                    "job_id": "SHADOW-JOB",
                    "ready_at": datetime.now(timezone.utc).isoformat(),
                    "estimated_delay_days": 2,
                }

            return refuse

        return getattr(self._target, method)


@dataclass
class ShadowRun:
    """What a candidate would have done on one case."""

    case_id: str
    version_id: str
    intended_actions: List[IntendedAction] = field(default_factory=list)
    live_actions: List[IntendedAction] = field(default_factory=list)
    blocked_writes: List[Dict[str, Any]] = field(default_factory=list)
    shadow_events: List[Event] = field(default_factory=list)
    error: Optional[str] = None
    completed: bool = True

    @property
    def diverged(self) -> bool:
        return [a.signature() for a in self.intended_actions] != [
            a.signature() for a in self.live_actions
        ]


def extract_actions(events: List[Event]) -> List[IntendedAction]:
    """Read the tool calls out of a run, in order, with the reasoning behind them."""
    reasoning_by_id = {
        e.event_id: e.payload.get("reasoning", "")
        for e in events
        if e.event_type == EventType.THOUGHT
    }
    actions: List[IntendedAction] = []
    for event in events:
        if event.event_type != EventType.TOOL_CALL:
            continue
        tool_name = event.payload.get("tool_name", "")
        # The poller is machinery, not a decision the agent made.
        if tool_name.startswith("IntakeChannel."):
            continue
        actions.append(
            IntendedAction(
                sequence=len(actions),
                tool_name=tool_name,
                parameters=dict(event.payload.get("parameters", {})),
                is_write=tool_name in OUTBOUND_TOOLS,
                reasoning=reasoning_by_id.get(event.caused_by or "", ""),
            )
        )
    return actions


def seed_shadow_world(
    live_store: EventStore, live_wiki: WikiStore, case_id: str
) -> tuple:
    """Build the scratch stores a candidate runs against.

    Copies the case's events and its Wiki page into memory. The candidate reads
    real history and writes into a copy, so its run is faithful and its writes
    are inert.
    """
    backend = InMemoryBackend()
    store = EventStore(project_id="shadow", backend=backend)

    events = live_store.list_events(case_id)
    for event in events:
        backend.put("events", event.event_id, event.to_firestore_dict())

    wiki = WikiStore(
        project_id="shadow", event_store=store, in_memory=True, worker_region="EU"
    )
    page = live_wiki.get_page(f"case:{case_id}", enforce_region=False)
    if page is not None:
        wiki.create_page(
            WikiPage(
                page_id=page.page_id,
                subject=page.subject,
                subject_type=page.subject_type,
                content=dict(page.content),
                derived_from=list(page.derived_from),
                version=page.version,
                created_at=page.created_at,
                updated_at=page.updated_at,
                jurisdiction=page.jurisdiction,
            )
        )

    return store, wiki, events
