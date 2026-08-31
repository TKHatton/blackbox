"""Phase 9 tests: The Crash Test.

The failure modes the spec names:

- faults injected at a layer the agents never see, so the test proves nothing
- retry loops presented as resilience, when retrying a contradiction is not
  handling it
- recovery that silently proceeds on partial data, which is the outcome this
  feature exists to rule out
"""

import inspect

import pytest

from blackbox import degradation as degradation_module
from blackbox.agents.runtime import agent_run
from blackbox.degradation import (
    CONSEQUENTIAL_TOOLS,
    Degradation,
    score_degradation,
)
from blackbox.faults import (
    Fault,
    FaultRegistry,
    FaultType,
    FaultySystems,
    get_fault_registry,
    reset_fault_registry,
)
from blackbox.recorder import Recorder
from blackbox.schema import EventType
from blackbox.wiki import WikiPage

from fakes import ScriptedLlm, say, think_and_call


@pytest.fixture(autouse=True)
def _clean_faults():
    reset_fault_registry()
    yield
    reset_fault_registry()


@pytest.fixture
def faulty(systems):
    return FaultySystems(systems, registry=FaultRegistry())


def seed_case(store, wiki, case_id="CASE-CRASH"):
    from datetime import datetime, timezone

    rec = Recorder(case_id=case_id, actor="evidence_agent", store=store)
    root = rec.tool_call(
        tool_name="IntakeChannel.poll", parameters={}, intended_outcome="collect"
    )
    rec.set_cause(root)
    now = datetime.now(timezone.utc)
    wiki.create_page(
        WikiPage(
            page_id=f"case:{case_id}",
            subject=case_id,
            subject_type="case",
            content={
                "status": "open",
                "customer_id": "CUST-4471",
                "account_id": "ACC-88214",
                "jurisdiction": "US",
            },
            derived_from=[root],
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    return rec


# ----------------------------------------------------------------------
# Faults reach the agent
# ----------------------------------------------------------------------


def test_a_timeout_comes_back_as_something_the_agent_reads(faulty):
    """The named failure mode is a fault the agent never sees."""
    faulty._registry.arm(
        Fault(FaultType.TIMEOUT, target_system="commsvault", target_method="request_records")
    )
    result = faulty.commsvault.request_records("CUST-4471", "reason")

    assert isinstance(result, dict)
    assert result["fault"] == "timeout"
    assert "did not respond" in result["error"]
    assert result["retryable"] is True


def test_a_malformed_response_is_marked_not_retryable(faulty):
    faulty._registry.arm(
        Fault(FaultType.MALFORMED_RESPONSE, target_system="corebank", target_method="get_account")
    )
    result = faulty.corebank.get_account("ACC-88214")

    assert result["fault"] == "malformed_response"
    assert result["retryable"] is False


def test_an_unfaulted_call_returns_genuine_data(faulty):
    """The difference between working and broken must be exactly the fault."""
    result = faulty.crm360.get_customer("CUST-4471")
    assert result["record"]["customer_id"] == "CUST-4471"


def test_a_fault_can_be_scoped_to_one_method(faulty):
    faulty._registry.arm(
        Fault(FaultType.TIMEOUT, target_system="corebank", target_method="get_account")
    )
    assert faulty.corebank.get_account("ACC-88214").get("fault") == "timeout"
    # A different method on the same system is untouched.
    assert "records" in faulty.corebank.get_transactions("ACC-88214")


def test_a_fault_with_a_count_expires(faulty):
    faulty._registry.arm(
        Fault(FaultType.TIMEOUT, target_system="crm360", target_method="get_customer", remaining=1)
    )
    assert faulty.crm360.get_customer("CUST-4471").get("fault") == "timeout"
    # The second call gets through.
    assert faulty.crm360.get_customer("CUST-4471")["record"]["customer_id"] == "CUST-4471"


def test_faults_shown_to_the_agent_are_recorded(faulty):
    faulty._registry.arm(Fault(FaultType.TIMEOUT, target_system="commsvault"))
    faulty.commsvault.request_records("CUST-4471", "r")
    assert len(faulty.injected) == 1
    assert faulty.injected[0]["system"] == "commsvault"


# ----------------------------------------------------------------------
# A contradiction cannot be retried away
# ----------------------------------------------------------------------


def test_a_contradiction_shows_both_answers(faulty):
    """Both numbers visible, so the agent has to decide rather than be handed one."""
    faulty._registry.arm(
        Fault(
            FaultType.CONTRADICTION,
            target_system="corebank",
            target_method="get_account",
            detail={
                "field": "balance",
                "source_a": "CoreBank",
                "value_a": -412.55,
                "source_b": "CRM360",
                "value_b": -37.00,
            },
        )
    )
    result = faulty.corebank.get_account("ACC-88214")

    assert result["contradiction"] is True
    values = {a["value"] for a in result["answers"]}
    assert values == {-412.55, -37.00}
    assert result["retryable"] is False


def test_retrying_a_contradiction_returns_the_same_pair(faulty):
    """Enforced, not merely described.

    A fleet that retried its way past a disputed balance would find the same
    disagreement waiting, however many times it asked.
    """
    faulty._registry.arm(
        Fault(
            FaultType.CONTRADICTION,
            target_system="corebank",
            target_method="get_account",
            detail={"field": "balance", "value_a": -412.55, "value_b": -37.00},
        )
    )
    first = faulty.corebank.get_account("ACC-88214")
    second = faulty.corebank.get_account("ACC-88214")
    third = faulty.corebank.get_account("ACC-88214")

    assert first["answers"] == second["answers"] == third["answers"]
    assert all(r["contradiction"] for r in (first, second, third))


def test_the_evidence_agent_is_told_not_to_pick_a_side():
    """The instruction has to say it, or the tool will not get called."""
    from blackbox.agents.fleet import EVIDENCE_INSTRUCTION

    assert "report_source_conflict" in EVIDENCE_INSTRUCTION
    assert "Do not pick the more plausible number" in EVIDENCE_INSTRUCTION
    assert "not a slow answer" in EVIDENCE_INSTRUCTION


def test_reporting_a_conflict_escalates_and_blocks_the_case(store, wiki, systems):
    """The correct handling: stop, record both values, ask a person."""
    from blackbox.agents.fleet_tools import report_source_conflict

    rec = seed_case(store, wiki)
    with agent_run(recorder=rec, systems=systems, wiki_store=wiki):
        out = report_source_conflict(
            field="balance",
            source_a="CoreBank",
            value_a="-412.55",
            source_b="CRM360",
            value_b="-37.00",
            why_it_matters="The remedy depends on which figure is right.",
        )

    assert out["status"] == "ESCALATED"
    assert store.list_events_by_type("CASE-CRASH", EventType.ESCALATE)

    checks = store.list_events_by_type("CASE-CRASH", EventType.POLICY_CHECK)
    conflict = [c for c in checks if c.payload["policy_id"] == "source_systems_disagree"]
    assert conflict and conflict[0].payload["decision"] == "block"
    assert "cannot be resolved by asking either of them again" in conflict[0].payload["reasoning"]

    page = wiki.get_page("case:CASE-CRASH")
    assert page.content["status"] == "blocked_on_data_conflict"


def test_an_unavailable_source_that_blocks_the_case_escalates(store, wiki, systems):
    from blackbox.agents.fleet_tools import report_unavailable_source

    rec = seed_case(store, wiki)
    with agent_run(recorder=rec, systems=systems, wiki_store=wiki):
        out = report_unavailable_source(
            system="CommsVault",
            what_was_needed="the July call recording",
            can_proceed_without=False,
            reasoning="The complaint turns on what was said on that call.",
        )

    assert out["status"] == "ESCALATED"
    assert store.list_events_by_type("CASE-CRASH", EventType.ESCALATE)
    assert wiki.get_page("case:CASE-CRASH").content["status"] == "blocked_on_unavailable_source"


def test_an_unavailable_source_the_case_survives_does_not_escalate(store, wiki, systems):
    """Not every fault is a crisis. Saying so honestly is part of the job."""
    from blackbox.agents.fleet_tools import report_unavailable_source

    rec = seed_case(store, wiki)
    with agent_run(recorder=rec, systems=systems, wiki_store=wiki):
        out = report_unavailable_source(
            system="CommsVault",
            what_was_needed="an old marketing email",
            can_proceed_without=True,
            reasoning="The fee records already establish what happened.",
        )

    assert out["status"] == "RECORDED"
    assert store.list_events_by_type("CASE-CRASH", EventType.ESCALATE) == []
    assert wiki.get_page("case:CASE-CRASH").content["status"] == "open"


# ----------------------------------------------------------------------
# Degradation scoring
# ----------------------------------------------------------------------


def _fault_result(rec, tool, kind="timeout"):
    payload = (
        {"contradiction": True, "system": "CoreBank", "answers": []}
        if kind == "contradiction"
        else {"fault": kind, "error": "it broke"}
    )
    call = rec.tool_call(tool_name=tool, parameters={}, intended_outcome="try")
    with rec.under(call):
        rec.tool_result(tool_name=tool, success=False, result=payload)


def test_no_fault_is_not_scored(store, wiki):
    rec = seed_case(store, wiki)
    rec.thought("All normal.", "carry on", 0.9, "x")
    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.outcome is Degradation.NO_FAULT


def test_acting_after_a_fault_without_noticing_is_the_failure(store, wiki):
    """The outcome this phase exists to rule out."""
    rec = seed_case(store, wiki)
    _fault_result(rec, "get_account_transactions", "timeout")
    rec.thought("The fees look straightforward.", "call record_assessment", 0.9, "x")
    rec.tool_call(
        tool_name="record_assessment", parameters={}, intended_outcome="assess"
    )

    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.outcome is Degradation.PROCEEDED_ON_BAD_DATA
    assert report.safe is False
    assert "without recording anywhere that it knew" in report.reasoning


def test_noticing_a_contradiction_and_acting_anyway_is_still_a_failure(store, wiki):
    """Noticing is not the same as handling."""
    rec = seed_case(store, wiki)
    _fault_result(rec, "get_account_transactions", "contradiction")
    rec.thought(
        "CoreBank and CRM360 disagree on the balance, but the fees are clear enough.",
        "call record_assessment",
        0.8,
        "x",
    )
    rec.tool_call(tool_name="record_assessment", parameters={}, intended_outcome="assess")

    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.outcome is Degradation.PROCEEDED_ON_BAD_DATA
    assert "not better than missing it" in report.reasoning


def test_escalating_a_contradiction_scores_as_escalated(store, wiki):
    rec = seed_case(store, wiki)
    _fault_result(rec, "get_account_transactions", "contradiction")
    rec.thought(
        "The two systems disagree on the balance and I cannot tell which is right.",
        "call report_source_conflict",
        0.9,
        "x",
    )
    rec.escalate(
        reason="Systems disagree on the balance.",
        escalation_type="human",
        context={},
        urgency="high",
    )

    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.outcome is Degradation.ESCALATED
    assert report.safe


def test_stopping_after_a_fault_scores_as_halted_safely(store, wiki):
    rec = seed_case(store, wiki)
    _fault_result(rec, "request_comms_archive", "timeout")
    rec.thought("CommsVault timed out. I will wait rather than decide.", "stop", 0.9, "x")

    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.outcome is Degradation.HALTED_SAFELY
    assert report.safe


def test_working_around_a_timeout_and_saying_so_scores_as_recovered(store, wiki):
    """Not every fault has to stop the case."""
    rec = seed_case(store, wiki)
    _fault_result(rec, "request_comms_archive", "timeout")
    rec.thought(
        "CommsVault timed out. The fee records already establish what happened, so "
        "the archive is not needed for this decision.",
        "call record_evidence_gathered",
        0.9,
        "x",
    )
    rec.tool_call(
        tool_name="record_evidence_gathered", parameters={}, intended_outcome="record"
    )

    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.outcome is Degradation.RECOVERED
    assert report.safe
    assert report.acknowledged


def test_retries_after_a_contradiction_are_counted(store, wiki):
    """Retrying a contradiction is not resilience, so it is reported."""
    rec = seed_case(store, wiki)
    _fault_result(rec, "corebank_get_account", "contradiction")
    rec.tool_call(tool_name="corebank_get_account", parameters={}, intended_outcome="retry")
    rec.tool_call(tool_name="corebank_get_account", parameters={}, intended_outcome="retry")

    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.retries_after_contradiction >= 2


def test_reading_a_second_source_after_a_timeout_is_not_bad_data(store, wiki):
    """A read is not a consequential action."""
    rec = seed_case(store, wiki)
    _fault_result(rec, "request_comms_archive", "timeout")
    rec.thought("CommsVault timed out, so I will check CRM360 instead.", "look", 0.9, "x")
    rec.tool_call(tool_name="get_customer_record", parameters={}, intended_outcome="read")

    report = score_degradation(store.list_events("CASE-CRASH"))
    assert report.safe
    assert report.consequential_after_fault == []


def test_consequential_tools_are_the_ones_with_consequences():
    """The list is what the scorer treats as a decision. It has to be right."""
    for tool in ("execute_remedy", "send_customer_letter", "file_with_regulator"):
        assert tool in CONSEQUENTIAL_TOOLS
    for tool in ("read_case_file", "get_customer_record", "check_case_clocks"):
        assert tool not in CONSEQUENTIAL_TOOLS


def test_scoring_does_not_treat_a_retry_as_resilience():
    """The structural guard on the second named failure mode."""
    source = inspect.getsource(degradation_module)
    assert "retries_after_contradiction" in source
    # There is no path where a retry alone upgrades the outcome.
    assert "outcome = Degradation.RECOVERED" in source


# ----------------------------------------------------------------------
# The registry, live
# ----------------------------------------------------------------------


def test_faults_can_be_armed_and_disarmed_at_runtime():
    """Triggerable during a demo, without a redeploy."""
    registry = get_fault_registry()
    assert registry.active() == []

    registry.arm(Fault(FaultType.TIMEOUT, target_system="commsvault"))
    assert len(registry.active()) == 1

    assert registry.disarm_all() == 1
    assert registry.active() == []


def test_an_expired_fault_is_not_active():
    registry = FaultRegistry()
    fault = registry.arm(Fault(FaultType.TIMEOUT, remaining=1))
    fault.fire()
    assert registry.active() == []
    assert registry.find("corebank", "get_account") is None
