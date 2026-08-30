"""Phase 3 tests: the fleet wakes itself up.

Organised around the failure modes the spec names, because those are the things
that would pass a casual demo and break in month two:

- wake conditions stored in process memory rather than as events
- context rehydration that loses detail, so a resumed agent repeats or
  contradicts itself
- polling loops disguised as autonomy
- one supervisor hard-coding the sequence
"""

from datetime import datetime, timedelta, timezone

import pytest

from blackbox import wake
from blackbox.agents.fleet import (
    SPECIALIST_BUILDERS,
    build_coordinator,
    build_specialist,
)
from blackbox.agents.fleet_service import resume_case
from blackbox.agents.rehydrate import (
    ContextUnavailable,
    rebuild_context,
    verify_context_sufficient,
)
from blackbox.agents.runtime import agent_run
from blackbox.approvals import handle_approval, handle_customer_reply
from blackbox.event_store import EventStore
from blackbox.heartbeat import evaluate_wake_condition, run_heartbeat
from blackbox.recorder import Recorder
from blackbox.schema import EventType
from blackbox.stubs.systems import SourceSystems
from blackbox.wake import (
    WakeCondition,
    WakeConditionType,
    find_open_suspensions,
    find_unparseable_suspensions,
)
from blackbox.wiki import WikiPage

from fakes import ScriptedLlm, say, think_and_call

CASE = "CASE-P3-001"


def seed_case(store: EventStore, wiki, case_id: str = CASE, **content_overrides) -> Recorder:
    """A case that has been through intake and has a Wiki page."""
    recorder = Recorder(case_id=case_id, actor="intake_agent", store=store)
    root = recorder.tool_call(
        tool_name="IntakeChannel.poll",
        parameters={"channel": "web_form"},
        intended_outcome="Collect complaints",
    )
    recorder.set_cause(root)

    now = datetime.now(timezone.utc)
    content = {
        "status": "open",
        "customer_id": "CUST-4471",
        "account_id": "ACC-88214",
        "category": "billing_dispute",
        "severity": "high",
        "jurisdiction": "EU_IE",
        "jurisdiction_reasoning": "Resident in Ireland, account domiciled in the UK.",
        "vulnerability_indicators": True,
        "summary": "Three arrears fees disputed.",
        "deadlines": {
            "acknowledgment_due": (now + timedelta(days=3)).isoformat(),
            "final_response_due": (now + timedelta(days=56)).isoformat(),
        },
    }
    content.update(content_overrides)

    wiki.create_page(
        WikiPage(
            page_id=f"case:{case_id}",
            subject=case_id,
            subject_type="case",
            content=content,
            derived_from=[root],
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    return recorder


# ----------------------------------------------------------------------
# Wake conditions live in the log, not in memory
# ----------------------------------------------------------------------


def test_wake_condition_survives_total_process_loss(store, wiki):
    """A brand new store instance finds the same pending work.

    This is the test for the failure mode "wake conditions stored in process
    memory rather than as events". Nothing is shared between the writer and the
    reader except the Diary.
    """
    recorder = seed_case(store, wiki)
    ready = datetime.now(timezone.utc) + timedelta(days=2)
    recorder.suspend(
        reason="Waiting on CommsVault",
        condition=wake.batch_job_condition(
            "evidence_agent", "CV-JOB-1", ready, "archived call recordings"
        ),
    )

    # A different EventStore object over the same backend: a fresh instance.
    reborn = EventStore(project_id="blackbox-test", backend=store._backend)
    found = find_open_suspensions(reborn)

    assert len(found) == 1
    assert found[0].case_id == CASE
    assert found[0].condition.resume_agent == "evidence_agent"
    assert found[0].condition.parameters["job_id"] == "CV-JOB-1"
    assert found[0].condition.earliest_wake_at is not None


def test_resume_closes_the_suspension(store, wiki):
    """A SUSPEND with a RESUME pointing at it is no longer open."""
    recorder = seed_case(store, wiki)
    suspend_id = recorder.suspend(
        reason="Waiting",
        condition=wake.approval_condition("assessment_agent", "A", "APR-1", "gate A"),
    )
    assert len(find_open_suspensions(store)) == 1

    recorder.resume(
        suspend_event_id=suspend_id,
        reason="Approved",
        wake_trigger={"source": "test"},
    )
    assert find_open_suspensions(store) == []


def test_resume_is_caused_by_its_suspend(store, wiki):
    """The link is what closes the wait, so it must be the causal parent."""
    recorder = seed_case(store, wiki)
    suspend_id = recorder.suspend(
        reason="Waiting",
        condition=wake.approval_condition("assessment_agent", "A", "APR-1", "gate A"),
    )
    resume_id = recorder.resume(suspend_id, "Approved", {"source": "test"})

    resume_event = store.get_event(resume_id)
    assert resume_event.caused_by == suspend_id
    # Work after a resume continues from the resume, not from before the wait.
    assert recorder.current_cause == resume_id


def test_unparseable_suspension_is_reported_not_swallowed(store, wiki):
    """A case that can never wake must be visible, not silently skipped."""
    recorder = seed_case(store, wiki)
    recorder.record(
        EventType.SUSPEND,
        {
            "reason": "Waiting on something",
            "wake_condition": {"type": "not_a_real_condition_type"},
            "state_snapshot": {},
        },
    )

    assert find_open_suspensions(store) == []
    broken = find_unparseable_suspensions(store)
    assert len(broken) == 1


def test_fold_reports_the_wait(store, wiki):
    """The folded state shows a suspended case as waiting."""
    recorder = seed_case(store, wiki)
    recorder.suspend(
        reason="Waiting on CommsVault",
        condition=wake.batch_job_condition(
            "evidence_agent", "CV-JOB-1", datetime.now(timezone.utc), "records"
        ),
    )
    state = recorder.state()
    assert state.current_status == "waiting"
    assert len(state.pending_actions) == 1


# ----------------------------------------------------------------------
# Wake decisions
# ----------------------------------------------------------------------


def test_batch_job_does_not_wake_before_it_is_ready(store, wiki, systems):
    """The heartbeat must not resume a case whose records do not exist yet."""
    recorder = seed_case(store, wiki)
    job = systems.commsvault.request_records("CUST-4471", "test")
    recorder.suspend(
        reason="Waiting on CommsVault",
        condition=wake.batch_job_condition(
            "evidence_agent", job["job_id"], datetime.fromisoformat(job["ready_at"]), "records"
        ),
    )

    suspension = find_open_suspensions(store)[0]
    decision = evaluate_wake_condition(suspension, systems, now=datetime.now(timezone.utc))
    assert decision.wake is False
    assert "Not due yet" in decision.reasoning


def test_batch_job_wakes_once_ready(store, wiki, systems):
    """Same suspension, evaluated later, now wakes and carries the records."""
    recorder = seed_case(store, wiki)
    job = systems.commsvault.request_records("CUST-4471", "test")
    recorder.suspend(
        reason="Waiting on CommsVault",
        condition=wake.batch_job_condition(
            "evidence_agent", job["job_id"], datetime.fromisoformat(job["ready_at"]), "records"
        ),
    )

    suspension = find_open_suspensions(store)[0]
    later = datetime.now(timezone.utc) + timedelta(days=5)
    decision = evaluate_wake_condition(suspension, systems, now=later)

    assert decision.wake is True
    assert decision.trigger["source"] == "commsvault"
    assert decision.trigger["records"]


def test_approval_never_wakes_from_the_heartbeat(store, wiki, systems):
    """An approval cannot be polled into existence, so time alone must not wake it."""
    recorder = seed_case(store, wiki)
    recorder.suspend(
        reason="Waiting on gate A",
        condition=wake.approval_condition("assessment_agent", "A", "APR-1", "gate A"),
    )

    suspension = find_open_suspensions(store)[0]
    far_future = datetime.now(timezone.utc) + timedelta(days=400)
    decision = evaluate_wake_condition(suspension, systems, now=far_future)

    assert decision.wake is False
    assert "cannot be polled" in decision.reasoning


def test_wake_evaluation_is_recorded_with_reasoning(store, wiki, systems):
    """Why a case woke on Thursday and not Wednesday must be answerable."""
    recorder = seed_case(store, wiki)
    recorder.suspend(
        reason="Waiting on gate A",
        condition=wake.approval_condition("assessment_agent", "A", "APR-1", "gate A"),
    )

    import asyncio

    asyncio.run(run_heartbeat(store=store, wiki_store=wiki, systems=systems))

    checks = store.list_events_by_type(CASE, EventType.POLICY_CHECK)
    wake_checks = [c for c in checks if c.payload["check_type"] == "wake_condition"]
    assert len(wake_checks) == 1
    assert wake_checks[0].payload["decision"] == "block"
    assert wake_checks[0].payload["reasoning"]


def test_heartbeat_starts_no_work_of_its_own(store, wiki, systems):
    """With nothing suspended and no deadline near, a beat does nothing.

    This is what separates a heartbeat from a polling loop: it has no opinion
    about any case and cannot begin work no agent asked for.
    """
    seed_case(store, wiki)

    import asyncio

    result = asyncio.run(run_heartbeat(store=store, wiki_store=wiki, systems=systems))

    assert result["open_suspensions"] == 0
    assert result["resumed"] == []
    assert result["compliance_reviews"] == []


# ----------------------------------------------------------------------
# Context rehydration
# ----------------------------------------------------------------------


def test_briefing_is_built_from_wiki_and_fold_only(store, wiki):
    """The resumed agent reads derived memory, never the raw Diary."""
    recorder = seed_case(store, wiki)
    for i in range(5):
        recorder.thought(
            reasoning=f"UNIQUE_DIARY_MARKER_{i} that must not reach the briefing",
            decision="think",
            confidence=0.5,
            context_summary="noise",
        )

    context = rebuild_context(CASE, store=store, wiki_store=wiki)
    briefing = context.to_briefing()

    for i in range(5):
        assert f"UNIQUE_DIARY_MARKER_{i}" not in briefing
    # But the Wiki content is there.
    assert "EU_IE" in briefing
    assert "billing_dispute" in briefing


def test_briefing_marks_earlier_decisions_as_settled(store, wiki):
    """Guards against the resumed agent contradicting its earlier self."""
    seed_case(store, wiki, outcome="upheld", remedy_amount=105.0)

    context = rebuild_context(CASE, store=store, wiki_store=wiki)
    settled = context.decisions_already_made()
    briefing = context.to_briefing()

    assert settled["jurisdiction"] == "EU_IE"
    assert settled["outcome"] == "upheld"
    assert "Do not revisit" in briefing
    assert "Contradicting them" in briefing


def test_resume_refuses_without_a_wiki_page(store, wiki, systems):
    """An agent resuming onto a blank sheet would improvise. It must not run."""
    recorder = Recorder(case_id="CASE-NOPAGE", actor="intake_agent", store=store)
    root = recorder.tool_call(
        tool_name="IntakeChannel.poll", parameters={}, intended_outcome="collect"
    )
    recorder.set_cause(root)
    recorder.suspend(
        reason="Waiting",
        condition=wake.approval_condition("assessment_agent", "A", "APR-X", "gate A"),
    )

    suspension = find_open_suspensions(store)[0]

    import asyncio

    result = asyncio.run(
        resume_case(
            suspension=suspension,
            trigger={"source": "test"},
            store=store,
            wiki_store=wiki,
            systems=systems,
            model=ScriptedLlm([say("should never run")]),
        )
    )

    assert result["resumed"] is False
    # The suspension is still open, so the case is stuck rather than lost.
    assert len(find_open_suspensions(store)) == 1
    escalations = store.list_events_by_type("CASE-NOPAGE", EventType.ESCALATE)
    assert len(escalations) == 1


def test_empty_wiki_page_is_not_sufficient_context(store, wiki):
    """A page that exists but says nothing must not pass the check."""
    now = datetime.now(timezone.utc)
    recorder = Recorder(case_id="CASE-EMPTY", actor="x", store=store)
    recorder.tool_call(tool_name="t", parameters={}, intended_outcome="o")
    wiki.create_page(
        WikiPage(
            page_id="case:CASE-EMPTY",
            subject="CASE-EMPTY",
            subject_type="case",
            content={},
            derived_from=["E1"],
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    context = rebuild_context("CASE-EMPTY", store=store, wiki_store=wiki, require_page=False)
    with pytest.raises(ContextUnavailable, match="empty"):
        verify_context_sufficient(context)


# ----------------------------------------------------------------------
# The full wait, end to end
# ----------------------------------------------------------------------


def test_agent_suspends_then_resumes_days_later(store, wiki, systems):
    """The property Phase 3 exists to demonstrate.

    An agent stops, nothing stays resident, and days later a different run picks
    the case up with full context and carries on.
    """
    import asyncio

    seed_case(store, wiki)
    job = systems.commsvault.request_records("CUST-4471", "seeded")

    # Day 0: the Evidence Agent decides it must wait.
    recorder = Recorder(case_id=CASE, actor="evidence_agent", store=store)
    events = recorder.events()
    recorder.set_cause(events[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        from blackbox.agents.fleet_tools import suspend_until_evidence_ready

        out = suspend_until_evidence_ready(
            job_id=job["job_id"],
            ready_at=job["ready_at"],
            what_you_are_waiting_for="the July call the customer described",
        )
    assert out["status"] == "SUSPENDED"
    assert len(find_open_suspensions(store)) == 1

    # Day 5: a heartbeat on a fresh store instance.
    reborn = EventStore(project_id="blackbox-test", backend=store._backend)
    resumed_model = ScriptedLlm(
        [
            think_and_call(
                "The archive is back. The July call confirms she disclosed her "
                "circumstances, which nobody logged. That is enough to assess on.",
                "record_evidence_gathered",
                {
                    "summary": "CommsVault confirms a July call disclosing hardship "
                    "that was never recorded in CRM360.",
                    "sufficient_to_assess": True,
                    "outstanding_items": "",
                },
            ),
            say("Evidence recorded."),
        ]
    )

    result = asyncio.run(
        run_heartbeat(
            store=reborn,
            wiki_store=wiki,
            systems=systems,
            model=resumed_model,
            now=datetime.now(timezone.utc) + timedelta(days=5),
        )
    )

    assert result["open_suspensions"] == 1
    assert len(result["resumed"]) == 1
    assert result["resumed"][0]["resumed"] is True
    assert result["resumed"][0]["resumed_agent"] == "evidence_agent"

    # The wait is closed and the case moved on.
    assert find_open_suspensions(store) == []
    page = wiki.get_page(f"case:{CASE}")
    assert page.content["evidence_sufficient"] is True
    assert "never recorded in CRM360" in page.content["evidence_summary"]

    resumes = store.list_events_by_type(CASE, EventType.RESUME)
    assert len(resumes) == 1
    assert resumes[0].payload["state_restored"] is True


def test_approval_arriving_wakes_the_case(store, wiki, systems):
    """An approval is the wake condition being met, not something polled for."""
    import asyncio

    seed_case(store, wiki, gate_a_required=True, remedy_amount=1200.0, outcome="upheld")

    recorder = Recorder(case_id=CASE, actor="assessment_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        from blackbox.agents.fleet_tools import suspend_until_approved

        suspend_until_approved(gate="A", what_is_being_approved="Refund of 1200 EUR")

    assert len(find_open_suspensions(store)) == 1

    model = ScriptedLlm(
        [
            think_and_call(
                "Gate A is approved, so the remedy can be executed.",
                "read_case_file",
                {},
            ),
            say("Approval noted. Handing on for remediation."),
        ]
    )
    result = asyncio.run(
        handle_approval(
            {
                "case_id": CASE,
                "gate": "A",
                "approved": True,
                "approver": "adjudicator_kim",
                "note": "Fees were charged after a disclosed hardship.",
            },
            store=store,
            wiki_store=wiki,
            systems=systems,
            model=model,
        )
    )

    assert result["resumed"] is True
    assert find_open_suspensions(store) == []
    page = wiki.get_page(f"case:{CASE}")
    assert page.content["gate_a_approved"] is True
    assert page.content["gate_a_approver"] == "adjudicator_kim"


def test_customer_reply_cuts_the_appeal_window_short(store, wiki, systems):
    """The conditional wake: a 30 day sleep ended early by an arriving event."""
    import asyncio

    seed_case(store, wiki, status="awaiting_appeal_window")
    recorder = Recorder(case_id=CASE, actor="correspondence_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        from blackbox.agents.fleet_tools import suspend_for_appeal_window

        out = suspend_for_appeal_window()

    assert out["status"] == "SUSPENDED"
    suspension = find_open_suspensions(store)[0]
    # It would not have woken on its own for 30 days.
    decision = evaluate_wake_condition(suspension, systems, now=datetime.now(timezone.utc))
    assert decision.wake is False

    model = ScriptedLlm([say("The customer has replied, so the case is reopened.")])
    result = asyncio.run(
        handle_customer_reply(
            {"case_id": CASE, "message": "I do not accept this outcome."},
            store=store,
            wiki_store=wiki,
            systems=systems,
            model=model,
        )
    )

    assert result["resumed"] is True
    assert find_open_suspensions(store) == []


def test_unmatched_approval_is_recorded_and_resumes_nothing(store, wiki, systems):
    """A duplicate approval must not restart a case that already moved on."""
    import asyncio

    seed_case(store, wiki)
    result = asyncio.run(
        handle_approval(
            {"case_id": CASE, "gate": "A", "approved": True},
            store=store,
            wiki_store=wiki,
            systems=systems,
        )
    )
    assert result["resumed"] is False
    checks = store.list_events_by_type(CASE, EventType.POLICY_CHECK)
    assert any(
        c.payload["policy_id"] == "approval_without_matching_suspension" for c in checks
    )


# ----------------------------------------------------------------------
# Guards that survive a persuasive model
# ----------------------------------------------------------------------


def test_remediation_refuses_without_approval(store, wiki, systems):
    """The gate is enforced in code, not only in the prompt."""
    seed_case(store, wiki, gate_a_required=True, remedy_amount=900.0)

    recorder = Recorder(case_id=CASE, actor="remediation_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        from blackbox.agents.fleet_tools import execute_remedy

        out = execute_remedy("ACC-88214", 900.0, "Refund of arrears fees")

    assert out["executed"] is False
    assert "Gate A approval is required" in out["error"]

    blocks = [
        c
        for c in store.list_events_by_type(CASE, EventType.POLICY_CHECK)
        if c.payload["decision"] == "block"
    ]
    assert blocks


def test_systemic_flag_blocks_customer_contact(store, wiki, systems):
    """Gate B stops any customer-facing statement until Compliance signs off."""
    seed_case(store, wiki, gate_b_required=True, systemic_flag=True)

    recorder = Recorder(case_id=CASE, actor="correspondence_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        from blackbox.agents.fleet_tools import send_customer_letter

        out = send_customer_letter("final_response", "Dear customer...", "final response")

    assert out["sent"] is False
    assert "systemic" in out["error"]
    assert store.list_events_by_type(CASE, EventType.MESSAGE_SENT) == []


def test_assessment_sets_gates_from_the_threshold(store, wiki, systems):
    """Gate A fires above the threshold and not at or below it."""
    from blackbox.agents.fleet_tools import GATE_A_THRESHOLD, record_assessment

    seed_case(store, wiki)
    recorder = Recorder(case_id=CASE, actor="assessment_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)

    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        below = record_assessment(
            outcome="upheld",
            reasoning="internal only",
            proposed_remedy="Refund one fee",
            remedy_amount=GATE_A_THRESHOLD,
            looks_systemic=False,
            systemic_reasoning="Isolated to this customer.",
        )
    assert below["gate_a_required"] is False

    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        above = record_assessment(
            outcome="upheld",
            reasoning="internal only",
            proposed_remedy="Refund all fees",
            remedy_amount=GATE_A_THRESHOLD + 0.01,
            looks_systemic=False,
            systemic_reasoning="Isolated to this customer.",
        )
    assert above["gate_a_required"] is True


def test_internal_reasoning_is_marked_and_stays_off_the_letter(store, wiki, systems):
    """Assessment reasoning is labelled internal and never reaches the customer."""
    from blackbox.agents.fleet_tools import record_assessment, send_customer_letter

    seed_case(store, wiki)
    recorder = Recorder(case_id=CASE, actor="assessment_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)

    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        record_assessment(
            outcome="upheld",
            reasoning="SECRET_INTERNAL_NOTE: the branch mishandled this.",
            proposed_remedy="Refund",
            remedy_amount=105.0,
            looks_systemic=False,
            systemic_reasoning="Isolated.",
        )
        send_customer_letter(
            "final_response",
            "We are sorry. We have refunded the fees.",
            "final response",
        )

    page = wiki.get_page(f"case:{CASE}")
    assert page.content["assessment_reasoning_visibility"] == "INTERNAL_ONLY"

    sent = store.list_events_by_type(CASE, EventType.MESSAGE_SENT)
    assert len(sent) == 1
    assert "SECRET_INTERNAL_NOTE" not in sent[0].payload["content"]


# ----------------------------------------------------------------------
# Routing is judgment, not a switch statement
# ----------------------------------------------------------------------


def test_coordinator_routes_through_adk_sub_agents():
    """The five specialists are sub-agents, so ADK carries the transfer."""
    coordinator = build_coordinator(model="scripted-test-model")
    names = {a.name for a in coordinator.sub_agents}
    assert names == set(SPECIALIST_BUILDERS)
    # The coordinator cannot do the work itself: it has one read tool.
    assert len(coordinator.tools) == 1


def test_no_hard_coded_sequence_exists_in_the_fleet():
    """Guards against the fleet quietly becoming a pipeline.

    If a later change introduces an ordered list of agents and steps through it,
    this is the test that should fail.
    """
    import inspect

    from blackbox.agents import fleet, fleet_service

    for module in (fleet, fleet_service):
        source = inspect.getsource(module)
        # A literal ordering of the specialists would be the giveaway.
        assert "evidence_agent\", \"assessment_agent" not in source
        assert "next_agent =" not in source


def test_only_remediation_can_move_money():
    """One agent has the money tool. The others are not given it."""
    money_tool = "execute_remedy"
    for name in SPECIALIST_BUILDERS:
        agent = build_specialist(name, model="scripted-test-model")
        tool_names = {getattr(t, "name", getattr(t, "__name__", "")) for t in agent.tools}
        if name == "remediation_agent":
            assert money_tool in tool_names
        else:
            assert money_tool not in tool_names, f"{name} can move money"


def test_every_specialist_writes_to_the_recorder():
    """No agent can act unrecorded."""
    for name in SPECIALIST_BUILDERS:
        agent = build_specialist(name, model="scripted-test-model")
        assert agent.after_model_callback is not None, name
        assert agent.before_tool_callback is not None, name
        assert agent.after_tool_callback is not None, name


def test_build_specialist_rejects_unknown_agents():
    with pytest.raises(ValueError, match="Unknown agent"):
        build_specialist("marketing_agent")


# ----------------------------------------------------------------------
# Compliance Officer acts without being asked
# ----------------------------------------------------------------------


def test_compliance_officer_picks_up_a_case_near_its_deadline(store, wiki, systems):
    """Nobody asks. A clock gets close and the case comes up for review."""
    import asyncio

    now = datetime.now(timezone.utc)
    seed_case(
        store,
        wiki,
        deadlines={
            "acknowledgment_due": (now - timedelta(days=50)).isoformat(),
            "final_response_due": (now + timedelta(days=5)).isoformat(),
        },
    )

    # The coordinator routes first, then the specialist acts. Both turns come
    # from the same scripted model, in the order the fleet actually runs them.
    model = ScriptedLlm(
        [
            think_and_call(
                "Five days from the final response deadline with no outcome and no "
                "holding letter. That is a compliance question, not an evidence or "
                "assessment one, so it goes to the compliance officer.",
                "transfer_to_agent",
                {"agent_name": "compliance_officer"},
            ),
            think_and_call(
                "This case is five days from its final response deadline with no "
                "answer and no holding letter, and the customer is flagged as "
                "vulnerable, which makes the delay worse rather than neutral.",
                "instruct_holding_letter",
                {
                    "why": "Five days from the eight week deadline with no outcome, "
                    "and the customer is in financial hardship."
                },
            ),
            say("Holding letter instructed."),
        ]
    )

    result = asyncio.run(
        run_heartbeat(store=store, wiki_store=wiki, systems=systems, model=model)
    )

    assert len(result["compliance_reviews"]) == 1
    review = result["compliance_reviews"][0]
    # Routing actually happened: the coordinator handed off rather than acting.
    assert "compliance_officer" in review["agents_that_acted"]

    page = wiki.get_page(f"case:{CASE}")
    assert page.content["holding_letter_required"] is True

    # The routing decision itself is in the record, with its reasoning.
    thoughts = store.list_events_by_type(CASE, EventType.THOUGHT)
    routing = [t for t in thoughts if "transfer_to_agent" in t.payload["decision"]]
    assert routing, "the decision of who should act must be recorded"
    assert "compliance question" in routing[0].payload["reasoning"]


def test_compliance_leaves_healthy_cases_alone(store, wiki, systems):
    """A case that is progressing does not get intervened with for its own sake."""
    import asyncio

    now = datetime.now(timezone.utc)
    seed_case(
        store,
        wiki,
        deadlines={
            "acknowledgment_due": (now + timedelta(days=2)).isoformat(),
            "final_response_due": (now + timedelta(days=50)).isoformat(),
        },
    )
    result = asyncio.run(run_heartbeat(store=store, wiki_store=wiki, systems=systems))
    assert result["compliance_reviews"] == []


def test_causal_tree_stays_intact_across_a_suspend_and_resume(store, wiki, systems):
    """A wait must not orphan the events on the far side of it."""
    import asyncio

    seed_case(store, wiki)
    job = systems.commsvault.request_records("CUST-4471", "seeded")

    recorder = Recorder(case_id=CASE, actor="evidence_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        from blackbox.agents.fleet_tools import suspend_until_evidence_ready

        suspend_until_evidence_ready(job["job_id"], job["ready_at"], "records")

    asyncio.run(
        run_heartbeat(
            store=store,
            wiki_store=wiki,
            systems=systems,
            model=ScriptedLlm([say("Nothing further needed.")]),
            now=datetime.now(timezone.utc) + timedelta(days=5),
        )
    )

    inspector = Recorder(case_id=CASE, actor="inspector", store=store)
    inspector.assert_causally_complete()
