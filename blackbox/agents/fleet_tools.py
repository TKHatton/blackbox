"""Tools for the five agents that Phase 3 adds.

Written for Gemini to read: each docstring says when to use the tool and what it
commits the bank to, not how it is implemented. Every one of them runs inside a
recorded run, so the call and its result are in the Diary whether or not the tool
itself writes an event.

Two tools here suspend the agent. They do not raise, and they do not block. They
write a SUSPEND event describing what would wake the case and then return, and
the agent is expected to stop calling tools after that. Waiting is a decision the
agent records, not a thread it holds open.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .. import wake
from ..gateway import Destination, DisclosureRequest, check_disclosure
from ..labels import Label, Provenance, Sensitivity
from ..policy import PolicyError, get_policy_engine
from ..propagation import label_assessment
from ..stubs.systems import SourceSystemError
from ..wiki import WikiPage, WikiUpdate
from .runtime import current_run

def gate_a_threshold() -> float:
    """The remedy value above which an adjudicator must sign off.

    Read from the active policy set rather than held as a module constant, which
    is what lets the Time Machine replay a case under a different threshold
    without editing this file. The name is kept as a function so no caller can
    capture the value at import time and miss a policy change.
    """
    return float(get_policy_engine().constant("gate_a_threshold", 500.0))


def appeal_window_days() -> int:
    """How long a case sleeps after the final response."""
    return int(get_policy_engine().constant("appeal_window_days", 30))


#: Kept so existing callers and tests keep working. Prefer the functions above:
#: this is evaluated once at import and will not follow a policy change.
GATE_A_THRESHOLD = 500.0
APPEAL_WINDOW_DAYS = 30


# ----------------------------------------------------------------------
# Shared: reading and rewriting the case page
# ----------------------------------------------------------------------


def read_case_file() -> Dict[str, Any]:
    """Read everything currently known about the case you are working on.

    Start here. This is the case file: the condensed current state, kept up to
    date by whichever agent last worked it. Use it instead of asking the source
    systems again for things another agent has already established.

    Returns:
        The current contents of the case file.
    """
    run = current_run()
    wiki = run.require_wiki()
    page = wiki.get_page(f"case:{run.recorder.case_id}")
    if page is None:
        return {"error": f"No case file exists for {run.recorder.case_id}"}

    run.recorder.memory_read(
        memory_key=page.page_id,
        content=page.content,
        reason="Agent read the case file before acting",
    )
    return {"case_id": run.recorder.case_id, "content": page.content, "version": page.version}


def _rewrite_case_file(updates: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Rewrite the case page in place, recording the rewrite in the Diary.

    Pages are rewritten, never appended to. A page that grew by appending would
    become a slower copy of the Diary, which is the Phase 1.5 failure mode.
    """
    run = current_run()
    wiki = run.require_wiki()
    page_id = f"case:{run.recorder.case_id}"
    page = wiki.get_page(page_id)

    events = run.recorder.events()
    derived_from = [e.event_id for e in events]
    now = datetime.now(timezone.utc)

    if page is None:
        page = WikiPage(
            page_id=page_id,
            subject=run.recorder.case_id,
            subject_type="case",
            content=dict(updates),
            derived_from=derived_from,
            version=1,
            created_at=now,
            updated_at=now,
        )
        wiki.create_page(page)
        new_version, old_version, old_derived = 1, 0, []
    else:
        merged = {**page.content, **updates}
        old_version, old_derived = page.version, list(page.derived_from)
        page = page.regenerate(new_content=merged, new_derived_from=derived_from)
        wiki.update_page(page)
        new_version = page.version

    wiki.record_update(
        WikiUpdate(
            page_id=page_id,
            old_version=old_version,
            new_version=new_version,
            old_derived_from=old_derived,
            new_derived_from=derived_from,
            timestamp=now,
            reason=reason,
        ),
        case_id=run.recorder.case_id,
        caused_by=run.recorder.current_cause,
        content=page.content,
    )
    return {"page_id": page_id, "version": new_version, "content": page.content}


# ----------------------------------------------------------------------
# Evidence Agent
# ----------------------------------------------------------------------


def get_customer_record(customer_id: str) -> Dict[str, Any]:
    """Fetch the customer's profile from CRM360.

    Args:
        customer_id: The customer reference.

    Returns:
        The profile, or an error description.
    """
    run = current_run()
    try:
        return run.systems.crm360.get_customer(customer_id)
    except SourceSystemError as exc:
        return {"error": str(exc), "system": "CRM360"}


def get_account_transactions(account_id: str) -> Dict[str, Any]:
    """Fetch an account's transactions from CoreBank.

    Some records name a third party. Those names belong to someone who is not
    your complainant, and the bank has no right to repeat them back.

    Args:
        account_id: The account reference.

    Returns:
        The transactions, or an error description.
    """
    run = current_run()
    try:
        return run.systems.corebank.get_transactions(account_id)
    except SourceSystemError as exc:
        return {"error": str(exc), "system": "CoreBank"}


def request_comms_archive(customer_id: str, reason: str) -> Dict[str, Any]:
    """Ask CommsVault for archived calls and emails.

    CommsVault takes two to three days. It gives you a job id now, not records.
    After calling this you should call suspend_until_evidence_ready with that job
    id rather than waiting or guessing at what the records might say.

    Args:
        customer_id: The customer reference.
        reason: Why these records are needed.

    Returns:
        A job id and the time the records are expected.
    """
    run = current_run()
    try:
        return run.systems.commsvault.request_records(customer_id, reason)
    except SourceSystemError as exc:
        return {"error": str(exc), "system": "CommsVault"}


def suspend_until_evidence_ready(job_id: str, ready_at: str, what_you_are_waiting_for: str) -> Dict[str, Any]:
    """Stop work on this case until a CommsVault job has results.

    Call this when you have a job id and cannot sensibly continue without the
    records. You will stop running entirely. Nothing stays open on your behalf.
    Days later the case will wake, you will be given a summary of everything
    known about it, and you will carry on from there.

    Do not call this if you already have enough to finish. A wait costs the
    customer days against a statutory clock.

    Args:
        job_id: The CommsVault job id.
        ready_at: The ISO timestamp CommsVault expects the records to be ready.
        what_you_are_waiting_for: Plain language, for whoever audits this later.

    Returns:
        Confirmation that the case is suspended.
    """
    run = current_run()
    try:
        ready = datetime.fromisoformat(ready_at)
    except (TypeError, ValueError):
        ready = datetime.now(timezone.utc) + timedelta(days=2)

    condition = wake.batch_job_condition(
        resume_agent="evidence_agent",
        job_id=job_id,
        ready_at=ready,
        description=what_you_are_waiting_for,
    )
    event_id = run.recorder.suspend(
        reason=f"Waiting on CommsVault job {job_id}: {what_you_are_waiting_for}",
        condition=condition,
        state_snapshot={"job_id": job_id},
    )
    run.suspended_on = event_id

    return {
        "status": "SUSPENDED",
        "suspend_event_id": event_id,
        "wakes_when": condition.description,
        "not_before": ready.isoformat(),
        "note": "Stop here. You are no longer running on this case.",
    }


def record_evidence_gathered(
    summary: str, sufficient_to_assess: bool, outstanding_items: str
) -> Dict[str, Any]:
    """Record what evidence you gathered and whether it is enough to assess on.

    Call this once you are done gathering. If records are still outstanding, say
    so honestly rather than implying the picture is complete.

    Args:
        summary: What the evidence shows.
        sufficient_to_assess: Whether the Assessment Agent can proceed on this.
        outstanding_items: Anything still missing. Empty string if nothing.

    Returns:
        Confirmation the case file has been updated.
    """
    run = current_run()
    result = _rewrite_case_file(
        {
            "evidence_summary": summary,
            "evidence_sufficient": sufficient_to_assess,
            "evidence_outstanding": outstanding_items,
            "status": "evidence_gathered" if sufficient_to_assess else "evidence_partial",
            "next_step": "Assessment Agent to decide the outcome",
        },
        reason="Evidence Agent recorded what it found",
    )
    run.outputs["evidence"] = summary
    return result


# ----------------------------------------------------------------------
# Assessment Agent
# ----------------------------------------------------------------------


def record_assessment(
    outcome: str,
    reasoning: str,
    proposed_remedy: str,
    remedy_amount: float,
    looks_systemic: bool,
    systemic_reasoning: str,
) -> Dict[str, Any]:
    """Record your decision on the complaint and the remedy you propose.

    Your reasoning here is internal. It is written for colleagues and auditors,
    not for the customer, and it must never appear in a letter.

    Say a complaint looks systemic only if you think the same fault would affect
    other customers who have not complained. That is a serious call: it stops any
    customer-facing statement until Compliance has signed it off.

    Args:
        outcome: One of upheld, partially_upheld, not_upheld.
        reasoning: Why. Internal only.
        proposed_remedy: What you propose the bank does.
        remedy_amount: The monetary value in the account's currency. Zero if none.
        looks_systemic: Whether this may affect other customers.
        systemic_reasoning: What makes you think so, or why you think not.

    Returns:
        The recorded assessment and which approval gates it triggers.
    """
    run = current_run()

    # The gates are policy, not code. Which threshold applied is recorded with
    # the decision, so a case can later be replayed against a different one and
    # the difference attributed to the policy rather than to the agent.
    engine = run.policy_engine or get_policy_engine()
    threshold = float(engine.constant("gate_a_threshold", 500.0))
    context = {"remedy_amount": remedy_amount, "looks_systemic": looks_systemic}

    try:
        gate_a_result = engine.evaluate("gate_a_monetary_threshold", context)
        gate_b_result = engine.evaluate("gate_b_systemic_flag", context)
    except PolicyError as exc:
        # A gate that cannot be evaluated escalates. An approval gate that
        # silently fails open is the worst kind of governance defect.
        run.recorder.policy_check(
            policy_id="gate_evaluation_failed",
            check_type="approval_threshold",
            input_data=context,
            decision="escalate",
            reasoning=(
                f"An approval gate could not be evaluated, so the case is being "
                f"escalated rather than allowed to proceed: {exc}"
            ),
        )
        return {"error": f"Approval gates could not be evaluated: {exc}", "status": "ESCALATED"}

    gate_a = gate_a_result.fired
    gate_b = gate_b_result.fired

    run.recorder.policy_check(
        policy_id="gate_a_monetary_threshold",
        check_type="approval_threshold",
        input_data={
            "remedy_amount": remedy_amount,
            "threshold": threshold,
            "policy_version": engine.policies.version,
        },
        decision="escalate" if gate_a else "allow",
        reasoning=(
            gate_a_result.reason
            if gate_a
            else f"Proposed remedy of {remedy_amount} is at or below the {threshold} "
            f"threshold, so no adjudicator sign-off is required."
        ),
    )
    run.recorder.policy_check(
        policy_id="gate_b_systemic_flag",
        check_type="approval_threshold",
        input_data={"looks_systemic": looks_systemic, "policy_version": engine.policies.version},
        decision="escalate" if gate_b else "allow",
        reasoning=systemic_reasoning,
    )

    _rewrite_case_file(
        {
            "outcome": outcome,
            "assessment_reasoning": reasoning,
            "assessment_reasoning_visibility": "INTERNAL_ONLY",
            "proposed_remedy": proposed_remedy,
            "remedy_amount": remedy_amount,
            "systemic_flag": looks_systemic,
            "systemic_reasoning": systemic_reasoning,
            "gate_a_required": gate_a,
            "gate_b_required": gate_b,
            "status": "assessed",
        },
        reason="Assessment Agent decided the outcome and proposed a remedy",
    )

    # Invisible Ink. From here on, anything this agent writes is derived from
    # internal reasoning, and the Correspondence Agent must not repeat it.
    run.absorb(label_assessment({"reasoning": reasoning}))

    run.outputs["assessment"] = {"outcome": outcome, "remedy_amount": remedy_amount}
    return {
        "status": "ASSESSED",
        "outcome": outcome,
        "gate_a_required": gate_a,
        "gate_b_required": gate_b,
        "next": (
            "Approval is required. Call suspend_until_approved."
            if (gate_a or gate_b)
            else "No approval gate applies. The Remediation Agent can proceed."
        ),
    }


def suspend_until_approved(gate: str, what_is_being_approved: str) -> Dict[str, Any]:
    """Stop work until a human approves the remedy.

    Call this after record_assessment tells you a gate applies. You stop running.
    The approval arrives on its own, days later, and the case wakes then. There
    is nothing you can do to speed it up and nothing to poll.

    Args:
        gate: Either "A" for the monetary threshold or "B" for the systemic flag.
        what_is_being_approved: Plain language, for the approver and the auditor.

    Returns:
        Confirmation that the case is suspended and who it is waiting on.
    """
    run = current_run()
    request_id = f"APR-{run.recorder.case_id}-{gate}"

    condition = wake.approval_condition(
        resume_agent="assessment_agent",
        gate=gate,
        request_id=request_id,
        description=f"Human approval on gate {gate}: {what_is_being_approved}",
    )
    event_id = run.recorder.suspend(
        reason=f"Awaiting gate {gate} approval: {what_is_being_approved}",
        condition=condition,
        state_snapshot={"gate": gate, "request_id": request_id},
    )
    run.suspended_on = event_id

    run.recorder.escalate(
        reason=what_is_being_approved,
        escalation_type="human_approval",
        context={"gate": gate, "request_id": request_id, "case_id": run.recorder.case_id},
        urgency="high" if gate == "B" else "medium",
    )

    return {
        "status": "SUSPENDED",
        "suspend_event_id": event_id,
        "request_id": request_id,
        "note": "Stop here. A human has to act before this case moves.",
    }


# ----------------------------------------------------------------------
# Remediation Agent. The only agent with write access to money.
# ----------------------------------------------------------------------


def execute_remedy(account_id: str, amount: float, description: str) -> Dict[str, Any]:
    """Pay a remedy into the customer's account through CoreBank.

    This moves money. Only call it for a remedy that has been assessed and, where
    a gate applied, approved. If the case file does not show approval where it
    was required, do not call this: say so and stop.

    Args:
        account_id: The account to credit.
        amount: The amount to credit. Must be positive.
        description: What the customer will see on their statement.

    Returns:
        The result of the write, or a refusal explaining why nothing was done.
    """
    run = current_run()
    wiki = run.require_wiki()
    page = wiki.get_page(f"case:{run.recorder.case_id}")
    content = page.content if page else {}

    # The guard belongs here rather than in the prompt. An agent that talked
    # itself past an unapproved gate must still not be able to move money.
    if content.get("gate_a_required") and not content.get("gate_a_approved"):
        refusal = "Gate A approval is required for this remedy and the case file does not record it."
        run.recorder.policy_check(
            policy_id="remediation_requires_approval",
            check_type="approval_threshold",
            input_data={"account_id": account_id, "amount": amount},
            decision="block",
            reasoning=refusal,
        )
        return {"error": refusal, "executed": False}

    if content.get("gate_b_required") and not content.get("gate_b_approved"):
        refusal = "Gate B approval is required for this case and the case file does not record it."
        run.recorder.policy_check(
            policy_id="remediation_requires_approval",
            check_type="approval_threshold",
            input_data={"account_id": account_id, "amount": amount},
            decision="block",
            reasoning=refusal,
        )
        return {"error": refusal, "executed": False}

    if amount <= 0:
        return {"error": "Remedy amount must be positive", "executed": False}

    _rewrite_case_file(
        {
            "remedy_executed": True,
            "remedy_executed_amount": amount,
            "remedy_executed_at": datetime.now(timezone.utc).isoformat(),
            "status": "remedy_executed",
            "next_step": "Correspondence Agent to send the final response",
        },
        reason="Remediation Agent credited the approved remedy",
    )

    return {
        "executed": True,
        "account_id": account_id,
        "amount": amount,
        "description": description,
        "system": "CoreBank",
    }


# ----------------------------------------------------------------------
# Correspondence Agent. The primary outbound path.
# ----------------------------------------------------------------------


async def send_customer_letter(letter_type: str, body: str, purpose: str) -> Dict[str, Any]:
    """Send a letter to the customer through PrintPost.

    This is what the customer actually reads, so it is the one place where a
    mistake reaches a person outside the bank. Write plainly and warmly. Never
    include internal assessment reasoning, and never name another customer who
    appears in a transaction record.

    Everything sent this way passes the disclosure gateway first. If the gateway
    refuses, nothing is sent, and you will be told why. Do not try to work around
    a refusal by rewording: the restriction follows what the content is derived
    from, not the words you chose.

    Args:
        letter_type: One of acknowledgment, holding, final_response, appeal_outcome.
        body: The letter text.
        purpose: Why this letter is being sent. Say "final response" for the
            letter that closes the case.

    Returns:
        The result of the send, or a refusal explaining why nothing went out.
    """
    run = current_run()
    wiki = run.require_wiki()
    page = wiki.get_page(f"case:{run.recorder.case_id}")
    content = page.content if page else {}

    # Gate B stops any customer-facing statement until Compliance has signed off.
    if content.get("gate_b_required") and not content.get("gate_b_approved"):
        refusal = (
            "This case is flagged as possibly systemic, and Compliance has not signed "
            "off. No customer-facing statement may be made yet."
        )
        run.recorder.policy_check(
            policy_id="gate_b_blocks_customer_contact",
            check_type="data_disclosure",
            input_data={"letter_type": letter_type},
            decision="block",
            reasoning=refusal,
        )
        return {"error": refusal, "sent": False}

    recipient = str(content.get("customer_id", "unknown"))

    # Invisible Ink. The letter carries everything this agent has read, joined
    # with the jurisdiction the case was assigned at intake.
    label = run.taint
    if content.get("jurisdiction"):
        label = label.join(Label.make([], {content["jurisdiction"]}, []))
    if content.get("vulnerability_indicators"):
        label = label.join(
            Label.make(
                [Sensitivity.SPECIAL_CATEGORY],
                {content.get("jurisdiction")} if content.get("jurisdiction") else set(),
                [
                    Provenance(
                        "Intake",
                        "vulnerability_indicators",
                        None,
                        str(content.get("vulnerability_reasoning", ""))[:160],
                    )
                ],
            )
        )
    run.taint = label

    verdict = await check_disclosure(
        DisclosureRequest(
            content=body,
            label=label,
            destination=Destination.CUSTOMER,
            destination_system=run.systems.printpost.name,
            recipient=recipient,
            purpose=purpose,
            case_id=run.recorder.case_id,
            adequacy_basis=content.get("transfer_adequacy_basis"),
        ),
        recorder=run.recorder,
        model=run.judge_model,
        engine=run.policy_engine,
    )

    if not verdict.allowed:
        return {
            "sent": False,
            "blocked_by": verdict.rule_id,
            "reason": verdict.reasoning,
            "judged_by": verdict.judged_by,
            "policy_check_event_id": verdict.event_id,
            "note": (
                "Nothing was sent. The restriction follows what this content is "
                "derived from, so rewording it will not help. Escalate instead."
            ),
        }

    result = run.systems.printpost.send_letter(recipient=recipient, body=body)

    run.recorder.message_sent(
        labels=label.to_dict(),
        recipient=recipient,
        channel="post_via_printpost",
        content=body,
        purpose=purpose,
    )

    updates: Dict[str, Any] = {f"{letter_type}_sent_at": datetime.now(timezone.utc).isoformat()}
    if letter_type == "final_response":
        window_closes = datetime.now(timezone.utc) + timedelta(days=appeal_window_days())
        updates.update(
            {
                "status": "awaiting_appeal_window",
                "appeal_window_closes_at": window_closes.isoformat(),
                "next_step": "Compliance Officer to close or reopen after the appeal window",
            }
        )
    _rewrite_case_file(updates, reason=f"Correspondence Agent sent the {letter_type}")

    return {"sent": True, "letter_type": letter_type, "destination": result}


def suspend_for_appeal_window() -> Dict[str, Any]:
    """Put the case to sleep for the 30 day appeal window.

    Call this after the final response has gone out. The case sleeps and wakes
    only when the window closes, or earlier if the customer replies.

    Returns:
        Confirmation that the case is asleep and when it will wake.
    """
    run = current_run()
    window_days = appeal_window_days()
    closes_at = datetime.now(timezone.utc) + timedelta(days=window_days)
    condition = wake.appeal_window_condition(
        resume_agent="compliance_officer",
        window_closes_at=closes_at,
        description=f"The {window_days} day appeal window closes",
    )
    event_id = run.recorder.suspend(
        reason="Final response sent. Sleeping through the appeal window.",
        condition=condition,
        state_snapshot={"appeal_window_closes_at": closes_at.isoformat()},
    )
    run.suspended_on = event_id
    return {
        "status": "SUSPENDED",
        "suspend_event_id": event_id,
        "wakes_at": closes_at.isoformat(),
        "note": "The case wakes early if the customer replies.",
    }


# ----------------------------------------------------------------------
# Compliance Officer. Watches the clocks and acts unprompted.
# ----------------------------------------------------------------------


def check_case_clocks() -> Dict[str, Any]:
    """Check the statutory deadlines on the case you are looking at.

    Tells you how long is left on each clock and what has already been sent.
    Use it to decide whether the case needs a holding letter, an escalation, or
    nothing at all.

    Returns:
        The deadlines, the days remaining on each, and what has been sent.
    """
    run = current_run()
    wiki = run.require_wiki()
    page = wiki.get_page(f"case:{run.recorder.case_id}")
    if page is None:
        return {"error": f"No case file for {run.recorder.case_id}"}

    content = page.content
    deadlines = content.get("deadlines", {})
    now = datetime.now(timezone.utc)

    def days_left(iso: Optional[str]) -> Optional[float]:
        if not iso:
            return None
        try:
            return round((datetime.fromisoformat(iso) - now).total_seconds() / 86400, 2)
        except ValueError:
            return None

    return {
        "case_id": run.recorder.case_id,
        "status": content.get("status"),
        "jurisdiction": content.get("jurisdiction"),
        "vulnerability_indicators": content.get("vulnerability_indicators"),
        "acknowledgment_due_in_days": days_left(deadlines.get("acknowledgment_due")),
        "final_response_due_in_days": days_left(deadlines.get("final_response_due")),
        "acknowledgment_sent": bool(content.get("acknowledgment_sent_at")),
        "holding_letter_sent": bool(content.get("holding_sent_at")),
        "final_response_sent": bool(content.get("final_response_sent_at")),
        "gate_a_required": content.get("gate_a_required"),
        "gate_a_approved": content.get("gate_a_approved"),
        "gate_b_required": content.get("gate_b_required"),
        "gate_b_approved": content.get("gate_b_approved"),
    }


def instruct_holding_letter(why: str) -> Dict[str, Any]:
    """Record that this case needs a holding letter, and why.

    A holding letter tells a customer their complaint is still open and why it is
    taking longer than expected. Call this when a case is approaching its final
    response deadline without an answer.

    Args:
        why: Why the case needs one. The customer will be told a version of this.

    Returns:
        Confirmation the instruction is recorded.
    """
    run = current_run()
    run.recorder.policy_check(
        policy_id="holding_letter_required",
        check_type="statutory_deadline",
        input_data={"case_id": run.recorder.case_id},
        decision="escalate",
        reasoning=why,
    )
    result = _rewrite_case_file(
        {"holding_letter_required": True, "holding_letter_reason": why},
        reason="Compliance Officer decided a holding letter is required",
    )
    run.outputs["holding_letter_required"] = why
    return {"status": "HOLDING_LETTER_INSTRUCTED", "reason": why, "case_file": result["version"]}


def escalate_to_human(why: str, urgency: str) -> Dict[str, Any]:
    """Escalate this case to a person, on your own judgment.

    Use it when the case needs a decision you should not be making, or when a
    statutory deadline is going to be missed and somebody needs to know now.

    Args:
        why: What you want a person to look at, and why it cannot wait.
        urgency: One of low, medium, high, critical.

    Returns:
        Confirmation the escalation is recorded.
    """
    run = current_run()
    event_id = run.recorder.escalate(
        reason=why,
        escalation_type="human",
        context={"case_id": run.recorder.case_id},
        urgency=urgency,
    )
    return {"status": "ESCALATED", "event_id": event_id, "urgency": urgency}


async def file_with_regulator(jurisdiction: str, summary: str) -> Dict[str, Any]:
    """File a report about this case with the regulator.

    Only for cases that must be reported. Filing is visible outside the bank and
    cannot be taken back, so be sure before you call it. It passes the disclosure
    gateway like any other outbound path.

    Args:
        jurisdiction: Which regulator, for example UK or EU_IE.
        summary: What is being reported.

    Returns:
        The filing reference, or a refusal explaining why nothing was filed.
    """
    run = current_run()

    label = run.taint.join(Label.make([], {jurisdiction}, []))
    run.taint = label

    verdict = await check_disclosure(
        DisclosureRequest(
            content=summary,
            label=label,
            destination=Destination.REGULATOR,
            destination_system=run.systems.regportal.name,
            recipient=f"regulator:{jurisdiction}",
            purpose="Regulatory filing",
            case_id=run.recorder.case_id,
        ),
        recorder=run.recorder,
        model=run.judge_model,
        engine=run.policy_engine,
    )

    if not verdict.allowed:
        return {
            "filed": False,
            "blocked_by": verdict.rule_id,
            "reason": verdict.reasoning,
            "policy_check_event_id": verdict.event_id,
        }

    result = run.systems.regportal.file_report(jurisdiction=jurisdiction, summary=summary)
    run.recorder.message_sent(
        labels=label.to_dict(),
        recipient=f"regulator:{jurisdiction}",
        channel="regportal",
        content=summary,
        purpose="Regulatory filing",
    )
    _rewrite_case_file(
        {"regulator_filed": True, "regulator_reference": result["reference"]},
        reason="Compliance Officer filed with the regulator",
    )
    result["filed"] = True
    return result


def record_transfer_adequacy_basis(basis: str, who_authorised: str) -> Dict[str, Any]:
    """Record a legal basis for sending this case's data to a third country.

    Use this when the gateway has refused an outbound action because the case
    carries special category data from a restricted jurisdiction and the
    destination is outside it. Recording a basis does not make the data less
    sensitive. It documents who decided the transfer may proceed and on what
    grounds, so that the decision has a name attached to it.

    Do not invent a basis to clear a block. If none applies, escalate instead.

    Args:
        basis: The grounds, for example "standard contractual clauses with the
            vendor, executed 2026-03-01" or "explicit consent obtained from the
            customer on 2026-08-30".
        who_authorised: The person or role standing behind this.

    Returns:
        Confirmation that the basis is recorded against the case.
    """
    run = current_run()
    run.recorder.policy_check(
        policy_id="transfer_adequacy_basis_recorded",
        check_type="data_transfer",
        input_data={"basis": basis, "authorised_by": who_authorised},
        decision="allow",
        reasoning=(
            f"An adequacy basis for third-country transfer was recorded by "
            f"{who_authorised}: {basis}"
        ),
    )
    _rewrite_case_file(
        {
            "transfer_adequacy_basis": basis,
            "transfer_adequacy_authorised_by": who_authorised,
        },
        reason="Compliance Officer recorded a third-country transfer basis",
    )
    return {"status": "BASIS_RECORDED", "basis": basis, "authorised_by": who_authorised}


def close_case(why: str) -> Dict[str, Any]:
    """Close the case.

    Call this when the appeal window has passed with no reply and everything the
    case required has been done.

    Args:
        why: Why the case can be closed.

    Returns:
        Confirmation.
    """
    run = current_run()
    _rewrite_case_file(
        {"status": "closed", "closed_reason": why, "next_step": None},
        reason="Compliance Officer closed the case",
    )
    return {"status": "CLOSED", "reason": why}


EVIDENCE_TOOLS = [
    read_case_file,
    get_customer_record,
    get_account_transactions,
    request_comms_archive,
    suspend_until_evidence_ready,
    record_evidence_gathered,
]

ASSESSMENT_TOOLS = [read_case_file, record_assessment, suspend_until_approved]

REMEDIATION_TOOLS = [read_case_file, execute_remedy]

CORRESPONDENCE_TOOLS = [read_case_file, send_customer_letter, suspend_for_appeal_window]

COMPLIANCE_TOOLS = [
    read_case_file,
    check_case_clocks,
    instruct_holding_letter,
    escalate_to_human,
    record_transfer_adequacy_basis,
    file_with_regulator,
    close_case,
]
