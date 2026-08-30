"""Tests for BLACKBOX Flight Recorder"""

import pytest
from datetime import datetime, timezone
from blackbox.schema import (
    Event, EventType, ThoughtPayload, ToolCallPayload,
    PolicyCheckPayload, PAYLOAD_SCHEMAS
)
from blackbox.event_store import EventStore
from blackbox.fold import fold_case, fold_events


def test_event_schema_creation():
    """Test that we can create a valid event"""
    event = Event(
        event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        timestamp=datetime.now(timezone.utc),
        actor="test_agent",
        event_type=EventType.THOUGHT,
        payload={
            "reasoning": "Analyzing complaint",
            "decision": "Route to assessment",
            "confidence": 0.85,
            "context_summary": "Initial review"
        },
        trace_id="trace123",
        span_id="span456",
        case_id="CASE-001"
    )
    assert event.case_id == "CASE-001"
    assert event.event_type == EventType.THOUGHT
    assert event.actor == "test_agent"


def test_payload_validation():
    """Test that payloads are validated against their schemas"""
    # Valid THOUGHT payload
    valid_payload = {
        "reasoning": "Analyzing complaint",
        "decision": "Route to assessment",
        "confidence": 0.85,
        "context_summary": "Initial review"
    }
    
    event = Event(
        event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        timestamp=datetime.now(timezone.utc),
        actor="test_agent",
        event_type=EventType.THOUGHT,
        payload=valid_payload,
        trace_id="trace123",
        span_id="span456",
        case_id="CASE-001"
    )
    
    # Should not raise
    event.validate_payload()


def test_invalid_payload_rejected():
    """Test that invalid payloads are rejected"""
    # Missing required field 'reasoning'
    invalid_payload = {
        "decision": "Route to assessment",
        "confidence": 0.85,
        "context_summary": "Initial review"
    }
    
    event = Event(
        event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        timestamp=datetime.now(timezone.utc),
        actor="test_agent",
        event_type=EventType.THOUGHT,
        payload=invalid_payload,
        trace_id="trace123",
        span_id="span456",
        case_id="CASE-001"
    )
    
    with pytest.raises(ValueError, match="Payload validation failed"):
        event.validate_payload()


def test_event_serialization():
    """Test that events can be serialized to/from Firestore format"""
    event = Event(
        event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        timestamp=datetime.now(timezone.utc),
        actor="test_agent",
        event_type=EventType.THOUGHT,
        payload={
            "reasoning": "Analyzing complaint",
            "decision": "Route to assessment",
            "confidence": 0.85,
            "context_summary": "Initial review"
        },
        trace_id="trace123",
        span_id="span456",
        case_id="CASE-001"
    )
    
    # Serialize to Firestore format
    firestore_dict = event.to_firestore_dict()
    assert "event_id" in firestore_dict
    assert firestore_dict["case_id"] == "CASE-001"
    
    # Deserialize back
    restored_event = Event.from_firestore_dict(firestore_dict)
    assert restored_event.event_id == event.event_id
    assert restored_event.case_id == event.case_id


def test_causal_chain():
    """Test that events can form a causal chain"""
    # Root event
    root = Event(
        event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        timestamp=datetime.now(timezone.utc),
        actor="test_agent",
        event_type=EventType.THOUGHT,
        payload={
            "reasoning": "Initial analysis",
            "decision": "Need more info",
            "confidence": 0.7,
            "context_summary": "Start"
        },
        trace_id="trace123",
        span_id="span456",
        case_id="CASE-001",
        caused_by=None  # Root event
    )
    
    # Child event
    child = Event(
        event_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        timestamp=datetime.now(timezone.utc),
        actor="test_agent",
        event_type=EventType.TOOL_CALL,
        payload={
            "tool_name": "search_complaints",
            "parameters": {"query": "customer_id:123"},
            "intended_outcome": "Find related complaints"
        },
        trace_id="trace123",
        span_id="span457",
        case_id="CASE-001",
        caused_by=root.event_id  # Points to parent
    )
    
    assert child.caused_by == root.event_id


def test_all_event_types_have_schemas():
    """Test that every event type has a corresponding payload schema"""
    for event_type in EventType:
        assert event_type in PAYLOAD_SCHEMAS, \
            f"Event type {event_type} has no payload schema"


def test_tool_call_payload():
    """Test TOOL_CALL payload structure"""
    payload = ToolCallPayload(
        tool_name="search_complaints",
        parameters={"query": "customer_id:123"},
        intended_outcome="Find related complaints"
    )
    assert payload.tool_name == "search_complaints"
    assert payload.parameters["query"] == "customer_id:123"


def test_policy_check_payload():
    """Test POLICY_CHECK payload structure"""
    payload = PolicyCheckPayload(
        policy_id="POL-001",
        check_type="data_transfer",
        input_data={"source": "EU", "destination": "US"},
        decision="block",
        reasoning="Cross-border transfer requires explicit consent"
    )
    assert payload.decision == "block"
    assert payload.policy_id == "POL-001"
