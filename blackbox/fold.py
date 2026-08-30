"""Fold function: compute case state from event log"""

from datetime import datetime
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from .schema import Event, EventType
from .event_store import EventStore


class CaseState(BaseModel):
    """
    Computed state of a case, derived purely from its event log.
    
    This is never stored, only computed on demand.
    """
    case_id: str = Field(..., description="Case identifier")
    current_status: str = Field(..., description="Current status of the case")
    events: List[Event] = Field(default_factory=list, description="All events in order")
    last_updated: datetime = Field(..., description="Timestamp of most recent event")
    pending_actions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Actions waiting to be completed"
    )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "case_id": self.case_id,
            "current_status": self.current_status,
            "events": [e.to_firestore_dict() for e in self.events],
            "last_updated": self.last_updated.isoformat(),
            "pending_actions": self.pending_actions,
        }


def fold_case(case_id: str, project_id: str = "test-project") -> CaseState:
    """
    Compute current state of a case from its event log.
    
    This is a PURE FUNCTION: no side effects, no caching, deterministic.
    Given the same events, it always produces the same output.
    
    The fold walks through events in order and derives state transitions:
    - Initial state is "open" when first event arrives
    - SUSPEND events add to pending_actions
    - RESUME events remove from pending_actions
    - ESCALATE events change status to "escalated"
    - Final status depends on the last state-changing event
    
    Args:
        case_id: Case identifier
        project_id: Google Cloud project ID
        
    Returns:
        CaseState computed from the event log
    """
    
    # Read all events in order (ULID order = creation order)
    store = EventStore(project_id=project_id)
    events = store.list_events(case_id)
    
    if not events:
        raise ValueError(f"No events found for case: {case_id}")
    
    # Initialize state
    current_status = "open"
    pending_actions = []
    last_updated = events[0].timestamp
    
    # Walk through events and derive state
    for event in events:
        last_updated = event.timestamp
        
        # State transitions based on event type
        if event.event_type == EventType.SUSPEND:
            # Agent suspended, add to pending actions
            pending_actions.append({
                "event_id": event.event_id,
                "reason": event.payload.get("reason", "Unknown"),
                "wake_condition": event.payload.get("wake_condition", {}),
                "suspended_at": event.timestamp.isoformat(),
            })
        
        elif event.event_type == EventType.RESUME:
            # Agent resumed, remove from pending actions
            # Find the matching suspend event (by caused_by)
            pending_actions = [
                pa for pa in pending_actions
                if pa["event_id"] != event.caused_by
            ]
        
        elif event.event_type == EventType.ESCALATE:
            # Case escalated to human
            current_status = "escalated"
        
        elif event.event_type == EventType.MESSAGE_SENT:
            # Message sent, check if it's a final response
            if "final" in event.payload.get("purpose", "").lower():
                current_status = "closed"
    
    # If there are pending actions, status is "waiting"
    if pending_actions and current_status == "open":
        current_status = "waiting"
    
    return CaseState(
        case_id=case_id,
        current_status=current_status,
        events=events,
        last_updated=last_updated,
        pending_actions=pending_actions,
    )


def fold_events(events: List[Event]) -> CaseState:
    """
    Compute state from a list of events (without reading from Firestore).
    
    This is useful for testing and for replay scenarios.
    
    Args:
        events: List of Event objects in order
        
    Returns:
        CaseState computed from the events
    """
    
    if not events:
        raise ValueError("No events provided")
    
    case_id = events[0].case_id
    
    # Initialize state
    current_status = "open"
    pending_actions = []
    last_updated = events[0].timestamp
    
    # Walk through events and derive state
    for event in events:
        last_updated = event.timestamp
        
        if event.event_type == EventType.SUSPEND:
            pending_actions.append({
                "event_id": event.event_id,
                "reason": event.payload.get("reason", "Unknown"),
                "wake_condition": event.payload.get("wake_condition", {}),
                "suspended_at": event.timestamp.isoformat(),
            })
        
        elif event.event_type == EventType.RESUME:
            pending_actions = [
                pa for pa in pending_actions
                if pa["event_id"] != event.caused_by
            ]
        
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
    )
