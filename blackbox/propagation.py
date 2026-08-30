"""Making labels survive the model.

A label on a database field is easy. The hard part, and the whole point of
Invisible Ink, is that the label is still there after Gemini has read the fact,
reasoned about it, summarised it, and written a paraphrase that shares no words
with the original.

The rule that makes it work is simple and conservative:

    **The output of a model turn carries the join of the labels of everything
    that turn could see.**

Not "everything relevant". Everything. A language model conditions on its whole
context, so any part of that context could have shaped any part of the output.
Trying to work out which facts actually influenced which sentences would mean
asking the model to mark its own homework, and a model that under-reports its
influences produces a label that is quietly too loose. Joining across the whole
context cannot under-report.

That is why the stamp does not wash off at the first summarisation. It was never
attached to the words. It is attached to the fact that the model *read* the
source, which stays true no matter how the wording changes.

The cost is over-restriction: a turn that saw special category data carries that
class even if it only wrote about fees. Over-restriction is the safe direction and
it is recoverable, because the origins on the label say exactly which source
caused it and a human can see that the block was cautious. Under-restriction is
not recoverable, because the data has already left.
"""

from typing import Any, Dict, List, Optional

from .labels import Label, Provenance, Sensitivity, join_all

# What each source system's fields are worth. Read alongside the field table in
# WORKFLOW.md, which this mirrors.
_FIELD_CLASSES: Dict[str, Dict[str, Sensitivity]] = {
    "CRM360": {
        "name": Sensitivity.PII,
        "date_of_birth": Sensitivity.PII,
        "address": Sensitivity.PII,
        "email": Sensitivity.PII,
        "national_identifier": Sensitivity.PII_HIGH,
        "vulnerability_flags": Sensitivity.SPECIAL_CATEGORY,
    },
    "CoreBank": {
        "balance": Sensitivity.FINANCIAL,
        "transactions": Sensitivity.FINANCIAL,
        "counterparty_name": Sensitivity.THIRD_PARTY_PII,
    },
}

_RETENTION_BY_CLASS: Dict[Sensitivity, str] = {
    Sensitivity.SPECIAL_CATEGORY: "retain_1_year",
    Sensitivity.PII_HIGH: "retain_1_year",
    Sensitivity.THIRD_PARTY_PII: "retain_1_year",
    Sensitivity.PII: "retain_6_years",
    Sensitivity.FINANCIAL: "retain_6_years",
    Sensitivity.INTERNAL_ONLY: "retain_6_years",
}


def _retention_for(classes) -> Optional[str]:
    """The retention rule implied by a set of classes, strictest first."""
    from .labels import _stricter_retention

    result = None
    for sensitivity in classes:
        result = _stricter_retention(result, _RETENTION_BY_CLASS.get(sensitivity))
    return result


def label_crm360_customer(response: Dict[str, Any], event_id: Optional[str] = None) -> Label:
    """Label a CRM360 profile.

    A profile is not one class. It is a name and an address, which are PII, a
    national identifier, which never leaves the bank, and possibly a
    vulnerability flag, which is special category. All three restrictions apply
    at once, which is why the label carries a set.
    """
    record = response.get("record", {})
    classes = {Sensitivity.PII}
    origins = [Provenance("CRM360", "profile", event_id)]

    if record.get("national_identifier"):
        classes.add(Sensitivity.PII_HIGH)
        origins.append(
            Provenance("CRM360", "national_identifier", event_id, "never leaves the bank")
        )

    if record.get("vulnerability_flags"):
        classes.add(Sensitivity.SPECIAL_CATEGORY)
        flags = ", ".join(record["vulnerability_flags"])
        origins.append(
            Provenance("CRM360", "vulnerability_flags", event_id, f"flags: {flags}")
        )

    jurisdictions = {record["country_of_residence"]} if record.get("country_of_residence") else set()
    return Label.make(classes, jurisdictions, origins, _retention_for(classes))


def label_corebank_account(response: Dict[str, Any], event_id: Optional[str] = None) -> Label:
    """Label a CoreBank account record. Jurisdiction comes from the domicile."""
    record = response.get("record", {})
    jurisdictions = {record["domicile"]} if record.get("domicile") else set()
    classes = {Sensitivity.FINANCIAL}
    return Label.make(
        classes,
        jurisdictions,
        [Provenance("CoreBank", "account", event_id)],
        _retention_for(classes),
    )


def label_corebank_transactions(
    response: Dict[str, Any], event_id: Optional[str] = None
) -> Label:
    """Label CoreBank transactions.

    A transaction record that names a counterparty carries THIRD_PARTY_PII. That
    name belongs to somebody who is not the complainant, and the shorter of the
    two Invisible Ink demonstrations is the Correspondence Agent trying to cite
    such a record as evidence.
    """
    classes = {Sensitivity.FINANCIAL}
    origins = [Provenance("CoreBank", "transactions", event_id)]

    for row in response.get("records", []):
        if row.get("counterparty_name"):
            classes.add(Sensitivity.THIRD_PARTY_PII)
            origins.append(
                Provenance(
                    "CoreBank",
                    "counterparty_name",
                    event_id,
                    f"names a third party on {row.get('transaction_id')}",
                )
            )
            break

    return Label.make(classes, set(), origins, _retention_for(classes))


def label_commsvault(response: Dict[str, Any], event_id: Optional[str] = None) -> Label:
    """Label a CommsVault answer.

    A job acceptance carries nothing yet. Returned records carry whatever the
    archived material carries, which for a call about a customer's health is
    special category.
    """
    if response.get("status") != "READY":
        return Label.make(
            [Sensitivity.INTERNAL_ONLY],
            set(),
            [Provenance("CommsVault", "job", event_id, "job id only, no records yet")],
        )

    classes = {Sensitivity.PII}
    origins = [Provenance("CommsVault", "archive", event_id)]
    for record in response.get("records", []):
        raw = record.get("sensitivity")
        if raw == "SPECIAL_CATEGORY":
            classes.add(Sensitivity.SPECIAL_CATEGORY)
            origins.append(
                Provenance(
                    "CommsVault",
                    record.get("record_id", "record"),
                    event_id,
                    f"{record.get('type')} dated {record.get('date')}",
                )
            )
        elif raw == "MIXED":
            classes.add(Sensitivity.MIXED)

    return Label.make(classes, set(), origins, _retention_for(classes))


def label_complaint_narrative(
    complaint: Dict[str, Any], event_id: Optional[str] = None
) -> Label:
    """Label an arriving complaint narrative.

    MIXED, because nobody has read it yet and it may contain anything. This is
    where the four-hop trail starts: the customer wrote a sentence about their
    health, and at this moment the system does not yet know that.
    """
    return Label.make(
        [Sensitivity.MIXED],
        set(),
        [
            Provenance(
                "Intake",
                "complaint_narrative",
                event_id,
                f"as written by the customer on {complaint.get('received_at', 'unknown date')}",
            )
        ],
        "retain_6_years",
    )


def label_intake_determination(
    determination: Dict[str, Any], event_id: Optional[str] = None
) -> Label:
    """Label the Intake Agent's structured extraction.

    Hop one of the trail. The narrative went in as unexamined free text; what
    comes out is structured, and if the agent concluded the customer shows
    vulnerability indicators then it has just extracted special category data out
    of prose. The label is attached to that conclusion, not to any word in it.
    """
    classes = {Sensitivity.PII}
    origins = [Provenance("Intake", "determination", event_id)]

    if determination.get("vulnerability_indicators"):
        classes.add(Sensitivity.SPECIAL_CATEGORY)
        origins.append(
            Provenance(
                "Intake",
                "vulnerability_indicators",
                event_id,
                determination.get("vulnerability_reasoning", "")[:160],
            )
        )

    jurisdictions = (
        {determination["jurisdiction"]} if determination.get("jurisdiction") else set()
    )
    return Label.make(classes, jurisdictions, origins, _retention_for(classes))


def label_assessment(assessment: Dict[str, Any], event_id: Optional[str] = None) -> Label:
    """Label the Assessment Agent's reasoning.

    INTERNAL_ONLY, always. This is the file note, written for colleagues and
    auditors, and it must never reach the customer. It is added to whatever the
    reasoning was derived from rather than replacing it.
    """
    return Label.make(
        [Sensitivity.INTERNAL_ONLY],
        set(),
        [
            Provenance(
                "Assessment",
                "reasoning",
                event_id,
                "internal file note, never for the customer",
            )
        ],
        "retain_6_years",
    )


# Which labelling function handles which tool's result.
_TOOL_LABELLERS = {
    "lookup_customer": label_crm360_customer,
    "get_customer_record": label_crm360_customer,
    "get_account_summary": label_corebank_account,
    "list_fee_transactions": label_corebank_transactions,
    "get_account_transactions": label_corebank_transactions,
    "request_archived_communications": label_commsvault,
    "request_comms_archive": label_commsvault,
}


def label_for_tool_result(
    tool_name: str, result: Any, event_id: Optional[str] = None
) -> Label:
    """Work out what a tool's answer is worth.

    A tool with no labeller is not assumed harmless. It is labelled from the
    ``origin`` and ``sensitivity`` the stub declared, and failing that it is
    treated as INTERNAL_ONLY, because a result nobody has classified is not
    something to put in a letter.
    """
    if not isinstance(result, dict):
        return Label.make(
            [Sensitivity.INTERNAL_ONLY],
            set(),
            [Provenance("tool", tool_name, event_id, "unstructured tool output")],
        )

    if result.get("error"):
        # An error message can still quote an identifier it was given.
        return Label.make(
            [Sensitivity.INTERNAL_ONLY],
            set(),
            [Provenance("tool", tool_name, event_id, "error response")],
        )

    labeller = _TOOL_LABELLERS.get(tool_name)
    if labeller is not None:
        return labeller(result, event_id)

    if tool_name == "record_intake_determination":
        return label_intake_determination(result.get("determination", {}), event_id)
    if tool_name == "record_assessment":
        return label_assessment(result, event_id)

    declared = result.get("sensitivity")
    if declared:
        try:
            sensitivity = Sensitivity(declared)
        except ValueError:
            sensitivity = Sensitivity.INTERNAL_ONLY
        return Label.make(
            [sensitivity],
            set(),
            [Provenance(result.get("origin", "tool"), tool_name, event_id)],
        )

    return Label.make(
        [Sensitivity.INTERNAL_ONLY],
        set(),
        [Provenance("tool", tool_name, event_id, "no sensitivity declared")],
    )


def label_for_model_turn(context_labels: List[Label]) -> Label:
    """The label carried by whatever the model just produced.

    The join of everything the model could see. See the module docstring for why
    this is the whole context rather than an attempt to work out which parts
    mattered.
    """
    return join_all(context_labels)
