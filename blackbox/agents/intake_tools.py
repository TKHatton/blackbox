"""Tools available to the Intake Agent.

Each function is handed to ADK as a tool, so its docstring and type hints become
the schema Gemini reads when deciding what to call. They are written for that
audience: the docstring says when to use the tool, not how it is implemented.

Every function reaches the source systems through the current run, which means
every call is bracketed by a TOOL_CALL and TOOL_RESULT event without the tool
having to write either one itself.

Tools return dictionaries rather than raising, because a raised exception would
end the model's turn. A tool that reports its failure lets the agent reason about
what to do next, and the failure still lands in the Diary as a TOOL_RESULT with
success set to false.
"""

from typing import Any, Dict

from ..propagation import label_intake_determination
from ..stubs.systems import SourceSystemError
from .runtime import current_run


def lookup_customer(customer_id: str) -> Dict[str, Any]:
    """Look up a customer's profile in CRM360.

    Use this to find the customer's country of residence, contact preferences,
    and any vulnerability flags already recorded against them. Vulnerability
    flags change how the case must be handled downstream, so check them before
    classifying.

    Args:
        customer_id: The customer reference, for example CUST-4471.

    Returns:
        The customer profile, or an error description if the customer is unknown.
    """
    run = current_run()
    try:
        return run.systems.crm360.get_customer(customer_id)
    except SourceSystemError as exc:
        return {"error": str(exc), "system": "CRM360"}


def get_account_summary(account_id: str) -> Dict[str, Any]:
    """Look up an account in CoreBank.

    Use this to find where the account is domiciled, its balance, and whether it
    is in arrears. The account's domicile can differ from the customer's country
    of residence, and when it does you must decide which jurisdiction's rules
    apply rather than assuming.

    Args:
        account_id: The account reference, for example ACC-88214.

    Returns:
        The account record, or an error description if the account is unknown.
    """
    run = current_run()
    try:
        return run.systems.corebank.get_account(account_id)
    except SourceSystemError as exc:
        return {"error": str(exc), "system": "CoreBank"}


def list_fee_transactions(account_id: str) -> Dict[str, Any]:
    """List the fees charged to an account in CoreBank.

    Use this when a complaint is about charges, to see what was actually taken
    and when. Transaction records may name third parties. Those names are not
    yours to repeat back to the complainant.

    Args:
        account_id: The account reference, for example ACC-88214.

    Returns:
        The fee transactions, or an error description if the account is unknown.
    """
    run = current_run()
    try:
        return run.systems.corebank.get_transactions(account_id, fees_only=True)
    except SourceSystemError as exc:
        return {"error": str(exc), "system": "CoreBank"}


def request_archived_communications(customer_id: str, reason: str) -> Dict[str, Any]:
    """Ask CommsVault for archived emails and call transcripts for a customer.

    Use this when the complaint refers to an earlier phone call or message that
    you cannot see in CRM360. CommsVault does not answer immediately. It accepts
    the request and returns a job id, and the records become available two to
    three days later. Do not wait for it and do not treat a job id as evidence:
    note the job id, say in your assessment that the records are outstanding, and
    continue with what you already have.

    Args:
        customer_id: The customer reference, for example CUST-4471.
        reason: Why these records are needed. Recorded for the audit trail.

    Returns:
        A job id and an estimated ready time, or an error description.
    """
    run = current_run()
    try:
        return run.systems.commsvault.request_records(customer_id, reason)
    except SourceSystemError as exc:
        return {"error": str(exc), "system": "CommsVault"}


def record_intake_determination(
    category: str,
    severity: str,
    jurisdiction: str,
    jurisdiction_reasoning: str,
    vulnerability_indicators: bool,
    vulnerability_reasoning: str,
    summary: str,
    acknowledgment_due_days: int,
    final_response_due_days: int,
) -> Dict[str, Any]:
    """Record your intake decision and start the statutory clock.

    Call this exactly once, as the last thing you do, after you have gathered
    what you need. This opens the case formally.

    Args:
        category: One of billing_dispute, service_failure, mis_sold_product,
            fraud, data_handling.
        severity: One of low, medium, high, critical.
        jurisdiction: One of US, US_CA, UK, EU_IE, EU_DE.
        jurisdiction_reasoning: Why that jurisdiction governs this case, given
            the customer's residence and the account's domicile. Say explicitly
            which one you weighted and why.
        vulnerability_indicators: True if the customer shows signs of
            vulnerability, whether flagged in CRM360 or evident in what they
            wrote.
        vulnerability_reasoning: What led you to that conclusion.
        summary: A short factual summary of the complaint.
        acknowledgment_due_days: Business days until an acknowledgment is due.
        final_response_due_days: Calendar days until a final response is due.

    Returns:
        Confirmation that the case is open, with the deadlines that now apply.
    """
    run = current_run()

    determination = {
        "category": category,
        "severity": severity,
        "jurisdiction": jurisdiction,
        "jurisdiction_reasoning": jurisdiction_reasoning,
        "vulnerability_indicators": vulnerability_indicators,
        "vulnerability_reasoning": vulnerability_reasoning,
        "summary": summary,
        "acknowledgment_due_days": acknowledgment_due_days,
        "final_response_due_days": final_response_due_days,
    }
    run.determination = determination

    # Invisible Ink, hop one. Unexamined prose has just become a structured
    # conclusion. If the agent read vulnerability out of the narrative, special
    # category data has been extracted from free text, and the label attaches to
    # that conclusion rather than to any word in it.
    run.absorb(label_intake_determination(determination))

    return {
        "status": "CASE_OPEN",
        "case_id": run.recorder.case_id,
        "determination": determination,
        "note": (
            "The statutory clock is running. The Compliance Officer Agent watches "
            "these deadlines from here."
        ),
    }


INTAKE_TOOLS = [
    lookup_customer,
    get_account_summary,
    list_fee_transactions,
    request_archived_communications,
    record_intake_determination,
]
