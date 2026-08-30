"""The inbound path. Complaints arrive; nobody presses a button.

Two pieces:

- ``poll_channels`` stands in for the email and web-form pollers. Cloud Scheduler
  calls it on a timer. It finds complaints that have arrived and publishes them
  to Pub/Sub. It does not process them.
- ``publish_complaint`` puts one complaint on the topic.

The split matters. The poller's only job is noticing. The agent runs because a
message landed on a topic, not because the poller called it, which is what lets
Phase 3 add five more agents subscribing to their own topics without rewriting
this file.

Redelivery is expected. Pub/Sub guarantees at-least-once, so the handler checks
whether a case already has events before running the agent again.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from .config import get_settings
from .event_store import EventStore
from .stubs import data

logger = logging.getLogger(__name__)


class ComplaintPublisher:
    """Publishes arriving complaints to the Pub/Sub topic."""

    def __init__(self, project_id: str, topic: str, publisher: Optional[Any] = None):
        self.project_id = project_id
        self.topic = topic
        self._publisher = publisher

    @property
    def publisher(self):
        """Lazy Pub/Sub client so importing this module needs no credentials."""
        if self._publisher is None:
            from google.cloud import pubsub_v1

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    @property
    def topic_path(self) -> str:
        return f"projects/{self.project_id}/topics/{self.topic}"

    def publish(self, complaint: Dict[str, Any]) -> str:
        """Publish one complaint. Returns the Pub/Sub message id."""
        payload = json.dumps(complaint).encode("utf-8")
        future = self.publisher.publish(
            self.topic_path,
            payload,
            complaint_ref=complaint["complaint_ref"],
        )
        return future.result(timeout=30)


def case_already_open(store: EventStore, case_id: str) -> bool:
    """True if this case already has events.

    The idempotency check. A redelivered Pub/Sub message must not open a second
    case or append a duplicate run of the agent's reasoning.
    """
    return bool(store.list_events(case_id, limit=1))


def pending_complaints(store: EventStore) -> List[Dict[str, Any]]:
    """Complaints that have arrived and have no case open yet."""
    from .agents.intake_service import case_id_for

    pending = []
    for complaint in data.INBOUND_COMPLAINTS:
        case_id = case_id_for(complaint["complaint_ref"])
        if not case_already_open(store, case_id):
            pending.append(dict(complaint))
    return pending


def poll_channels(
    store: Optional[EventStore] = None,
    publisher: Optional[ComplaintPublisher] = None,
) -> Dict[str, Any]:
    """Check the inbound channels and publish anything new.

    Called by Cloud Scheduler on a timer. Returns what it found so the scheduler
    log shows whether a run did anything.
    """
    settings = get_settings()
    store = store or EventStore(project_id=settings.project_id)
    publisher = publisher or ComplaintPublisher(
        project_id=settings.require_project_id(), topic=settings.complaints_topic
    )

    published = []
    for complaint in pending_complaints(store):
        try:
            message_id = publisher.publish(complaint)
            published.append(
                {"complaint_ref": complaint["complaint_ref"], "message_id": message_id}
            )
        except Exception as exc:
            logger.exception("Failed to publish %s", complaint["complaint_ref"])
            published.append(
                {"complaint_ref": complaint["complaint_ref"], "error": str(exc)}
            )

    return {"checked": len(data.INBOUND_COMPLAINTS), "published": published}


def decode_push_message(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the complaint out of a Pub/Sub push envelope.

    Raises ValueError on a malformed envelope so the caller can answer 400 and
    stop Pub/Sub retrying a message that will never parse.
    """
    import base64

    message = envelope.get("message")
    if not isinstance(message, dict):
        raise ValueError("Push envelope has no message object")

    raw = message.get("data")
    if not raw:
        raise ValueError("Push message carries no data")

    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        complaint = json.loads(decoded)
    except Exception as exc:
        raise ValueError(f"Push message data is not valid JSON: {exc}") from exc

    if "complaint_ref" not in complaint:
        raise ValueError("Complaint has no complaint_ref")

    return complaint
