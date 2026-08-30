"""Phase 2 tests: one agent, recorded end to end.

These cover the things the Phase 2 failure modes name:

- reasoning is captured, not just inputs and outputs
- the causal tree is intact, with exactly one root and no orphans
- the agent runs through ADK rather than a hand-rolled inference loop
- the stub systems behave the way the workflow needs them to
- a redelivered message does not open a second case
"""

import pytest

from blackbox.agents.intake_service import case_id_for, run_intake
from blackbox.agents.runtime import agent_run, current_run
from blackbox.event_store import EventStore
from blackbox.fold import fold_events
from blackbox.recorder import Recorder
from blackbox.schema import EventType
from blackbox.stubs import data
from blackbox.stubs.systems import SourceSystemError

from fakes import ScriptedLlm, say, think_and_call

COMPLAINT = data.INBOUND_COMPLAINTS[0]  # The EU_IE hardship case.


def scripted_intake_model() -> ScriptedLlm:
    """A model that works the case the way the instruction asks it to."""
    return ScriptedLlm(
        [
            think_and_call(
                "The complaint is about repeated arrears fees. Before I can classify "
                "it I need to know where this customer lives and whether anyone has "
                "already recorded a vulnerability against them.",
                "lookup_customer",
                {"customer_id": COMPLAINT["customer_id"]},
            ),
            think_and_call(
                "She is resident in Ireland and CRM360 already carries a financial "
                "hardship flag. I need the account next, because the domicile may "
                "not match her residence.",
                "get_account_summary",
                {"account_id": COMPLAINT["account_id"]},
            ),
            think_and_call(
                "The account is domiciled in the UK while she is resident in "
                "Ireland. Before deciding I want to see the fees she is describing.",
                "list_fee_transactions",
                {"account_id": COMPLAINT["account_id"]},
            ),
            think_and_call(
                "She refers to a phone call in July that is not in CRM360. I will "
                "request the archive but not wait for it.",
                "request_archived_communications",
                {
                    "customer_id": COMPLAINT["customer_id"],
                    "reason": "Customer describes a July call that was not logged",
                },
            ),
            think_and_call(
                "Three arrears fees and an unpaid direct debit fee, against a "
                "customer who told us she was on reduced pay during treatment. "
                "Consumer protection follows her residence, so EU_IE governs even "
                "though the account sits in the UK.",
                "record_intake_determination",
                {
                    "category": "billing_dispute",
                    "severity": "high",
                    "jurisdiction": "EU_IE",
                    "jurisdiction_reasoning": (
                        "Customer is resident in Ireland; the account is domiciled in "
                        "the UK. Consumer protection attaches to residence, so EU_IE "
                        "rules govern."
                    ),
                    "vulnerability_indicators": True,
                    "vulnerability_reasoning": (
                        "CRM360 carries a financial hardship flag, and the narrative "
                        "discloses a health condition affecting her income."
                    ),
                    "summary": (
                        "Customer disputes three arrears management fees and one "
                        "unpaid direct debit fee charged while she was in financial "
                        "difficulty she says she disclosed by phone."
                    ),
                    "acknowledgment_due_days": 3,
                    "final_response_due_days": 56,
                },
            ),
            say("The case is open and the clock is running."),
        ]
    )


@pytest.mark.asyncio
async def test_intake_records_reasoning_not_just_actions(store, wiki, systems):
    """Gemini's rationale lands in the Diary as THOUGHT events."""
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=scripted_intake_model()
    )

    thoughts = store.list_events_by_type(result["case_id"], EventType.THOUGHT)
    assert len(thoughts) >= 5, "every model turn should leave a THOUGHT"

    reasoning = " ".join(t.payload["reasoning"] for t in thoughts)
    assert "domiciled in the UK" in reasoning
    assert "resident in Ireland" in reasoning

    # The rationale, not merely the action taken.
    assert all(t.payload["reasoning"].strip() for t in thoughts)
    assert any("record_intake_determination" in t.payload["decision"] for t in thoughts)


@pytest.mark.asyncio
async def test_causal_tree_has_one_root_and_no_orphans(store, wiki, systems):
    """The failure mode that makes the Time Machine impossible must not occur."""
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=scripted_intake_model()
    )

    recorder = Recorder(case_id=result["case_id"], actor="inspector", store=store)
    recorder.assert_causally_complete()

    events = recorder.events()
    roots = [e for e in events if e.caused_by is None]
    assert len(roots) == 1
    assert roots[0].event_type == EventType.TOOL_CALL
    assert roots[0].payload["tool_name"] == "IntakeChannel.poll"

    # Every other event names a parent that exists.
    known = {e.event_id for e in events}
    assert all(e.caused_by in known for e in events if e.caused_by is not None)


@pytest.mark.asyncio
async def test_tool_result_is_a_child_of_its_tool_call(store, wiki, systems):
    """A call and its answer are parent and child, not two loose siblings."""
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=scripted_intake_model()
    )

    calls = store.list_events_by_type(result["case_id"], EventType.TOOL_CALL)
    results = store.list_events_by_type(result["case_id"], EventType.TOOL_RESULT)
    call_ids = {c.event_id for c in calls}

    agent_results = [r for r in results if r.payload["tool_name"] != "IntakeChannel.poll"]
    assert agent_results, "the agent should have called at least one tool"
    for tool_result in agent_results:
        assert tool_result.caused_by in call_ids, (
            f"{tool_result.payload['tool_name']} result is not attached to its call"
        )


@pytest.mark.asyncio
async def test_every_tool_call_reaches_a_source_system(store, wiki, systems):
    """The tools the agent called are the ones that were recorded."""
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=scripted_intake_model()
    )
    calls = store.list_events_by_type(result["case_id"], EventType.TOOL_CALL)
    names = {c.payload["tool_name"] for c in calls}

    assert "lookup_customer" in names
    assert "get_account_summary" in names
    assert "list_fee_transactions" in names
    assert "request_archived_communications" in names
    assert "record_intake_determination" in names


@pytest.mark.asyncio
async def test_wiki_page_written_with_complete_derived_from(store, wiki, systems):
    """derived_from must list the events that produced the page, or Phase 5 breaks."""
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=scripted_intake_model()
    )

    page = wiki.get_page(f"case:{result['case_id']}")
    assert page is not None
    assert page.content["jurisdiction"] == "EU_IE"
    assert page.content["vulnerability_indicators"] is True

    recorded_ids = {e.event_id for e in store.list_events(result["case_id"])}
    assert page.derived_from, "derived_from must not be empty"
    assert set(page.derived_from).issubset(recorded_ids)
    # Every event that existed when the page was written is cited.
    assert len(page.derived_from) >= len(recorded_ids) - 1


@pytest.mark.asyncio
async def test_commsvault_does_not_answer_synchronously(store, wiki, systems):
    """The slow system must return a job id, not records."""
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=scripted_intake_model()
    )

    results = store.list_events_by_type(result["case_id"], EventType.TOOL_RESULT)
    vault = [r for r in results if r.payload["tool_name"] == "request_archived_communications"]
    assert len(vault) == 1

    payload = vault[0].payload["result"]
    assert payload["status"] == "ACCEPTED"
    assert payload["job_id"]
    assert "records" not in payload
    assert 2 <= payload["estimated_delay_days"] <= 3


@pytest.mark.asyncio
async def test_missing_determination_escalates(store, wiki, systems):
    """An agent that never opens the case escalates rather than failing silently."""
    model = ScriptedLlm([say("I am not sure what to do with this one.")])
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=model
    )

    assert result["determination"] is None
    escalations = store.list_events_by_type(result["case_id"], EventType.ESCALATE)
    assert len(escalations) == 1
    assert escalations[0].payload["urgency"] == "high"
    assert wiki.get_page(f"case:{result['case_id']}") is None


@pytest.mark.asyncio
async def test_tool_failure_is_recorded_as_a_failed_result(store, wiki, systems):
    """A tool that cannot answer records the failure instead of ending the turn."""
    model = ScriptedLlm(
        [
            think_and_call(
                "Looking up a customer id that I am not confident about.",
                "lookup_customer",
                {"customer_id": "CUST-DOES-NOT-EXIST"},
            ),
            say("That customer does not exist, so I cannot proceed."),
        ]
    )
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=model
    )

    results = store.list_events_by_type(result["case_id"], EventType.TOOL_RESULT)
    failed = [r for r in results if r.payload["tool_name"] == "lookup_customer"]
    assert len(failed) == 1
    assert failed[0].payload["success"] is False
    assert "CUST-DOES-NOT-EXIST" in failed[0].payload["error_message"]


@pytest.mark.asyncio
async def test_state_is_computed_not_stored(store, wiki, systems):
    """The fold produces the same state every time it is run over the same log."""
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=scripted_intake_model()
    )

    events = store.list_events(result["case_id"])
    first = fold_events(events)
    second = fold_events(store.list_events(result["case_id"]))

    assert first.current_status == second.current_status
    assert first.last_event_id == second.last_event_id
    assert len(first.events) == len(second.events)


def test_redelivery_does_not_open_a_second_case(store):
    """Pub/Sub delivers at least once, so the handler must notice a repeat."""
    from blackbox import ingest

    case_id = case_id_for(COMPLAINT["complaint_ref"])
    assert ingest.case_already_open(store, case_id) is False

    recorder = Recorder(case_id=case_id, actor="intake_channel", store=store)
    recorder.tool_call(
        tool_name="IntakeChannel.poll",
        parameters={"channel": "web_form"},
        intended_outcome="Collect complaints",
    )

    assert ingest.case_already_open(store, case_id) is True
    assert COMPLAINT["complaint_ref"] not in [
        c["complaint_ref"] for c in ingest.pending_complaints(store)
    ]


def test_tools_refuse_to_run_outside_a_recorded_run():
    """A tool that ran unrecorded would do real work and leave no trace."""
    from blackbox.agents.intake_tools import lookup_customer

    with pytest.raises(RuntimeError, match="unrecorded"):
        lookup_customer("CUST-4471")


def test_agent_run_context_is_scoped(recorder, systems):
    """The run context is set inside the block and cleared after it."""
    with agent_run(recorder=recorder, systems=systems) as run:
        assert current_run() is run
    with pytest.raises(RuntimeError):
        current_run()


def test_intake_agent_is_an_adk_agent():
    """Bypassing ADK for a bare inference loop is a Phase 2 failure mode."""
    from google.adk.agents import LlmAgent

    from blackbox.agents.intake_agent import build_intake_agent

    agent = build_intake_agent(model="scripted-test-model")
    assert isinstance(agent, LlmAgent)
    assert agent.after_model_callback is not None
    assert agent.before_tool_callback is not None
    assert agent.after_tool_callback is not None
    assert len(agent.tools) == 5


def test_source_systems_raise_on_unknown_ids(systems):
    """The stubs report what they do not have rather than inventing it."""
    with pytest.raises(SourceSystemError):
        systems.crm360.get_customer("CUST-NOPE")
    with pytest.raises(SourceSystemError):
        systems.corebank.get_account("ACC-NOPE")
    with pytest.raises(SourceSystemError):
        systems.commsvault.poll("CV-JOB-NOPE")


def test_third_party_pii_is_present_and_marked(systems):
    """The shorter Invisible Ink demonstration needs a real third-party name."""
    transactions = systems.corebank.get_transactions("ACC-30117")
    assert transactions["contains_third_party_pii"] is True
    named = [r for r in transactions["records"] if r.get("counterparty_name")]
    assert named
    assert named[0]["counterparty_sensitivity"] == "THIRD_PARTY_PII"


def test_append_only_backend_refuses_to_overwrite(backend):
    """The append-only guarantee is enforced by the store, not by convention."""
    from blackbox.backends import DocumentAlreadyExists

    backend.put("events", "E1", {"a": 1})
    with pytest.raises(DocumentAlreadyExists):
        backend.put("events", "E1", {"a": 2})
    assert backend.get("events", "E1") == {"a": 1}


def test_event_store_has_no_write_path_but_append(store):
    """Still true after the Phase 2 refactor."""
    for forbidden in ("update_event", "delete_event", "set_event", "patch_event"):
        assert not hasattr(store, forbidden)
