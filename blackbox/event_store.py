"""Event store for BLACKBOX Flight Recorder"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from google.cloud import firestore
from .schema import Event, EventType, PAYLOAD_SCHEMAS
from .opentelemetry_setup import get_tracer


class EventStore:
    """
    Firestore-backed storage for BLACKBOX events.
    
    Implements the append-only write path for the Flight Recorder.
    Events are immutable once written and form a causal tree via caused_by links.
    """
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self._client = None
        self._tracer = get_tracer()
    
    @property
    def client(self) -> firestore.Client:
        """Lazy initialization of Firestore client"""
        if self._client is None:
            self._client = firestore.Client(project=self.project_id)
        return self._client
    
    def append_event(
        self,
        case_id: str,
        event_type: EventType,
        payload: Dict[str, Any],
        actor: str,
        caused_by: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Append an event to the Flight Recorder.
        
        This is the ONLY write method. There is no update() or delete().
        Events are immutable once written.
        
        Args:
            case_id: The case this event belongs to
            event_type: Type of event (THOUGHT, TOOL_CALL, etc.)
            payload: Structured data for this event type
            actor: Which agent or system produced this event
            caused_by: Optional parent event_id (forms causal tree)
            labels: Optional data labels for Invisible Ink (Phase 4)
        
        Returns:
            event_id: ULID of the created event
        
        Raises:
            ValueError: If payload doesn't match schema for event_type
        """
        # Validate payload against schema
        schema_class = PAYLOAD_SCHEMAS.get(event_type)
        if schema_class is None:
            raise ValueError(f"No payload schema defined for event_type: {event_type}")
        
        try:
            schema_class(**payload)
        except Exception as e:
            raise ValueError(f"Payload validation failed for {event_type}: {e}")
        
        # Generate ULID
        from ulid import ULID
        event_id = str(ULID())
        
        # Create event
        event = Event(
            event_id=event_id,
            timestamp=datetime.utcnow(),
            actor=actor,
            event_type=event_type,
            caused_by=caused_by,
            payload=payload,
            labels=labels or {},
            trace_id=self._tracer.trace_id,
            span_id=self._tracer.span_id,
            case_id=case_id,
        )
        
        # Write to Firestore
        with self._tracer.start_span(f"append_event:{event_type.value}") as span:
            doc_ref = self.client.collection("events").document(event_id)
            doc_ref.set(event.to_firestore_dict())
            
            span.set_attribute("event_id", event_id)
            span.set_attribute("case_id", case_id)
            span.set_attribute("event_type", event_type.value)
            if caused_by:
                span.set_attribute("caused_by", caused_by)
        
        return event_id
    
    def get_event(self, event_id: str) -> Optional[Event]:
        """
        Retrieve a single event by ID.
        
        Args:
            event_id: ULID of the event
        
        Returns:
            Event object or None if not found
        """
        with self._tracer.start_span("get_event") as span:
            doc_ref = self.client.collection("events").document(event_id)
            doc = doc_ref.get()
            
            if not doc.exists:
                return None
            
            return Event.from_firestore_dict(doc.to_dict())
    
    def list_events(
        self,
        case_id: str,
        limit: Optional[int] = None,
    ) -> List[Event]:
        """
        List all events for a case, ordered by timestamp.
        
        Args:
            case_id: The case to query
            limit: Optional maximum number of events to return
        
        Returns:
            List of Event objects, ordered by timestamp (oldest first)
        """
        with self._tracer.start_span("list_events") as span:
            query = (
                self.client.collection("events")
                .where("case_id", "==", case_id)
                .order_by("timestamp")
            )
            
            if limit:
                query = query.limit(limit)
            
            events = []
            for doc in query.stream():
                events.append(Event.from_firestore_dict(doc.to_dict()))
            
            span.set_attribute("case_id", case_id)
            span.set_attribute("event_count", len(events))
            
            return events
    
    def list_events_by_type(
        self,
        case_id: str,
        event_type: EventType,
    ) -> List[Event]:
        """
        List events of a specific type for a case.
        
        Args:
            case_id: The case to query
            event_type: Type of events to retrieve
        
        Returns:
            List of Event objects of the specified type
        """
        with self._tracer.start_span("list_events_by_type") as span:
            query = (
                self.client.collection("events")
                .where("case_id", "==", case_id)
                .where("event_type", "==", event_type.value)
                .order_by("timestamp")
            )
            
            events = []
            for doc in query.stream():
                events.append(Event.from_firestore_dict(doc.to_dict()))
            
            span.set_attribute("case_id", case_id)
            span.set_attribute("event_type", event_type.value)
            span.set_attribute("event_count", len(events))
            
            return events
    
    def get_causal_chain(self, event_id: str) -> List[Event]:
        """
        Retrieve the full causal chain leading to an event.
        
        Args:
            event_id: Starting event ID
        
        Returns:
            List of Event objects from root to the specified event
        """
        with self._tracer.start_span("get_causal_chain") as span:
            chain = []
            current_id = event_id
            
            while current_id:
                event = self.get_event(current_id)
                if not event:
                    break
                
                chain.append(event)
                current_id = event.caused_by
            
            # Reverse to get root-to-leaf order
            chain.reverse()
            
            span.set_attribute("chain_length", len(chain))
            
            return chain
    
    def get_children(self, event_id: str) -> List[Event]:
        """
        Get all events that were caused by a given event.
        
        Args:
            event_id: Parent event ID
        
        Returns:
            List of child Event objects
        """
        with self._tracer.start_span("get_children") as span:
            query = (
                self.client.collection("events")
                .where("caused_by", "==", event_id)
                .order_by("timestamp")
            )
            
            children = []
            for doc in query.stream():
                children.append(Event.from_firestore_dict(doc.to_dict()))
            
            span.set_attribute("parent_event_id", event_id)
            span.set_attribute("children_count", len(children))
            
            return children
