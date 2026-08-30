"""The heartbeat: how suspended work wakes up.

Cloud Scheduler calls this on a timer. Each beat does three things:

1. Reads every open suspension out of the Diary. Not out of memory: an instance
   that has been alive for four seconds finds exactly the same work as one that
   has been up for a week.
2. Asks, per suspension, whether its wake condition is now met. The answer is
   recorded as a POLICY_CHECK with reasoning, so "why did this case wake on
   Thursday and not Wednesday" is answerable later.
3. Resumes the ones that are ready, and lets the Compliance Officer look at the
   ones that are not.

**This is not a polling loop.** The distinction matters and it is easy to lose.
A polling loop asks "is there work?" on a fixed cadence and does the work itself.
What happens here is that a timer gives suspended agents an opportunity to
evaluate conditions they themselves defined, and each decides whether to wake.
The heartbeat has no opinion about any case; it cannot start work that no agent
asked to have started. Between beats, nothing is resident: no thread, no
coroutine, no open connection.

The consequence, which is the property Phase 3 is judged on: close the laptop,
come back Thursday, and a case that suspended on Monday has advanced.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .agents.fleet_service import advance_case, resume_case
from .config import get_settings
from .event_store import EventStore
from .fold import fold_events
from .recorder import Recorder
from .schema import EventType
from .stubs.systems import SourceSystemError, SourceSystems
from .wake import (
    OpenSuspension,
    WakeConditionType,
    find_open_suspensions,
    find_unparseable_suspensions,
)
from .wiki_store import WikiStore

logger = logging.getLogger(__name__)

# How close to a statutory deadline a case has to be before the Compliance
# Officer is asked to look at it. Eight weeks is the final response deadline, so
# two weeks out leaves room to act.
COMPLIANCE_REVIEW_LEAD_DAYS = 14


class WakeDecision:
    """Whether one suspension should wake, and why."""

    def __init__(self, suspension: OpenSuspension, wake: bool, reasoning: str, trigger: Dict[str, Any]):
        self.suspension = suspension
        self.wake = wake
        self.reasoning = reasoning
        self.trigger = trigger


def evaluate_wake_condition(
    suspension: OpenSuspension,
    systems: SourceSystems,
    now: Optional[datetime] = None,
) -> WakeDecision:
    """Decide whether a suspension's condition is met.

    These are the deterministic conditions: a clock has passed, or an external
    system has an answer. They are decided by rule rather than by Gemini, because
    "has this job finished" has a correct answer and asking a model to guess at
    it would be worse, not more intelligent. Judgment enters afterwards, when the
    woken agent decides what to do about it.
    """
    now = now or datetime.now(timezone.utc)
    condition = suspension.condition

    if not condition.is_due(now):
        return WakeDecision(
            suspension,
            False,
            f"Not due yet. Nothing can change before "
            f"{condition.earliest_wake_at.isoformat()}.",
            {},
        )

    if condition.type == WakeConditionType.BATCH_JOB_READY:
        job_id = condition.parameters.get("job_id", "")
        try:
            status = systems.commsvault.poll(job_id, as_of=now)
        except SourceSystemError as exc:
            return WakeDecision(
                suspension,
                False,
                f"CommsVault could not be asked about job {job_id}: {exc}. Leaving "
                f"the case suspended rather than resuming without the records.",
                {},
            )
        if status.get("status") != "READY":
            return WakeDecision(
                suspension,
                False,
                f"CommsVault job {job_id} is still {status.get('status')}.",
                {},
            )
        return WakeDecision(
            suspension,
            True,
            f"CommsVault job {job_id} has returned its records.",
            {
                "source": "commsvault",
                "job_id": job_id,
                "records": status.get("records", []),
                "summary": (
                    f"The archived records you requested from CommsVault are now "
                    f"available: {status.get('records')}"
                ),
            },
        )

    if condition.type == WakeConditionType.APPROVAL_RECEIVED:
        # An approval cannot be discovered by asking. It arrives on the approvals
        # topic and wakes the case that way. The heartbeat only notices when one
        # has been outstanding long enough to be worth a person knowing about.
        waiting_days = (now - suspension.suspended_at).total_seconds() / 86400
        return WakeDecision(
            suspension,
            False,
            f"Waiting on human approval for gate "
            f"{condition.parameters.get('gate')}, outstanding {waiting_days:.1f} days. "
            f"Approvals arrive by message and cannot be polled into existence.",
            {},
        )

    if condition.type in (
        WakeConditionType.DEADLINE_APPROACHING,
        WakeConditionType.APPEAL_WINDOW_ELAPSED,
    ):
        return WakeDecision(
            suspension,
            True,
            f"The time this case was waiting for has passed: {condition.description}.",
            {
                "source": "clock",
                "summary": f"{condition.description}. The time has now passed.",
                "elapsed_at": now.isoformat(),
            },
        )

    if condition.type == WakeConditionType.CUSTOMER_REPLIED:
        return WakeDecision(
            suspension,
            False,
            "Waiting for the customer to reply. That arrives by message, not by "
            "the passage of time.",
            {},
        )

    return WakeDecision(
        suspension, False, f"No evaluator for condition type {condition.type}.", {}
    )


def cases_needing_compliance_review(
    store: EventStore, wiki_store: WikiStore, now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """Open cases whose statutory clocks are close enough to need a look.

    Reads the Wiki, not the Diary. The Compliance Officer's job is a judgment
    about current state, and current state is what the Wiki holds.
    """
    now = now or datetime.now(timezone.utc)
    due = []

    for page in wiki_store.list_pages_by_subject_type("case"):
        content = page.content
        if content.get("status") in ("closed",):
            continue

        deadlines = content.get("deadlines", {})
        final_due = deadlines.get("final_response_due")
        if not final_due:
            continue
        try:
            deadline = datetime.fromisoformat(final_due)
        except (TypeError, ValueError):
            continue

        days_left = (deadline - now).total_seconds() / 86400
        already_held = bool(content.get("holding_sent_at")) or content.get(
            "holding_letter_required"
        )
        final_sent = bool(content.get("final_response_sent_at"))

        if final_sent:
            continue
        if days_left <= COMPLIANCE_REVIEW_LEAD_DAYS and not already_held:
            due.append(
                {
                    "case_id": page.subject,
                    "days_to_final_response": round(days_left, 2),
                    "reason": "Final response deadline is approaching and no holding "
                    "letter has been sent.",
                }
            )
    return due


async def run_heartbeat(
    store: EventStore,
    wiki_store: WikiStore,
    systems: SourceSystems,
    model: Optional[Any] = None,
    now: Optional[datetime] = None,
    resume_limit: int = 10,
    compliance_limit: int = 5,
) -> Dict[str, Any]:
    """One beat. Evaluate every open suspension, resume what is ready.

    Args:
        store: The Diary.
        wiki_store: Derived memory.
        systems: The stub source systems, asked about batch jobs.
        model: Override the Gemini model. Used by tests.
        now: Evaluate as of this time rather than the wall clock. This is what
            lets a demonstration compress a 30 day appeal window into a visible
            span without faking the mechanism.
        resume_limit: Cap on resumptions per beat, so one beat cannot run away.
        compliance_limit: Cap on compliance reviews per beat.

    Returns:
        What the beat found and did.
    """
    now = now or datetime.now(timezone.utc)
    suspensions = find_open_suspensions(store)
    broken = find_unparseable_suspensions(store)

    evaluated: List[Dict[str, Any]] = []
    resumed: List[Dict[str, Any]] = []

    for suspension in suspensions:
        decision = evaluate_wake_condition(suspension, systems, now=now)

        # Why a case did or did not wake is part of the record, not a log line.
        recorder = Recorder(
            case_id=suspension.case_id, actor="heartbeat", store=store
        )
        recorder.set_cause(suspension.suspend_event_id)
        recorder.policy_check(
            policy_id="wake_condition_evaluation",
            check_type="wake_condition",
            input_data={
                "condition_type": suspension.condition.type.value,
                "evaluated_at": now.isoformat(),
                "suspend_event_id": suspension.suspend_event_id,
            },
            decision="allow" if decision.wake else "block",
            reasoning=decision.reasoning,
        )

        evaluated.append(
            {
                "case_id": suspension.case_id,
                "condition": suspension.condition.type.value,
                "wake": decision.wake,
                "reasoning": decision.reasoning,
            }
        )

        if decision.wake and len(resumed) < resume_limit:
            try:
                outcome = await resume_case(
                    suspension=suspension,
                    trigger=decision.trigger,
                    store=store,
                    wiki_store=wiki_store,
                    systems=systems,
                    model=model,
                )
                resumed.append(outcome)
            except Exception as exc:
                # A failure to resume must not take the whole beat down: the
                # other suspended cases in the fleet still need evaluating.
                logger.exception("Resume failed for %s", suspension.case_id)
                resumed.append(
                    {"case_id": suspension.case_id, "resumed": False, "error": str(exc)}
                )

    reviews: List[Dict[str, Any]] = []
    for item in cases_needing_compliance_review(store, wiki_store, now=now)[:compliance_limit]:
        try:
            outcome = await advance_case(
                case_id=item["case_id"],
                store=store,
                wiki_store=wiki_store,
                systems=systems,
                model=model,
                trigger=f"compliance review: {item['reason']}",
            )
            outcome["why_reviewed"] = item["reason"]
            reviews.append(outcome)
        except Exception as exc:
            logger.exception("Compliance review failed for %s", item["case_id"])
            reviews.append({"case_id": item["case_id"], "error": str(exc)})

    return {
        "beat_at": now.isoformat(),
        "open_suspensions": len(suspensions),
        "evaluated": evaluated,
        "resumed": resumed,
        "compliance_reviews": reviews,
        "unparseable_suspensions": [
            {"case_id": e.case_id, "event_id": e.event_id} for e in broken
        ],
    }


def find_suspension_for_approval(
    store: EventStore, case_id: str, request_id: Optional[str] = None
) -> Optional[OpenSuspension]:
    """Find the suspension an arriving approval answers."""
    for suspension in find_open_suspensions(store):
        if suspension.case_id != case_id:
            continue
        if suspension.condition.type != WakeConditionType.APPROVAL_RECEIVED:
            continue
        if request_id and suspension.condition.parameters.get("request_id") != request_id:
            continue
        return suspension
    return None


def find_suspension_for_customer_reply(
    store: EventStore, case_id: str
) -> Optional[OpenSuspension]:
    """Find a sleeping appeal window that a customer reply should cut short."""
    for suspension in find_open_suspensions(store):
        if suspension.case_id != case_id:
            continue
        if suspension.condition.type in (
            WakeConditionType.APPEAL_WINDOW_ELAPSED,
            WakeConditionType.CUSTOMER_REPLIED,
        ):
            return suspension
    return None
