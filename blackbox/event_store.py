"""Event store for the BLACKBOX Flight Recorder.

The write path for the Diary. There is exactly one write method, ``append_event``,
and it is backed by Firestore ``create()`` rather than ``set()``, so an attempt to
overwrite an existing event fails at the database rather than succeeding quietly.
There is no update method and no delete method, and the destructive test suite
asserts that the public surface stays that way.

Ordering note: events are ordered by ``event_id``, not by ``timestamp``. Event ids
are ULIDs, whose leading 48 bits are a millisecond timestamp, so lexical ULID order
is creation order. Ordering on the timestamp string would depend on Python's
variable-width isoformat output and would tie-break arbitrarily.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ulid import ULID

from .backends import AppendOnlyBackend, build_backend
from .config import get_settings
from .opentelemetry_setup import current_trace_ids, get_tracer
from .schema import PAYLOAD_SCHEMAS, Event, EventType


class EventStore:
    """Append-only storage for BLACKBOX events.

    Events are immutable once written and form a causal tree through ``caused_by``.
    The store never computes or caches derived state: that is the fold function's
    job, and keeping it out of here is what stops a convenience cache from drifting
    away from the log.
    """

    def __init__(
        self,
        project_id: str,
        backend: Optional[AppendOnlyBackend] = None,
        in_memory: Optional[bool] = None,
    ):
        settings = get_settings()
        self.project_id = project_id
        self._collection = settings.events_collection
        self._tracer = get_tracer()
        if backend is not None:
            self._backend = backend
        else:
            use_memory = settings.in_memory if in_memory is None else in_memory
            self._backend = build_backend(
                project_id=project_id,
                in_memory=use_memory,
                database=settings.firestore_database,
            )

    def append_event(
        self,
        case_id: str,
        event_type: EventType,
        payload: Dict[str, Any],
        actor: str,
        caused_by: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Append one event to the Flight Recorder.

        This is the only write method in the system. Events are immutable once
        written.

        Args:
            case_id: The case this event belongs to.
            event_type: THOUGHT, TOOL_CALL, and so on.
            payload: Structured data matching the schema for this event type.
            actor: Which agent or system produced this event.
            caused_by: Parent event_id. Null only for the root event of a case.
            labels: Data labels for Invisible Ink. Populated in Phase 4.

        Returns:
            The ULID of the created event.

        Raises:
            ValueError: If the payload does not match the schema for event_type.
            DocumentAlreadyExists: If the generated id somehow collides.
        """
        schema_class = PAYLOAD_SCHEMAS.get(event_type)
        if schema_class is None:
            raise ValueError(f"No payload schema defined for event_type: {event_type}")

        try:
            schema_class(**payload)
        except Exception as exc:
            raise ValueError(f"Payload validation failed for {event_type}: {exc}") from exc

        event_id = str(ULID())

        # The span is opened before the ids are read so that each event records its
        # own span. That is what gives Cloud Trace a node per recorded event.
        with self._tracer.start_as_current_span(f"append_event:{event_type.value}") as span:
            trace_id, span_id = current_trace_ids()

            event = Event(
                event_id=event_id,
                timestamp=datetime.now(timezone.utc),
                actor=actor,
                event_type=event_type,
                caused_by=caused_by,
                payload=payload,
                labels=labels or {},
                trace_id=trace_id,
                span_id=span_id,
                case_id=case_id,
            )

            self._backend.put(self._collection, event_id, event.to_firestore_dict())

            span.set_attribute("blackbox.event_id", event_id)
            span.set_attribute("blackbox.case_id", case_id)
            span.set_attribute("blackbox.event_type", event_type.value)
            span.set_attribute("blackbox.actor", actor)
            if caused_by:
                span.set_attribute("blackbox.caused_by", caused_by)

        return event_id

    def get_event(self, event_id: str) -> Optional[Event]:
        """Retrieve a single event by id, or None if it does not exist."""
        data = self._backend.get(self._collection, event_id)
        if data is None:
            return None
        return Event.from_firestore_dict(data)

    def list_events(self, case_id: str, limit: Optional[int] = None) -> List[Event]:
        """List a case's events in creation order (oldest first)."""
        rows = self._backend.query(
            self._collection,
            filters=[("case_id", "==", case_id)],
            order_by="event_id",
            limit=limit,
        )
        return [Event.from_firestore_dict(row) for row in rows]

    def list_events_by_type(self, case_id: str, event_type: EventType) -> List[Event]:
        """List a case's events of one type, in creation order."""
        rows = self._backend.query(
            self._collection,
            filters=[("case_id", "==", case_id), ("event_type", "==", event_type.value)],
            order_by="event_id",
        )
        return [Event.from_firestore_dict(row) for row in rows]

    def scan_events_by_type(
        self, event_type: EventType, limit: Optional[int] = None
    ) -> List[Event]:
        """List events of one type across every case, in creation order.

        The heartbeat needs this: to find suspended work it has to look at every
        SUSPEND event in the system, not just one case's. This is a read, and it
        is the only cross-case query in the store.

        Note that this is the replay reader's kind of query, not an agent's.
        Agents read the Wiki. Nothing on the normal workflow path calls this.
        """
        rows = self._backend.query(
            self._collection,
            filters=[("event_type", "==", event_type.value)],
            order_by="event_id",
            limit=limit,
        )
        return [Event.from_firestore_dict(row) for row in rows]

    def get_causal_chain(self, event_id: str) -> List[Event]:
        """Walk ``caused_by`` from an event back to its root.

        Returns the chain root-first. Guards against a cycle, which should be
        impossible given ULID ordering but would otherwise hang the caller.
        """
        chain: List[Event] = []
        seen = set()
        current_id: Optional[str] = event_id

        while current_id and current_id not in seen:
            seen.add(current_id)
            event = self.get_event(current_id)
            if event is None:
                break
            chain.append(event)
            current_id = event.caused_by

        chain.reverse()
        return chain

    def get_children(self, event_id: str) -> List[Event]:
        """Return every event directly caused by the given event."""
        rows = self._backend.query(
            self._collection,
            filters=[("caused_by", "==", event_id)],
            order_by="event_id",
        )
        return [Event.from_firestore_dict(row) for row in rows]
