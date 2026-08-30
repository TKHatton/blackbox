"""Event schema for BLACKBOX Flight Recorder"""

from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator
from ulid import ULID


class EventType(str, Enum):
    """Types of events in the flight recorder"""
    THOUGHT = "THOUGHT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    MEMORY_READ = "MEMORY_READ"
    MEMORY_WRITE = "MEMORY_WRITE"
    POLICY_CHECK = "POLICY_CHECK"
    MESSAGE_SENT = "MESSAGE_SENT"
    SUSPEND = "SUSPEND"
    RESUME = "RESUME"
    ESCALATE = "ESCALATE"
    # Phase 5. The spec's list is a minimum, and a retraction that had to be
    # recorded as a MEMORY_WRITE would be indistinguishable from an ordinary
    # rewrite in the audit trail. The whole point is that the Diary still shows a
    # retraction happened after the content is gone, so it gets its own type.
    RETRACT = "RETRACT"
    INVALIDATE = "INVALIDATE"


# Payload schemas for each event type
class ThoughtPayload(BaseModel):
    """Payload for THOUGHT events"""
    reasoning: str = Field(..., description="Gemini's stated rationale")
    decision: str = Field(..., description="What was decided")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score 0.0-1.0")
    context_summary: str = Field(..., description="What the agent was considering")


class ToolCallPayload(BaseModel):
    """Payload for TOOL_CALL events"""
    tool_name: str = Field(..., description="Name of the tool being called")
    parameters: Dict[str, Any] = Field(..., description="Input parameters")
    intended_outcome: str = Field(..., description="What the agent expected")


class ToolResultPayload(BaseModel):
    """Payload for TOOL_RESULT events"""
    tool_name: str = Field(..., description="Name of the tool that was called")
    success: bool = Field(..., description="Whether the tool call succeeded")
    result: Any = Field(..., description="Result data from the tool")
    error_message: Optional[str] = Field(None, description="Error message if failed")


class MemoryReadPayload(BaseModel):
    """Payload for MEMORY_READ events"""
    memory_key: str = Field(..., description="Key of the memory being read")
    content: Any = Field(..., description="Content retrieved from memory")
    reason: str = Field(..., description="Why this memory was read")


class MemoryWritePayload(BaseModel):
    """Payload for MEMORY_WRITE events"""
    memory_key: str = Field(..., description="Key of the memory being written")
    content: Any = Field(..., description="Content being written to memory")
    reason: str = Field(..., description="Why this memory was written")


class PolicyCheckPayload(BaseModel):
    """Payload for POLICY_CHECK events"""
    policy_id: str = Field(..., description="Which policy was checked")
    check_type: str = Field(..., description="Type of check (e.g., data_transfer, approval_threshold)")
    input_data: Dict[str, Any] = Field(..., description="Data being checked")
    decision: Literal["allow", "block", "escalate"] = Field(..., description="Policy decision")
    reasoning: str = Field(..., description="Why this decision was made")


class MessageSentPayload(BaseModel):
    """Payload for MESSAGE_SENT events"""
    recipient: str = Field(..., description="Who received the message")
    channel: str = Field(..., description="Channel used (email, slack, etc.)")
    content: str = Field(..., description="Message content")
    purpose: str = Field(..., description="Why this message was sent")


class SuspendPayload(BaseModel):
    """Payload for SUSPEND events"""
    reason: str = Field(..., description="Why the agent is suspending")
    wake_condition: Dict[str, Any] = Field(..., description="Condition for waking up")
    state_snapshot: Dict[str, Any] = Field(..., description="State at suspension point")


class ResumePayload(BaseModel):
    """Payload for RESUME events"""
    reason: str = Field(..., description="Why the agent is resuming")
    wake_trigger: Dict[str, Any] = Field(..., description="What triggered the wake")
    state_restored: bool = Field(..., description="Whether state was successfully restored")


class EscalatePayload(BaseModel):
    """Payload for ESCALATE events"""
    reason: str = Field(..., description="Why escalation is needed")
    escalation_type: str = Field(..., description="Type of escalation (human, supervisor, etc.)")
    context: Dict[str, Any] = Field(..., description="Context for the escalation")
    urgency: Literal["low", "medium", "high", "critical"] = Field(..., description="Urgency level")


class RetractPayload(BaseModel):
    """Payload for RETRACT events.

    Records that a fact was withdrawn, and enough about it to prove the
    withdrawal happened, without restating the content being withdrawn. Storing
    the retracted values here would defeat the purpose: the Diary is append-only,
    so anything written into it cannot later be taken out.
    """
    subject: str = Field(..., description="Who or what the retraction is about")
    retracted_fields: list = Field(..., description="Which fields were withdrawn")
    reason: str = Field(..., description="Why, for example a right to erasure request")
    requested_by: str = Field(..., description="Who asked")
    scope: Dict[str, Any] = Field(
        default_factory=dict, description="What the retraction reaches: pages, events"
    )


class InvalidatePayload(BaseModel):
    """Payload for INVALIDATE events.

    One entry per derived page the cascade reached. Recording every invalidation
    separately is what makes the cascade auditable: you can see how far it
    travelled and by which edge each page was reached.
    """
    page_id: str = Field(..., description="The derived page being invalidated")
    caused_by_retraction: str = Field(..., description="Which retraction reached it")
    depth: int = Field(..., description="How many edges from the retracted fact")
    reached_via: str = Field(..., description="The source that pulled this page in")
    regenerated: bool = Field(..., description="Whether the page was rebuilt")
    reason: str = Field(..., description="Why this page was affected")


# Map event types to their payload schemas
PAYLOAD_SCHEMAS = {
    EventType.THOUGHT: ThoughtPayload,
    EventType.TOOL_CALL: ToolCallPayload,
    EventType.TOOL_RESULT: ToolResultPayload,
    EventType.MEMORY_READ: MemoryReadPayload,
    EventType.MEMORY_WRITE: MemoryWritePayload,
    EventType.POLICY_CHECK: PolicyCheckPayload,
    EventType.MESSAGE_SENT: MessageSentPayload,
    EventType.SUSPEND: SuspendPayload,
    EventType.RESUME: ResumePayload,
    EventType.ESCALATE: EscalatePayload,
    EventType.RETRACT: RetractPayload,
    EventType.INVALIDATE: InvalidatePayload,
}


class Event(BaseModel):
    """
    Core event structure for BLACKBOX Flight Recorder.
    
    Every event represents a single atomic action or decision in the system.
    Events are append-only and form a causal tree via caused_by relationships.
    """
    
    model_config = {"frozen": True}  # Make immutable
    
    event_id: str = Field(..., description="ULID, sortable by creation time")
    timestamp: datetime = Field(..., description="ISO 8601 timestamp with timezone")
    actor: str = Field(..., description="Which agent or system produced this event")
    event_type: EventType = Field(..., description="Type of event")
    caused_by: Optional[str] = Field(None, description="Parent event_id (null for root events)")
    payload: Dict[str, Any] = Field(..., description="Structured payload, shape varies by event_type")
    labels: Dict[str, Any] = Field(default_factory=dict, description="Data labels for Invisible Ink (Phase 4)")
    trace_id: str = Field(..., description="OpenTelemetry trace ID")
    span_id: str = Field(..., description="OpenTelemetry span ID")
    case_id: str = Field(..., description="Case ID (partition key)")
    
    @field_validator('event_id')
    @classmethod
    def validate_event_id(cls, v):
        """Validate that event_id is a valid ULID"""
        try:
            ULID.from_str(v)
        except ValueError:
            raise ValueError(f"Invalid ULID: {v}")
        return v
    
    @field_validator('caused_by')
    @classmethod
    def validate_caused_by(cls, v):
        """Validate that caused_by is a valid ULID if present"""
        if v is not None:
            try:
                ULID.from_str(v)
            except ValueError:
                raise ValueError(f"Invalid ULID in caused_by: {v}")
        return v
    
    def validate_payload(self) -> bool:
        """
        Validate that payload matches the schema for this event_type.
        
        Returns True if valid, raises ValueError if invalid.
        """
        schema_class = PAYLOAD_SCHEMAS.get(self.event_type)
        if schema_class is None:
            raise ValueError(f"No payload schema defined for event_type: {self.event_type}")
        
        try:
            schema_class(**self.payload)
            return True
        except Exception as e:
            raise ValueError(f"Payload validation failed for {self.event_type}: {e}")
    
    def to_firestore_dict(self) -> Dict[str, Any]:
        """Convert event to Firestore-compatible dictionary"""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "actor": self.actor,
            "event_type": self.event_type.value,
            "caused_by": self.caused_by,
            "payload": self.payload,
            "labels": self.labels,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "case_id": self.case_id,
        }
    
    @classmethod
    def from_firestore_dict(cls, data: Dict[str, Any]) -> "Event":
        """Create Event from Firestore dictionary"""
        # Convert timestamp string back to datetime
        if isinstance(data.get("timestamp"), str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        
        # Convert event_type string back to enum
        if isinstance(data.get("event_type"), str):
            data["event_type"] = EventType(data["event_type"])
        
        return cls(**data)
