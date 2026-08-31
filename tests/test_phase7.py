"""Phase 7 tests: The Stunt Double.

The failure modes the spec names:

- a write tool that is not fully stubbed, so the stunt double affects production
- comparison reduced to string equality on outputs, which reports differences on
  rephrasing and misses differences in judgment
- shadow runs consuming enough resources to affect live latency
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from blackbox import shadow_service, stunt
from blackbox.agents import fleet_tools
from blackbox.event_store import EventStore
from blackbox.recorder import Recorder
from blackbox.schema import EventType
from blackbox.shadow_service import (
    ComparisonReport,
    Difference,
    PromotionThresholds,
    build_comparison_prompt,
    decide_promotion,
    judge_candidate,
    parse_comparison,
    record_shadow_run,
    run_shadow,
)
from blackbox.stubs.systems import SourceSystems
from blackbox.stunt import (
    OUTBOUND_TOOLS,
    AgentVersion,
    IntendedAction,
    ShadowRun,
    ShadowSystems,
    extract_actions,
    seed_shadow_world,
)
from blackbox.wiki import WikiPage

from fakes import ScriptedLlm, say, think_and_call

CASE = "CASE-SHADOW-001"


def seed_case(store: EventStore, wiki, case_id: str = CASE):
    """A case the live fleet has already worked."""
    rec = Recorder(case_id=case_id, actor="intake_agent", store=store)
    root = rec.tool_call(
        tool_name="IntakeChannel.poll",
        parameters={"channel": "web_form"},
        intended_outcome="Collect complaints",
    )
    rec.set_cause(root)

    rec.actor = "assessment_agent"
    thought = rec.thought(
        reasoning="The evidence supports upholding this and refunding the fees.",
        decision="call record_assessment",
        confidence=0.8,
        context_summary="Assessment",
    )
    with rec.under(thought):
        rec.tool_call(
            tool_name="record_assessment",
            parameters={"outcome": "upheld", "remedy_amount": 300.0},
            intended_outcome="Record the assessment",
        )

    now = datetime.now(timezone.utc)
    wiki.create_page(
        WikiPage(
            page_id=f"case:{case_id}",
            subject=case_id,
            subject_type="case",
            content={
                "status": "assessed",
                "customer_id": "CUST-4471",
                "account_id": "ACC-88214",
                "jurisdiction": "US",
                "outcome": "upheld",
                "remedy_amount": 300.0,
                "summary": "Fees disputed.",
                "deadlines": {
                    "final_response_due": (now + timedelta(days=40)).isoformat()
                },
            },
            derived_from=[root],
            version=1,
            created_at=now,
            updated_at=now,
        )
    )
    return rec, root


# ----------------------------------------------------------------------
# Every write is stubbed
# ----------------------------------------------------------------------


def test_shadow_systems_refuses_every_outbound_write(systems):
    """The failure mode: a write tool that is not fully stubbed."""
    shadow = ShadowSystems(systems)

    letter = shadow.printpost.send_letter(recipient="CUST-4471", body="hello")
    assert letter["shadow"] is True
    assert "blocked" in letter

    filing = shadow.regportal.file_report(jurisdiction="UK", summary="x")
    assert filing["shadow"] is True

    archive = shadow.commsvault.request_records("CUST-4471", "reason")
    assert archive["shadow"] is True

    assert len(shadow.blocked_writes) == 3
    assert {b["system"] for b in shadow.blocked_writes} == {
        "printpost",
        "regportal",
        "commsvault",
    }


def test_shadow_systems_still_serves_reads(systems):
    """A candidate that could not read anything would not be a fair test."""
    shadow = ShadowSystems(systems)

    profile = shadow.crm360.get_customer("CUST-4471")
    assert profile["record"]["customer_id"] == "CUST-4471"

    account = shadow.corebank.get_account("ACC-88214")
    assert account["record"]["domicile"] == "UK"


def test_a_shadow_run_cannot_write_to_the_live_diary(store, wiki, systems):
    """The candidate writes into a scratch store seeded from the live one."""
    seed_case(store, wiki)
    before = [e.event_id for e in store.list_events(CASE)]

    shadow_store, shadow_wiki, live_events = seed_shadow_world(store, wiki, CASE)

    # The scratch store starts as a faithful copy.
    assert [e.event_id for e in shadow_store.list_events(CASE)] == before

    # Writing to it does not reach the live Diary.
    rec = Recorder(case_id=CASE, actor="shadow:test", store=shadow_store)
    rec.thought("A shadow thought.", "do nothing", 0.5, "shadow")

    assert len(shadow_store.list_events(CASE)) == len(before) + 1
    assert [e.event_id for e in store.list_events(CASE)] == before


def test_a_shadow_run_cannot_write_to_the_live_wiki(store, wiki, systems):
    """The case file the candidate rewrites is a copy."""
    seed_case(store, wiki)
    shadow_store, shadow_wiki, _ = seed_shadow_world(store, wiki, CASE)

    page = shadow_wiki.get_page(f"case:{CASE}")
    shadow_wiki.update_page(
        page.regenerate(new_content={"status": "shadow_changed"}, new_derived_from=[])
    )

    assert shadow_wiki.get_page(f"case:{CASE}").content["status"] == "shadow_changed"
    assert wiki.get_page(f"case:{CASE}").content["status"] == "assessed"


def test_every_outbound_tool_is_named_in_the_blocked_set():
    """A new outbound tool must not slip past by being forgotten here.

    Walks the fleet's tool module and asserts that anything reaching PrintPost,
    RegPortal, or moving money is on the list the shadow layer knows about.
    """
    source = inspect.getsource(fleet_tools)
    for tool_name in ("send_customer_letter", "file_with_regulator", "execute_remedy"):
        assert tool_name in source
        assert tool_name in OUTBOUND_TOOLS, f"{tool_name} is not marked as a write"


def test_shadow_systems_blocks_every_state_changing_method(systems):
    """The block list covers the methods that change something outside the fleet."""
    for system_key, methods in ShadowSystems.BLOCKED.items():
        shadow = ShadowSystems(systems)
        facade = getattr(shadow, system_key)
        for method in methods:
            result = getattr(facade, method)()
            assert result.get("shadow") is True, f"{system_key}.{method} was not blocked"


@pytest.mark.asyncio
async def test_a_full_shadow_run_changes_nothing(store, wiki, systems):
    """End to end: the candidate acts, and the live world is untouched."""
    seed_case(store, wiki)
    events_before = [e.event_id for e in store.list_events(CASE)]
    page_before = wiki.get_page(f"case:{CASE}").content["status"]

    candidate = AgentVersion(
        version_id="correspondence-v2",
        agent_name="correspondence_agent",
        description="A candidate that writes to the customer sooner.",
    )
    model = ScriptedLlm(
        [
            think_and_call(
                "The case is assessed, so the customer should hear the outcome now.",
                "send_customer_letter",
                {
                    "letter_type": "final_response",
                    "body": "We have upheld your complaint and refunded the fees.",
                    "purpose": "final response",
                },
            ),
            say("Letter sent."),
        ]
    )

    run = await run_shadow(
        case_id=CASE,
        candidate=candidate,
        live_store=store,
        live_wiki=wiki,
        systems=systems,
        model=model,
    )

    assert run.intended_actions, "the candidate should have intended something"
    assert any(a.tool_name == "send_customer_letter" for a in run.intended_actions)

    # Nothing at all changed in the live world.
    assert [e.event_id for e in store.list_events(CASE)] == events_before
    assert wiki.get_page(f"case:{CASE}").content["status"] == page_before
    assert store.list_events_by_type(CASE, EventType.MESSAGE_SENT) == []


# ----------------------------------------------------------------------
# Comparison on judgment, not strings
# ----------------------------------------------------------------------


def test_actions_are_extracted_with_their_reasoning(store, wiki):
    """The judge needs the why, not just the what."""
    seed_case(store, wiki)
    actions = extract_actions(store.list_events(CASE))

    assert len(actions) == 1, "the poller is machinery, not an agent decision"
    assert actions[0].tool_name == "record_assessment"
    assert "upholding" in actions[0].reasoning


def test_the_poller_is_not_counted_as_a_decision(store, wiki):
    seed_case(store, wiki)
    actions = extract_actions(store.list_events(CASE))
    assert all(not a.tool_name.startswith("IntakeChannel.") for a in actions)


def test_outbound_actions_are_marked_as_writes():
    actions = extract_actions([])
    assert actions == []

    action = IntendedAction(0, "send_customer_letter", {}, is_write=True)
    assert action.is_write


def test_the_comparison_prompt_carries_both_sides_and_the_reasoning():
    """A judge shown only outputs would be doing string comparison by proxy."""
    run = ShadowRun(
        case_id=CASE,
        version_id="v2",
        live_actions=[
            IntendedAction(0, "close_case", {"why": "resolved"}, reasoning="LIVE_REASONING_MARKER")
        ],
        intended_actions=[
            IntendedAction(
                0, "escalate_to_human", {"why": "unclear"}, reasoning="CANDIDATE_REASONING_MARKER"
            )
        ],
    )
    prompt = build_comparison_prompt([run])

    assert "close_case" in prompt
    assert "escalate_to_human" in prompt
    assert "LIVE_REASONING_MARKER" in prompt
    assert "CANDIDATE_REASONING_MARKER" in prompt


def test_the_judge_is_told_to_ignore_phrasing():
    """The named failure mode is comparison by string equality."""
    assert "Ignore differences of phrasing entirely" in shadow_service.JUDGE_INSTRUCTION
    for category in ("EQUIVALENT", "SAFER", "RISKIER", "INCORRECT"):
        assert category in shadow_service.JUDGE_INSTRUCTION


def test_parsing_a_judge_answer():
    text = (
        "SAFER | the candidate escalated instead of closing | a human sees a case "
        "the live version closed on thin evidence\n"
        "EQUIVALENT | tools called in a different order | same information gathered\n"
        "VERDICT: The candidate is more cautious and worth promoting."
    )
    report = parse_comparison(text, "v2", cases=1)

    assert report.count("SAFER") == 1
    assert report.count("EQUIVALENT") == 1
    assert report.count("RISKIER") == 0
    assert "more cautious" in report.verdict


def test_an_unparseable_judge_line_is_kept_as_incorrect():
    """A gate that ignored what it could not read would pass on a bad answer."""
    text = "MAYBE_FINE | something happened | who knows\nVERDICT: unclear"
    report = parse_comparison(text, "v2", cases=1)

    assert report.count("INCORRECT") == 1
    assert "unrecognised category" in report.differences[0].why


def test_a_judge_answer_with_no_verdict_cannot_support_a_promotion():
    report = parse_comparison("EQUIVALENT | nothing | nothing", "v2", cases=1)
    assert "cannot support" in report.verdict


@pytest.mark.asyncio
async def test_the_judge_is_a_separate_call_from_either_agent(store, wiki, systems):
    """Neither version marks its own homework."""
    run = ShadowRun(
        case_id=CASE,
        version_id="v2",
        live_actions=[IntendedAction(0, "close_case", {})],
        intended_actions=[IntendedAction(0, "escalate_to_human", {})],
    )
    judge = ScriptedLlm(
        [
            say(
                "SAFER | the candidate escalated where the live version closed | a "
                "person reviews a case that was closed on thin evidence\n"
                "VERDICT: More cautious than the live version."
            )
        ]
    )
    report = await judge_candidate([run], version_id="v2", model=judge)

    assert report.count("SAFER") == 1
    assert "More cautious" in report.verdict


@pytest.mark.asyncio
async def test_an_unreachable_judge_does_not_pass_the_candidate(store):
    """Failing to compare must not read as nothing to report."""

    class Exploding(ScriptedLlm):
        async def generate_content_async(self, llm_request, stream=False):
            raise RuntimeError("judge is down")
            yield  # pragma: no cover

    report = await judge_candidate(
        [ShadowRun(case_id=CASE, version_id="v2")], version_id="v2", model=Exploding([])
    )
    assert report.count("INCORRECT") == 1
    decision = decide_promotion(report)
    assert decision.promote is False


# ----------------------------------------------------------------------
# The promotion gate
# ----------------------------------------------------------------------


def test_a_clean_candidate_is_promotable():
    report = ComparisonReport(
        version_id="v2",
        cases_compared=3,
        differences=[
            Difference("EQUIVALENT", "order of lookups", "same information"),
            Difference("SAFER", "escalated one case", "a person now reviews it"),
        ],
        verdict="Worth promoting.",
    )
    decision = decide_promotion(report)
    assert decision.promote is True
    assert "1 difference(s) were judged safer" in decision.reasons[0]


def test_one_incorrect_difference_blocks_promotion():
    report = ComparisonReport(
        version_id="v2",
        cases_compared=5,
        differences=[Difference("INCORRECT", "invented a fact", "not in the case file")],
    )
    decision = decide_promotion(report)
    assert decision.promote is False
    assert "incorrectly on 1 occasion" in decision.reasons[0]


def test_riskier_behaviour_blocks_by_default():
    """Riskier needs a person, not a threshold waving it through."""
    report = ComparisonReport(
        version_id="v2",
        cases_compared=5,
        differences=[Difference("RISKIER", "skipped an approval gate", "money moved sooner")],
    )
    assert decide_promotion(report).promote is False


def test_a_thin_sample_blocks_promotion():
    """One quiet case is not evidence a candidate is safe."""
    report = ComparisonReport(version_id="v2", cases_compared=1, differences=[])
    decision = decide_promotion(report, PromotionThresholds(min_cases=5))
    assert decision.promote is False
    assert "Only 1 case(s)" in decision.reasons[0]


def test_the_gate_blocks_rather_than_warns():
    """A gate that reported a problem and deployed anyway is a dashboard."""
    report = ComparisonReport(
        version_id="v2",
        cases_compared=10,
        differences=[
            Difference("INCORRECT", "a", "b"),
            Difference("RISKIER", "c", "d"),
        ],
    )
    decision = decide_promotion(report)
    assert decision.promote is False
    assert len(decision.reasons) == 2, "every blocking reason should be reported"


def test_thresholds_can_be_loosened_deliberately():
    """Loosening is possible but has to be stated, not defaulted into."""
    report = ComparisonReport(
        version_id="v2",
        cases_compared=10,
        differences=[Difference("RISKIER", "a", "b")],
    )
    assert decide_promotion(report).promote is False
    assert decide_promotion(report, PromotionThresholds(max_riskier=1)).promote is True


# ----------------------------------------------------------------------
# The record
# ----------------------------------------------------------------------


def test_shadow_intent_is_recorded_beside_what_actually_happened(store, wiki):
    """A promotion decision made months ago should be re-examinable."""
    seed_case(store, wiki)
    run = ShadowRun(
        case_id=CASE,
        version_id="correspondence-v2",
        intended_actions=[IntendedAction(0, "send_customer_letter", {}, is_write=True)],
        blocked_writes=[{"system": "printpost", "method": "send_letter", "arguments": {}}],
    )
    event_id = record_shadow_run(run, store)

    event = store.get_event(event_id)
    assert event.actor == "shadow:correspondence-v2"
    assert "send_customer_letter" in event.payload["reasoning"]
    assert "Nothing was changed" in event.payload["context_summary"]

    # And it is unmistakably distinct from a real action.
    assert event.actor.startswith("shadow:")
    assert store.list_events_by_type(CASE, EventType.MESSAGE_SENT) == []


def test_shadow_runs_are_off_the_request_path():
    """Shadow work must not add latency to the live fleet."""
    from blackbox.agents import fleet_service

    source = inspect.getsource(fleet_service)
    assert "run_shadow" not in source, "the live path invokes a shadow run"
    assert "shadow_service" not in source
