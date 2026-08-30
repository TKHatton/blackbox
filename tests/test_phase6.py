"""Phase 6 tests: The Time Machine.

The failure modes the spec names, in order of how much damage each would do:

- fixtures that fall through to live tool calls when a recording is missing,
  which mutates production data during a replay. The spec calls this the most
  dangerous defect possible in this build.
- replay that reads current state instead of state as-of the rewind point.
  Contaminated replay produces confident nonsense.
- only the divergent decision surfaced, without the downstream consequences.
- policies still hard-coded, forcing a fake replay that only re-renders the
  original run.
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from blackbox import gateway, replay as replay_module, timemachine
from blackbox.agents.runtime import agent_run
from blackbox.divergence import (
    DECISION_TYPES,
    build_recorded_turns,
    compare_runs,
    decision_signature,
)
from blackbox.event_store import EventStore
from blackbox.gateway import Destination, DisclosureRequest, apply_rules
from blackbox.labels import Label, Sensitivity
from blackbox.policy import (
    DEFAULT_POLICIES,
    PolicyEngine,
    PolicyError,
    PolicyRule,
    PolicySet,
)
from blackbox.recorder import Recorder
from blackbox.replay import ReplayMode, replay_case
from blackbox.schema import EventType
from blackbox.timemachine import (
    FixtureMiss,
    FixtureSystems,
    ReplayViolation,
    build_fixtures,
    state_as_of,
    wiki_as_of,
)
from blackbox.wiki import WikiPage

from fakes import ScriptedLlm, say

S = Sensitivity


# ----------------------------------------------------------------------
# Policies as data
# ----------------------------------------------------------------------


def test_the_gate_threshold_is_data_not_code():
    """If it were a constant in a module, this phase would be impossible."""
    assert DEFAULT_POLICIES.constants["gate_a_threshold"] == 500.0

    tightened = DEFAULT_POLICIES.with_constants(gate_a_threshold=100.0)
    assert tightened.constants["gate_a_threshold"] == 100.0
    # The original is untouched: amending a policy makes a new set.
    assert DEFAULT_POLICIES.constants["gate_a_threshold"] == 500.0
    assert tightened.version != DEFAULT_POLICIES.version


def test_the_same_case_gates_differently_under_a_different_threshold():
    """The headline lever, at the level of a single rule."""
    context = {"remedy_amount": 300.0, "looks_systemic": False}

    live = PolicyEngine(DEFAULT_POLICIES)
    assert live.evaluate("gate_a_monetary_threshold", context).fired is False

    tightened = PolicyEngine(DEFAULT_POLICIES.with_constants(gate_a_threshold=100.0))
    assert tightened.evaluate("gate_a_monetary_threshold", context).fired is True


def test_a_rule_that_cannot_be_evaluated_raises_rather_than_returning_false():
    """False means 'this restriction does not apply'.

    A broken rule quietly meaning that is how a governance system develops a hole
    nobody can see.
    """
    engine = PolicyEngine(DEFAULT_POLICIES)
    with pytest.raises(PolicyError):
        engine.evaluate("gate_a_monetary_threshold", {})  # no remedy_amount


def test_a_rule_that_will_not_compile_raises():
    broken = PolicySet(
        policy_set_id="broken",
        version="1",
        description="",
        rules=(
            PolicyRule(
                rule_id="nonsense",
                description="",
                expression="this is not (( cel",
                effect="block",
                reason="",
                category="disclosure",
            ),
        ),
    )
    with pytest.raises(PolicyError, match="will not compile"):
        PolicyEngine(broken).evaluate("nonsense", {})


def test_the_gateway_blocks_when_a_policy_cannot_be_evaluated(store):
    """The outbound path treats an unevaluable rule as a block, not an allow."""
    import asyncio

    from blackbox.gateway import check_disclosure

    broken = PolicySet(
        policy_set_id="broken",
        version="1",
        description="",
        constants={},
        rules=(
            PolicyRule(
                rule_id="needs_missing_var",
                description="",
                expression="something_never_supplied",
                effect="block",
                reason="",
                category="disclosure",
            ),
        ),
    )
    recorder = Recorder(case_id="CASE-BROKENPOLICY", actor="x", store=store)
    request = DisclosureRequest(
        content="anything",
        label=Label.make([S.PII], ["US"], []),
        destination=Destination.CUSTOMER,
        destination_system="PrintPost",
        recipient="CUST-1",
        purpose="test",
        case_id="CASE-BROKENPOLICY",
    )
    verdict = asyncio.run(
        check_disclosure(request, recorder=recorder, engine=PolicyEngine(broken))
    )
    assert verdict.decision.value == "block"
    assert verdict.rule_id == "policy_evaluation_failed"


def test_the_gateway_rules_live_in_the_policy_set_not_in_python():
    """A rule compiled into the module could not be swapped at replay time."""
    source = inspect.getsource(gateway)
    assert "_RULES = [" not in source, "the gateway still holds a hard-coded rule list"
    assert "evaluate_category(\"disclosure\"" in source

    ids = {r.rule_id for r in DEFAULT_POLICIES.rules_in("disclosure")}
    assert "special_category_third_country_transfer" in ids
    assert "pii_high_never_leaves_the_bank" in ids
    assert "third_party_pii_not_to_complainant" in ids


def test_a_policy_set_survives_a_round_trip():
    """Policies are data, so they must serialize."""
    restored = PolicySet.from_dict(DEFAULT_POLICIES.to_dict())
    assert restored.constants == DEFAULT_POLICIES.constants
    assert len(restored.rules) == len(DEFAULT_POLICIES.rules)


def test_the_gateway_reaches_a_different_verdict_under_an_amended_rule():
    """Swapping the policy changes the answer without touching the gateway."""
    label = Label.make([S.SPECIAL_CATEGORY], ["EU_IE"], [])
    request = DisclosureRequest(
        content="A letter.",
        label=label,
        destination=Destination.CUSTOMER,
        destination_system="PrintPost",
        recipient="CUST-1",
        purpose="final response",
        case_id="CASE-X",
    )

    assert apply_rules(request, engine=PolicyEngine(DEFAULT_POLICIES)) is not None

    # A world where the US is treated as adequate. The code is identical.
    relaxed = DEFAULT_POLICIES.with_constants(adequate_regions=["EU", "EEA", "US"])
    assert apply_rules(request, engine=PolicyEngine(relaxed)) is None


# ----------------------------------------------------------------------
# State as-of
# ----------------------------------------------------------------------


def seed_case(store: EventStore, case_id: str = "CASE-TM"):
    """A recorded run with a decision in the middle of it."""
    rec = Recorder(case_id=case_id, actor="intake_agent", store=store)
    root = rec.tool_call(
        tool_name="IntakeChannel.poll", parameters={}, intended_outcome="collect"
    )
    rec.set_cause(root)

    early = rec.memory_write(
        memory_key=f"wiki:case:{case_id}",
        content={"status": "open", "outcome": None, "_version": {"new_version": 1}},
        reason="Case opened",
    )
    thought = rec.thought(
        reasoning="Deciding the outcome.",
        decision="call record_assessment",
        confidence=0.8,
        context_summary="assessment",
    )
    with rec.under(thought):
        rec.tool_call(
            tool_name="record_assessment",
            parameters={
                "outcome": "upheld",
                "reasoning": "internal",
                "proposed_remedy": "Refund",
                "remedy_amount": 300.0,
                "looks_systemic": False,
                "systemic_reasoning": "isolated",
            },
            intended_outcome="record the assessment",
        )
    late = rec.memory_write(
        memory_key=f"wiki:case:{case_id}",
        content={"status": "closed", "outcome": "upheld", "_version": {"new_version": 2}},
        reason="Case closed",
    )
    return rec, root, early, thought, late


def test_state_as_of_excludes_everything_after_the_rewind_point(store):
    """The whole basis of a replay that is not contaminated."""
    rec, root, early, thought, late = seed_case(store)
    world = state_as_of(store, "CASE-TM", early)

    ids = [e.event_id for e in world.events]
    assert root in ids
    assert early in ids
    assert thought not in ids, "the replay window includes events from after the rewind"
    assert late not in ids


def test_the_wiki_is_reconstructed_not_read(store, wiki):
    """Reading today's page would show the replay the answer it is deciding."""
    rec, root, early, thought, late = seed_case(store)

    # The live Wiki says closed. The world as-of the early write must not.
    wiki.create_page(
        WikiPage(
            page_id="case:CASE-TM",
            subject="CASE-TM",
            subject_type="case",
            content={"status": "closed", "outcome": "upheld"},
            derived_from=[root],
            version=9,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    world = state_as_of(store, "CASE-TM", early)
    page = world.page("case:CASE-TM")

    assert page is not None
    assert page["status"] == "open", "the replay saw current state, not state as-of"
    assert page["outcome"] is None

    # And the module never reads a live Wiki page at all.
    source = inspect.getsource(timemachine)
    assert "wiki_store" not in source
    assert "get_page" not in source


def test_version_bookkeeping_is_not_mistaken_for_page_content(store):
    """A MEMORY_WRITE carries both; only one of them is the page."""
    rec, root, early, thought, late = seed_case(store)
    world = state_as_of(store, "CASE-TM", late)
    page = world.page("case:CASE-TM")
    assert "_version" not in page
    assert page["status"] == "closed"


def test_rewinding_to_a_foreign_event_is_refused(store):
    """It would build a plausible world out of the wrong history."""
    seed_case(store, "CASE-A")
    _, other_root, _, _, _ = seed_case(store, "CASE-B")

    with pytest.raises(ValueError, match="not part of case"):
        state_as_of(store, "CASE-A", other_root)


def test_wiki_as_of_skips_writes_with_no_content():
    """A gap in the recording is not an empty page."""
    assert wiki_as_of([]) == {}


# ----------------------------------------------------------------------
# Fixtures: the dangerous part
# ----------------------------------------------------------------------


def test_a_fixture_miss_raises_and_never_falls_through(store):
    """The most dangerous defect possible in this build, tested directly."""
    seed_case(store)
    fixtures = build_fixtures(store.list_events("CASE-TM"))

    with pytest.raises(FixtureMiss, match="may not call a live system"):
        fixtures.tool_result("CoreBank.get_transactions", {"account_id": "ACC-1"})

    assert fixtures.misses, "a miss must be reported, not silently absorbed"


def test_the_replay_systems_object_cannot_reach_anything():
    """Not a flag on the real systems. A different class, with no clients.

    There is nothing to disable because there is nothing there.
    """
    from blackbox.stubs.systems import SourceSystems

    fixtures = build_fixtures([])
    systems = FixtureSystems(fixtures)

    assert not isinstance(systems, SourceSystems)

    source = inspect.getsource(timemachine)
    for forbidden in ("import requests", "httpx", "firestore.Client", "pubsub_v1", "urllib"):
        assert forbidden not in source, f"the replay layer imports {forbidden}"


def test_outbound_systems_refuse_during_a_replay():
    """A replay of a refund case must not queue the letter again."""
    systems = FixtureSystems(build_fixtures([]))

    with pytest.raises(ReplayViolation, match="nothing was sent"):
        systems.printpost.send_letter(recipient="CUST-1", body="hello")

    with pytest.raises(ReplayViolation):
        systems.regportal.file_report(jurisdiction="UK", summary="x")

    # The attempt is reported, because that is the interesting part.
    assert len(systems.attempted_outbound) == 2
    assert systems.attempted_outbound[0]["system"] == "PrintPost"


def test_fixtures_serve_recorded_results(store):
    seed_case(store)
    fixtures = build_fixtures(store.list_events("CASE-TM"))
    assert fixtures.model_turns, "the recording should yield model turns"


def test_the_recorded_model_runs_out_rather_than_inventing_a_turn():
    """A replay that went further than the original is a result, not a gap to fill."""
    import asyncio

    from blackbox.divergence import RecordedLlm

    model = RecordedLlm([])

    async def drain():
        async for _ in model.generate_content_async(None):
            pass

    with pytest.raises(FixtureMiss, match="has none left"):
        asyncio.run(drain())


def test_recorded_turns_carry_the_original_tool_arguments(store):
    """Fast mode must reach the same tools with the same inputs."""
    seed_case(store)
    events = store.list_events("CASE-TM")
    turns = build_recorded_turns(events)

    calls = [
        part.function_call
        for turn in turns
        for part in turn.parts
        if getattr(part, "function_call", None)
    ]
    assert any(c.name == "record_assessment" for c in calls)
    assessment = next(c for c in calls if c.name == "record_assessment")
    assert assessment.args["remedy_amount"] == 300.0


# ----------------------------------------------------------------------
# Divergence
# ----------------------------------------------------------------------


def test_runs_are_compared_on_decisions_not_wording(store):
    """Rephrasing is not divergence."""
    rec_a = Recorder(case_id="CASE-W1", actor="a", store=store)
    a1 = rec_a.thought("One wording entirely.", "call x", 0.5, "c")
    rec_a.tool_call(tool_name="x", parameters={}, intended_outcome="o")

    rec_b = Recorder(case_id="CASE-W2", actor="a", store=store)
    rec_b.thought("Completely different words here.", "call x", 0.5, "c")
    rec_b.tool_call(tool_name="x", parameters={}, intended_outcome="different text")

    divergence = compare_runs(
        store.list_events("CASE-W1"), store.list_events("CASE-W2")
    )
    assert divergence.diverged is False


def test_a_different_policy_decision_is_a_divergence(store):
    rec_a = Recorder(case_id="CASE-D1", actor="a", store=store)
    rec_a.policy_check("gate_a", "approval_threshold", {}, "allow", "under threshold")

    rec_b = Recorder(case_id="CASE-D2", actor="a", store=store)
    rec_b.policy_check("gate_a", "approval_threshold", {}, "escalate", "over threshold")

    divergence = compare_runs(
        store.list_events("CASE-D1"), store.list_events("CASE-D2")
    )
    assert divergence.diverged
    assert divergence.first_difference_index == 0
    assert "different decision" in divergence.explanation


def test_downstream_consequences_are_reported_not_just_the_split(store):
    """Surfacing only the divergent decision undersells the feature."""
    rec_a = Recorder(case_id="CASE-C1", actor="a", store=store)
    rec_a.policy_check("gate_a", "approval_threshold", {}, "allow", "")
    rec_a.tool_call(tool_name="execute_remedy", parameters={}, intended_outcome="pay")
    rec_a.message_sent("CUST-1", "post", "letter", "final response")

    rec_b = Recorder(case_id="CASE-C2", actor="a", store=store)
    rec_b.policy_check("gate_a", "approval_threshold", {}, "escalate", "")
    rec_b.record(
        EventType.SUSPEND,
        {
            "reason": "awaiting approval",
            "wake_condition": {
                "type": "approval_received",
                "resume_agent": "assessment_agent",
                "description": "gate A",
                "earliest_wake_at": None,
                "parameters": {},
            },
            "state_snapshot": {},
        },
    )

    divergence = compare_runs(
        store.list_events("CASE-C1"), store.list_events("CASE-C2")
    )
    assert divergence.diverged
    assert len(divergence.downstream) >= 2, "the consequences after the split are missing"

    # The letter that went out originally does not go out in the replay.
    replayed = [d["in_replay"] for d in divergence.downstream]
    assert None in replayed or all(r != ("MESSAGE_SENT", "final response") for r in replayed)


def test_decision_signature_ignores_reason_text(store):
    rec = Recorder(case_id="CASE-SIG", actor="a", store=store)
    one = rec.policy_check("p", "t", {}, "block", "because of one thing")
    two = rec.policy_check("p", "t", {}, "block", "because of something else")
    events = store.list_events("CASE-SIG")
    assert decision_signature(events[0]) == decision_signature(events[1])


def test_thought_events_are_not_decision_types():
    assert EventType.THOUGHT not in DECISION_TYPES


# ----------------------------------------------------------------------
# A whole replay
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replaying_under_a_tighter_threshold_fires_a_gate_that_did_not(store, wiki):
    """The headline. Same case, same code, one number changed.

    Originally a 300 remedy was under the 500 threshold and no approval was
    needed. Under a 100 threshold the same case needs sign-off, and the case
    that sailed through now waits on a human.
    """
    rec, root, early, thought, late = seed_case(store)

    result = await replay_case(
        store=store,
        case_id="CASE-TM",
        rewind_to=early,
        policies=DEFAULT_POLICIES.with_constants(gate_a_threshold=100.0),
        mode=ReplayMode.FAST,
        original_policy_version="1.0.0",
    )

    assert result.mode is ReplayMode.FAST
    assert "gate_a_threshold=100.0" in result.policy_version

    gate_checks = [
        e
        for e in result.replayed_events
        if e.event_type == EventType.POLICY_CHECK
        and e.payload.get("policy_id") == "gate_a_monetary_threshold"
    ]
    assert gate_checks, "the replay never evaluated the gate"
    assert gate_checks[0].payload["decision"] == "escalate", (
        "a 300 remedy should escalate under a 100 threshold"
    )
    assert gate_checks[0].payload["input_data"]["threshold"] == 100.0


@pytest.mark.asyncio
async def test_replaying_under_the_same_policy_does_not_diverge(store, wiki):
    """The control. If this diverged, the machinery would be the cause."""
    rec, root, early, thought, late = seed_case(store)

    result = await replay_case(
        store=store,
        case_id="CASE-TM",
        rewind_to=early,
        policies=DEFAULT_POLICIES,
        mode=ReplayMode.FAST,
    )
    gate_checks = [
        e
        for e in result.replayed_events
        if e.event_type == EventType.POLICY_CHECK
        and e.payload.get("policy_id") == "gate_a_monetary_threshold"
    ]
    assert gate_checks
    assert gate_checks[0].payload["decision"] == "allow"


@pytest.mark.asyncio
async def test_a_replay_never_writes_to_the_real_diary(store, wiki):
    """The Diary is append-only, and a replay did not happen."""
    rec, root, early, thought, late = seed_case(store)
    before = [e.event_id for e in store.list_events("CASE-TM")]

    await replay_case(
        store=store,
        case_id="CASE-TM",
        rewind_to=early,
        policies=DEFAULT_POLICIES.with_constants(gate_a_threshold=100.0),
    )

    after = [e.event_id for e in store.list_events("CASE-TM")]
    assert before == after, "the replay appended to the real Diary"


@pytest.mark.asyncio
async def test_a_replay_reports_which_policy_it_ran_under(store, wiki):
    """A divergence report that cannot name its policy proves nothing."""
    rec, root, early, thought, late = seed_case(store)
    result = await replay_case(
        store=store,
        case_id="CASE-TM",
        rewind_to=early,
        policies=DEFAULT_POLICIES.with_constants(gate_a_threshold=100.0),
        original_policy_version="1.0.0",
    )
    report = result.to_dict()
    assert report["original_policy_version"] == "1.0.0"
    assert "100.0" in report["policy_version"]
    assert report["outbound_attempts_blocked"] == [] or isinstance(
        report["outbound_attempts_blocked"], list
    )


def test_replay_module_never_constructs_the_live_source_systems():
    """Structural guard on the one mistake that would reach production."""
    source = inspect.getsource(replay_module)
    assert "get_source_systems" not in source
    assert "SourceSystems(" not in source
    assert "FixtureSystems(" in source
