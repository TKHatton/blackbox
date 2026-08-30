"""Suspend and resume: how the fleet waits without staying resident.

The rule this module exists to enforce: **a wake condition is an event, never a
variable.** If an agent's reason for waking lived in process memory, a Cloud Run
instance recycling would lose every pending case in the fleet, silently. Because
the condition is in the SUSPEND payload in the Diary, a fresh instance with no
memory of anything can find all outstanding work by reading the log.

The shape of a wait:

1. An agent decides it cannot continue yet. It writes a SUSPEND event carrying a
   ``WakeCondition`` describing what would have to become true, and stops. No
   process, thread, or coroutine remains.
2. Something later asks whether that condition is now met. Two things do the
   asking: the Cloud Scheduler heartbeat, for conditions about time or about an
   external system, and a Pub/Sub message, for conditions about an event that
   arrives on its own such as an approval.
3. When the condition is met, a RESUME event is written naming the SUSPEND it
   answers, context is rebuilt from the Wiki and the fold, and the agent carries on.

A suspension is open when it has a SUSPEND with no RESUME pointing back at it.
That is derived from the log rather than tracked in a table, so there is no second
source of truth to drift.
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .event_store import EventStore
from .schema import Event, EventType


class WakeConditionType(str, Enum):
    """The kinds of waiting this workflow actually does.

    These are not four names for one timer. A batch job is answered by asking an
    external system; an approval arrives on its own; a deadline is a clock; an
    appeal window is a clock that can be cut short by a customer replying. They
    resume through different routes, which is why they are distinguished.
    """

    #: CommsVault was asked for records and returned a job id. Poll it.
    BATCH_JOB_READY = "batch_job_ready"
    #: A human approval gate. Arrives by Pub/Sub, cannot be polled into existence.
    APPROVAL_RECEIVED = "approval_received"
    #: A statutory clock. Wake when the deadline is within reach.
    DEADLINE_APPROACHING = "deadline_approaching"
    #: The 30 day appeal window closed with no reply, so the case may close.
    APPEAL_WINDOW_ELAPSED = "appeal_window_elapsed"
    #: The customer replied during the appeal window. Wakes the case early.
    CUSTOMER_REPLIED = "customer_replied"


class WakeCondition(BaseModel):
    """What would have to become true for a suspended agent to continue.

    Stored inside the SUSPEND event's payload. Everything needed to evaluate the
    condition and to route the resumption is here, because whatever evaluates it
    later will have none of the context the suspending agent had.
    """

    type: WakeConditionType = Field(..., description="Which kind of wait this is")
    resume_agent: str = Field(..., description="Which agent should pick the work back up")
    description: str = Field(..., description="Plain language, for the audit trail")
    earliest_wake_at: Optional[datetime] = Field(
        None, description="Do not even check before this time. None means check every heartbeat."
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="What the evaluator needs: a job id, an approval reference, a deadline.",
    )

    def to_payload(self) -> Dict[str, Any]:
        """Serialize for storage in an event payload."""
        return {
            "type": self.type.value,
            "resume_agent": self.resume_agent,
            "description": self.description,
            "earliest_wake_at": (
                self.earliest_wake_at.isoformat() if self.earliest_wake_at else None
            ),
            "parameters": self.parameters,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "WakeCondition":
        """Rebuild from an event payload."""
        earliest = payload.get("earliest_wake_at")
        return cls(
            type=WakeConditionType(payload["type"]),
            resume_agent=payload["resume_agent"],
            description=payload.get("description", ""),
            earliest_wake_at=datetime.fromisoformat(earliest) if earliest else None,
            parameters=payload.get("parameters", {}),
        )

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """True if enough time has passed to be worth evaluating.

        This is not "the condition is met". It only says the check is not
        pointless yet, which is what keeps the heartbeat from hammering
        CommsVault about a job that cannot possibly be ready.
        """
        if self.earliest_wake_at is None:
            return True
        return (now or datetime.now(timezone.utc)) >= self.earliest_wake_at


class OpenSuspension(BaseModel):
    """A SUSPEND with no RESUME answering it yet."""

    model_config = {"arbitrary_types_allowed": True}

    case_id: str
    suspend_event_id: str
    suspended_at: datetime
    actor: str
    reason: str
    condition: WakeCondition
    state_snapshot: Dict[str, Any] = Field(default_factory=dict)


def build_suspend_payload(
    reason: str, condition: WakeCondition, state_snapshot: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Assemble a SUSPEND payload.

    The state snapshot is a convenience for the resuming agent, not its source of
    truth. Context is rebuilt from the Wiki and the fold on resume, so a snapshot
    that has gone stale cannot mislead anyone.
    """
    return {
        "reason": reason,
        "wake_condition": condition.to_payload(),
        "state_snapshot": state_snapshot or {},
    }


def find_open_suspensions(store: EventStore) -> List[OpenSuspension]:
    """Every suspension in the fleet that has not been resumed.

    Derived from the log: read all SUSPEND events, read all RESUME events, and
    subtract. A restart of every instance in the fleet changes nothing about the
    answer, which is the property that makes this autonomy rather than a process
    sitting in a loop.
    """
    suspends = store.scan_events_by_type(EventType.SUSPEND)
    resumes = store.scan_events_by_type(EventType.RESUME)
    answered = {r.caused_by for r in resumes if r.caused_by}

    open_suspensions: List[OpenSuspension] = []
    for event in suspends:
        if event.event_id in answered:
            continue
        try:
            condition = WakeCondition.from_payload(event.payload["wake_condition"])
        except (KeyError, ValueError):
            # A SUSPEND whose condition cannot be parsed is stuck work, and
            # skipping it silently would hide that. It is surfaced instead.
            continue
        open_suspensions.append(
            OpenSuspension(
                case_id=event.case_id,
                suspend_event_id=event.event_id,
                suspended_at=event.timestamp,
                actor=event.actor,
                reason=event.payload.get("reason", ""),
                condition=condition,
                state_snapshot=event.payload.get("state_snapshot", {}),
            )
        )
    return open_suspensions


def find_unparseable_suspensions(store: EventStore) -> List[Event]:
    """SUSPEND events whose wake condition cannot be read.

    These are cases that will never wake on their own. Reporting them is the
    difference between a stuck case and a lost one.
    """
    suspends = store.scan_events_by_type(EventType.SUSPEND)
    resumes = store.scan_events_by_type(EventType.RESUME)
    answered = {r.caused_by for r in resumes if r.caused_by}

    broken = []
    for event in suspends:
        if event.event_id in answered:
            continue
        try:
            WakeCondition.from_payload(event.payload["wake_condition"])
        except (KeyError, ValueError):
            broken.append(event)
    return broken


def deadline_condition(
    resume_agent: str,
    deadline: datetime,
    lead_time: timedelta,
    description: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> WakeCondition:
    """A wait on a statutory clock, waking ``lead_time`` before the deadline."""
    return WakeCondition(
        type=WakeConditionType.DEADLINE_APPROACHING,
        resume_agent=resume_agent,
        description=description,
        earliest_wake_at=deadline - lead_time,
        parameters={**(parameters or {}), "deadline": deadline.isoformat()},
    )


def batch_job_condition(
    resume_agent: str, job_id: str, ready_at: datetime, description: str
) -> WakeCondition:
    """A wait on an external batch job, such as a CommsVault retrieval."""
    return WakeCondition(
        type=WakeConditionType.BATCH_JOB_READY,
        resume_agent=resume_agent,
        description=description,
        earliest_wake_at=ready_at,
        parameters={"job_id": job_id},
    )


def approval_condition(
    resume_agent: str, gate: str, request_id: str, description: str
) -> WakeCondition:
    """A wait on a human approval.

    No ``earliest_wake_at``: an approval cannot be hurried by waiting, and it
    arrives by Pub/Sub rather than being discovered by polling. The heartbeat
    still sees it, so a gate that has been sitting too long can be reported.
    """
    return WakeCondition(
        type=WakeConditionType.APPROVAL_RECEIVED,
        resume_agent=resume_agent,
        description=description,
        earliest_wake_at=None,
        parameters={"gate": gate, "request_id": request_id},
    )


def appeal_window_condition(
    resume_agent: str, window_closes_at: datetime, description: str
) -> WakeCondition:
    """A wait for the appeal window to close, cut short if the customer replies."""
    return WakeCondition(
        type=WakeConditionType.APPEAL_WINDOW_ELAPSED,
        resume_agent=resume_agent,
        description=description,
        earliest_wake_at=window_closes_at,
        parameters={"window_closes_at": window_closes_at.isoformat()},
    )
