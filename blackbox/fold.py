"""The fold: compute case state from the event log.

State is never stored. It is computed by replaying the Diary in order. Wipe every
piece of derived state in the system, run the fold, and you are back where you
started. That property is what makes the Time Machine possible in Phase 6, so the
fold must stay a pure function of the events it is handed.

``fold_events`` is the whole implementation. ``fold_case`` only reads the log and
delegates, so there is one definition of what the events mean rather than two
copies that drift apart.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .config import get_settings
from .event_store import EventStore
from .schema import Event, EventType


class CaseState(BaseModel):
    """Computed state of a case, derived purely from its event log."""

    case_id: str = Field(..., description="Case identifier")
    current_status: str = Field(..., description="Current status of the case")
    events: List[Event] = Field(default_factory=list, description="All events in order")
    last_updated: datetime = Field(..., description="Timestamp of most recent event")
    pending_actions: List[Dict[str, Any]] = Field(
        default_factory=list, description="Waits the case is currently blocked on"
    )
    last_event_id: Optional[str] = Field(
        None, description="Id of the most recent event, used to chain the next one"
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "case_id": self.case_id,
            "current_status": self.current_status,
            "events": [e.to_firestore_dict() for e in self.events],
            "last_updated": self.last_updated.isoformat(),
            "pending_actions": self.pending_actions,
            "last_event_id": self.last_event_id,
        }


def fold_events(events: List[Event]) -> CaseState:
    """Compute state from an ordered list of events.

    Pure: no I/O, no caching, deterministic. The same events always produce the
    same state.

    State transitions:
      - A case is "open" from its first event.
      - SUSPEND adds a pending wait; the matching RESUME clears it.
      - ESCALATE moves the case to "escalated".
      - A MESSAGE_SENT whose purpose mentions a final response closes the case.
      - A case with pending waits and no stronger status is "waiting".
    """
    if not events:
        raise ValueError("No events provided")

    case_id = events[0].case_id
    current_status = "open"
    pending_actions: List[Dict[str, Any]] = []
    last_updated = events[0].timestamp

    for event in events:
        last_updated = event.timestamp

        if event.event_type == EventType.SUSPEND:
            pending_actions.append(
                {
                    "event_id": event.event_id,
                    "reason": event.payload.get("reason", "Unknown"),
                    "wake_condition": event.payload.get("wake_condition", {}),
                    "suspended_at": event.timestamp.isoformat(),
                }
            )

        elif event.event_type == EventType.RESUME:
            # A RESUME clears the SUSPEND it was caused by.
            pending_actions = [pa for pa in pending_actions if pa["event_id"] != event.caused_by]

        elif event.event_type == EventType.ESCALATE:
            current_status = "escalated"

        elif event.event_type == EventType.MESSAGE_SENT:
            if "final" in event.payload.get("purpose", "").lower():
                current_status = "closed"

    if pending_actions and current_status == "open":
        current_status = "waiting"

    return CaseState(
        case_id=case_id,
        current_status=current_status,
        events=events,
        last_updated=last_updated,
        pending_actions=pending_actions,
        last_event_id=events[-1].event_id,
    )


def fold_case(
    case_id: str,
    project_id: Optional[str] = None,
    store: Optional[EventStore] = None,
) -> CaseState:
    """Read a case's events and fold them into current state.

    Args:
        case_id: Case identifier.
        project_id: Google Cloud project. Defaults to the configured project.
        store: An existing EventStore to read through. Supplied by the agent
            runtime so a single store instance serves a whole request.

    Returns:
        The CaseState computed from the event log.
    """
    if store is None:
        settings = get_settings()
        store = EventStore(project_id=project_id or settings.project_id)

    events = store.list_events(case_id)
    if not events:
        raise ValueError(f"No events found for case: {case_id}")

    return fold_events(events)
