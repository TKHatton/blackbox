"""Phase 4 tests: Invisible Ink.

The failure modes the spec names, in order of how badly each one would matter:

- labels attached to fields but not propagating through model output, so the
  stamp washes off at the first summarisation. This is the whole feature.
- combining labels by taking the first or the loosest instead of the strictest.
- a keyword or regex filter wearing the Invisible Ink name.
- checks placed only at the obvious exit and not at every outbound path.
"""

import inspect

import pytest

from blackbox import gateway
from blackbox.agents import fleet_tools
from blackbox.agents.intake_service import run_intake
from blackbox.agents.runtime import agent_run
from blackbox.gateway import (
    Decision,
    Destination,
    DisclosureRequest,
    apply_rules,
    check_disclosure,
    parse_judge_response,
)
from blackbox.labels import (
    Label,
    Provenance,
    Sensitivity,
    implies,
    join_all,
    lattice_is_well_formed,
    reduce_classes,
)
from blackbox.recorder import Recorder
from blackbox.schema import EventType
from blackbox.stubs import data
from blackbox.taint import blocked_disclosures, summarise_path, taint_path

from fakes import ScriptedLlm, say, think_and_call

S = Sensitivity
EU_COMPLAINT = data.INBOUND_COMPLAINTS[0]  # the EU_IE hardship case
US_COMPLAINT = data.INBOUND_COMPLAINTS[1]  # the US_CA disputed transaction


# ----------------------------------------------------------------------
# The lattice, and the combination rule
# ----------------------------------------------------------------------


def test_lattice_is_a_partial_order():
    """A cycle here would make reduce_classes drop a restriction it should keep."""
    ok, why = lattice_is_well_formed()
    assert ok, why


def test_join_is_a_lattice_join():
    """Commutative, associative, idempotent, with the public label as identity.

    Without these, "combine two labels" would depend on the order they happened
    to arrive in, and two runs over the same data could reach different verdicts.
    """
    labels = [
        Label.make([S.PII], ["EU_IE"], [Provenance("CRM360", "name")]),
        Label.make([S.SPECIAL_CATEGORY], ["UK"], [Provenance("Intake", "narrative")]),
        Label.make([S.INTERNAL_ONLY]),
        Label.make([S.THIRD_PARTY_PII], [], [], "retain_1_year"),
        Label.public(),
    ]
    for a in labels:
        assert a.join(a) == a, "not idempotent"
        assert a.join(Label.public()) == a, "public is not the identity"
        for b in labels:
            assert a.join(b) == b.join(a), "not commutative"
            for c in labels:
                assert a.join(b).join(c) == a.join(b.join(c)), "not associative"


def test_join_never_loosens():
    """The direction that matters. Combining moves towards more restrictive."""
    a = Label.make([S.PII], ["EU_IE"], [Provenance("CRM360", "name")], "retain_6_years")
    b = Label.make([S.INTERNAL_ONLY], ["UK"], [Provenance("Assessment", "note")], "retain_1_year")
    joined = a.join(b)

    assert joined.dominates(a)
    assert joined.dominates(b)
    assert a.jurisdictions <= joined.jurisdictions
    assert b.jurisdictions <= joined.jurisdictions
    assert a.origins <= joined.origins
    assert joined.retention == "retain_1_year", "retention must take the shorter window"


def test_incomparable_classes_are_both_kept():
    """The bug a single scalar sensitivity would cause.

    INTERNAL_ONLY and PII_HIGH restrict different things. Collapsing them onto
    one axis silently discards whichever loses.
    """
    joined = Label.make([S.INTERNAL_ONLY]).join(Label.make([S.PII_HIGH]))
    assert joined.classes == frozenset({S.INTERNAL_ONLY, S.PII_HIGH})
    assert joined.has(S.INTERNAL_ONLY)
    assert joined.has(S.PII_HIGH)


def test_implied_classes_are_absorbed_without_losing_the_restriction():
    """{PII, SPECIAL_CATEGORY} reduces, but still answers yes to has(PII)."""
    reduced = reduce_classes([S.PII, S.SPECIAL_CATEGORY])
    assert reduced == frozenset({S.SPECIAL_CATEGORY})

    label = Label.make([S.PII, S.SPECIAL_CATEGORY])
    assert label.has(S.PII), "absorbing PII must not drop the PII restriction"
    assert label.has(S.SPECIAL_CATEGORY)


def test_has_is_not_set_membership():
    """A check written as `in label.classes` would miss absorbed restrictions."""
    label = Label.make([S.SPECIAL_CATEGORY])
    assert S.PII not in label.classes
    assert label.has(S.PII)


def test_no_order_invented_between_unrelated_classes():
    """PII_HIGH, SPECIAL_CATEGORY and INTERNAL_ONLY are mutually incomparable."""
    for a, b in [
        (S.PII_HIGH, S.SPECIAL_CATEGORY),
        (S.PII_HIGH, S.INTERNAL_ONLY),
        (S.SPECIAL_CATEGORY, S.INTERNAL_ONLY),
        (S.THIRD_PARTY_PII, S.SPECIAL_CATEGORY),
    ]:
        assert not implies(a, b), f"{a} should not imply {b}"
        assert not implies(b, a), f"{b} should not imply {a}"


def test_empty_label_is_public_not_empty():
    assert Label.make([]).classes == frozenset({S.PUBLIC})
    assert join_all([]).is_public


def test_label_survives_a_round_trip_through_storage():
    """Labels are stored on events, so they must reconstruct exactly."""
    label = Label.make(
        [S.SPECIAL_CATEGORY, S.THIRD_PARTY_PII],
        ["EU_IE", "UK"],
        [Provenance("CRM360", "vulnerability_flags", "E1", "hardship")],
        "retain_1_year",
    )
    assert Label.from_dict(label.to_dict()) == label


# ----------------------------------------------------------------------
# Propagation: the stamp does not wash off
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_survives_summarisation_through_the_model(store, wiki, systems):
    """The failure mode that would leave nothing of this feature.

    The Intake Agent reads a narrative mentioning a diagnosis and writes a
    structured determination that shares almost no words with it. The
    SPECIAL_CATEGORY class has to be on the resulting events anyway.
    """
    model = ScriptedLlm(
        [
            think_and_call(
                "She mentions a health condition affecting her income. I need her "
                "profile.",
                "lookup_customer",
                {"customer_id": EU_COMPLAINT["customer_id"]},
            ),
            think_and_call(
                "CRM360 carries a hardship flag. Resident in Ireland.",
                "record_intake_determination",
                {
                    "category": "billing_dispute",
                    "severity": "high",
                    "jurisdiction": "EU_IE",
                    "jurisdiction_reasoning": "Resident in Ireland.",
                    "vulnerability_indicators": True,
                    "vulnerability_reasoning": "Reduced income during a period of ill health.",
                    "summary": "Arrears fees charged during financial difficulty.",
                    "acknowledgment_due_days": 3,
                    "final_response_due_days": 56,
                },
            ),
            say("Case opened."),
        ]
    )
    result = await run_intake(
        EU_COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=model
    )

    thoughts = store.list_events_by_type(result["case_id"], EventType.THOUGHT)
    final_thought = Label.from_dict(thoughts[-1].labels)

    assert final_thought.has(S.SPECIAL_CATEGORY), "the stamp washed off at the model"
    assert "EU_IE" in final_thought.jurisdictions
    assert final_thought.origins, "the trail back must survive too"


@pytest.mark.asyncio
async def test_every_event_after_the_source_carries_the_label(store, wiki, systems):
    """Once special category enters a run, it stays on everything after it."""
    model = ScriptedLlm(
        [
            think_and_call(
                "Checking the customer.",
                "lookup_customer",
                {"customer_id": EU_COMPLAINT["customer_id"]},
            ),
            think_and_call(
                "Now the account.",
                "get_account_summary",
                {"account_id": EU_COMPLAINT["account_id"]},
            ),
            say("Done looking."),
        ]
    )
    result = await run_intake(
        EU_COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=model
    )

    events = store.list_events(result["case_id"])
    crm_index = next(
        i
        for i, e in enumerate(events)
        if e.event_type == EventType.TOOL_RESULT
        and e.payload.get("tool_name") == "lookup_customer"
    )
    after = [e for e in events[crm_index:] if e.labels]
    assert after, "events after the source must carry labels"
    assert all(
        Label.from_dict(e.labels).has(S.SPECIAL_CATEGORY) for e in after
    ), "the label came off partway down the run"


def test_third_party_pii_enters_from_a_transaction_record(systems):
    """CoreBank naming another customer is what makes the second demo work."""
    from blackbox.propagation import label_corebank_transactions

    response = systems.corebank.get_transactions("ACC-30117")
    label = label_corebank_transactions(response, event_id="E1")

    assert label.has(S.THIRD_PARTY_PII)
    assert any("counterparty_name" in o.field for o in label.origins)


def test_unclassified_tool_output_is_not_assumed_harmless():
    """A result nobody labelled is not something to put in a letter."""
    from blackbox.propagation import label_for_tool_result

    label = label_for_tool_result("some_new_tool", {"anything": "at all"})
    assert label.has(S.INTERNAL_ONLY)
    assert not label.is_public


# ----------------------------------------------------------------------
# The gateway
# ----------------------------------------------------------------------


def _request(label: Label, destination_system: str = "PrintPost", **kwargs) -> DisclosureRequest:
    defaults = dict(
        content="We are sorry about the difficult time you have had this year.",
        label=label,
        destination=Destination.CUSTOMER,
        destination_system=destination_system,
        recipient="CUST-4471",
        purpose="final response",
        case_id="CASE-TEST",
    )
    defaults.update(kwargs)
    return DisclosureRequest(**defaults)


def test_special_category_to_a_third_country_is_blocked():
    """The headline rule. All four conditions have to hold."""
    label = Label.make(
        [S.SPECIAL_CATEGORY],
        ["EU_IE"],
        [Provenance("Intake", "complaint_narrative", "E1")],
    )
    verdict = apply_rules(_request(label))

    assert verdict is not None
    assert verdict.decision == Decision.BLOCK
    assert verdict.rule_id == "special_category_third_country_transfer"
    assert "US" in verdict.reasoning


def test_the_same_data_to_an_eu_destination_is_not_blocked_by_that_rule():
    """The rule is about the border, not about the data alone."""
    label = Label.make([S.SPECIAL_CATEGORY], ["EU_IE"], [])
    verdict = apply_rules(
        _request(label, destination_system="RegPortal", destination=Destination.REGULATOR)
    )
    assert verdict is None


def test_special_category_from_an_unrestricted_jurisdiction_passes_the_border_rule():
    """A US customer's data going to a US vendor crosses no border."""
    label = Label.make([S.SPECIAL_CATEGORY], ["US_CA"], [])
    verdict = apply_rules(_request(label))
    assert verdict is None


def test_third_party_pii_to_the_complainant_is_blocked():
    """The shorter demonstration: another customer's name in a letter."""
    label = Label.make(
        [S.THIRD_PARTY_PII],
        ["US_CA"],
        [Provenance("CoreBank", "counterparty_name", "E9", "names a third party")],
    )
    verdict = apply_rules(_request(label))

    assert verdict is not None
    assert verdict.rule_id == "third_party_pii_not_to_complainant"


def test_national_identifier_never_leaves_the_bank():
    label = Label.make([S.PII_HIGH], ["UK"], [])
    verdict = apply_rules(_request(label))
    assert verdict is not None
    assert verdict.rule_id == "pii_high_never_leaves_the_bank"


def test_all_grounds_are_reported_not_just_the_first():
    """Content that trips three rules should say so."""
    label = Label.make([S.PII_HIGH, S.SPECIAL_CATEGORY, S.THIRD_PARTY_PII], ["EU_IE"], [])
    verdict = apply_rules(_request(label))
    assert verdict is not None
    assert len(verdict.considered) == 3


def test_internal_only_is_not_decided_by_rule():
    """Every letter is derived from the assessment, so a rule would block them all."""
    label = Label.make([S.INTERNAL_ONLY], ["US"], [])
    assert apply_rules(_request(label)) is None


@pytest.mark.asyncio
async def test_gemini_judges_what_the_rules_leave_open(store):
    """The ambiguous case goes to the model, and its reasoning is the basis."""
    recorder = Recorder(case_id="CASE-JUDGE", actor="correspondence_agent", store=store)
    label = Label.make([S.INTERNAL_ONLY], ["US"], [Provenance("Assessment", "reasoning")])
    judge = ScriptedLlm(
        [say("BLOCK: This repeats the internal file note rather than stating the outcome.")]
    )

    verdict = await check_disclosure(_request(label), recorder=recorder, model=judge)

    assert verdict.decision == Decision.BLOCK
    assert verdict.judged_by == "gemini"
    assert "file note" in verdict.reasoning

    checks = store.list_events_by_type("CASE-JUDGE", EventType.POLICY_CHECK)
    assert len(checks) == 1
    assert checks[0].payload["input_data"]["judged_by"] == "gemini"
    assert checks[0].payload["reasoning"] == verdict.reasoning


@pytest.mark.asyncio
async def test_an_unparseable_judge_answer_blocks(store):
    """A gateway that fails open would be worse than no gateway."""
    recorder = Recorder(case_id="CASE-CONFUSED", actor="x", store=store)
    label = Label.make([S.PII], ["US"], [])
    judge = ScriptedLlm([say("I am not sure how to answer that.")])

    verdict = await check_disclosure(_request(label), recorder=recorder, model=judge)
    assert verdict.decision == Decision.BLOCK
    assert verdict.rule_id == "gemini_judge_unparseable"


def test_judge_response_parsing():
    assert parse_judge_response("ALLOW: fine").decision == Decision.ALLOW
    assert parse_judge_response("BLOCK: no").decision == Decision.BLOCK
    assert parse_judge_response("").decision == Decision.BLOCK
    assert parse_judge_response("maybe?").decision == Decision.BLOCK


@pytest.mark.asyncio
async def test_allows_are_recorded_too(store):
    """A gateway that logs only refusals cannot answer 'was this ever checked'."""
    recorder = Recorder(case_id="CASE-ALLOW", actor="x", store=store)
    verdict = await check_disclosure(
        _request(Label.public()), recorder=recorder, use_judge=False
    )
    assert verdict.allowed
    checks = store.list_events_by_type("CASE-ALLOW", EventType.POLICY_CHECK)
    assert len(checks) == 1
    assert checks[0].payload["decision"] == "allow"


def test_every_outbound_tool_goes_through_the_gateway():
    """Checks placed only at the obvious exit is a named failure mode."""
    outbound = ["send_customer_letter", "file_with_regulator"]
    for name in outbound:
        source = inspect.getsource(getattr(fleet_tools, name))
        assert "check_disclosure" in source, f"{name} bypasses the gateway"
        assert "verdict.allowed" in source, f"{name} does not act on the verdict"


# ----------------------------------------------------------------------
# The headline: four hops, no keyword a filter could match
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_four_hop_block_and_its_trail(store, wiki, systems):
    """An empathetic letter with no medical vocabulary, blocked and explained.

    Hop 0: the customer writes about a diagnosis.
    Hop 1: Intake extracts vulnerability indicators from that prose.
    Hop 2: CRM360 confirms a hardship flag.
    Hop 3: the Correspondence Agent paraphrases it warmly.
    Then the gateway refuses the transfer to a US-based vendor.
    """
    intake_model = ScriptedLlm(
        [
            think_and_call(
                "She describes a health condition affecting her income.",
                "lookup_customer",
                {"customer_id": EU_COMPLAINT["customer_id"]},
            ),
            think_and_call(
                "Resident in Ireland, hardship already flagged.",
                "record_intake_determination",
                {
                    "category": "billing_dispute",
                    "severity": "high",
                    "jurisdiction": "EU_IE",
                    "jurisdiction_reasoning": "Resident in Ireland.",
                    "vulnerability_indicators": True,
                    "vulnerability_reasoning": "Reduced income through a period of treatment.",
                    "summary": "Arrears fees during financial difficulty.",
                    "acknowledgment_due_days": 3,
                    "final_response_due_days": 56,
                },
            ),
            say("Case opened."),
        ]
    )
    result = await run_intake(
        EU_COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=intake_model
    )
    case_id = result["case_id"]

    # The letter. Warm, ordinary, and containing no medical word at all.
    letter = (
        "Dear Ms Brennan,\n\n"
        "Thank you for writing to us, and I am sorry that this has come at what "
        "sounds like an already difficult time for you.\n\n"
        "We have refunded the fees in full."
    )

    recorder = Recorder(case_id=case_id, actor="correspondence_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki) as run:
        run.absorb(
            Label.make(
                [S.SPECIAL_CATEGORY],
                ["EU_IE"],
                [Provenance("Intake", "vulnerability_indicators", None)],
            )
        )
        out = await fleet_tools.send_customer_letter("final_response", letter, "final response")

    assert out["sent"] is False
    assert out["blocked_by"] == "special_category_third_country_transfer"

    # No keyword filter could have caught this.
    for word in [
        "cancer", "diagnos", "medical", "health", "illness", "treatment",
        "hospital", "disease", "condition", "sick",
    ]:
        assert word not in letter.lower(), f"the letter contains {word!r}"

    # Nothing was sent.
    assert store.list_events_by_type(case_id, EventType.MESSAGE_SENT) == []

    # And the block is explainable.
    path = taint_path(store, out["policy_check_event_id"])
    assert path["found"]
    assert "SPECIAL_CATEGORY" in path["final_classes"]
    assert "EU_IE" in path["final_jurisdictions"]
    assert path["hop_count"] >= 4, f"expected a multi-hop trail, got {path['hop_count']}"

    # The trail reaches the customer's own words.
    narratives = [h for h in path["hops"] if h["source_text"]]
    assert narratives, "the trail must reach back to the original text"
    assert "cancer" in narratives[0]["source_text"].lower()

    # And it names the hop where the restriction attached.
    assert "SPECIAL_CATEGORY" in path["restrictions_attached_at"]

    rendered = summarise_path(path)
    assert "Taint path" in rendered
    assert "SPECIAL_CATEGORY" in rendered


@pytest.mark.asyncio
async def test_blocked_disclosures_are_listable(store, wiki, systems):
    """What did this system stop, and why."""
    recorder = Recorder(case_id="CASE-BLOCKS", actor="correspondence_agent", store=store)
    recorder.tool_call(tool_name="root", parameters={}, intended_outcome="start")
    label = Label.make([S.PII_HIGH], ["UK"], [Provenance("CRM360", "national_identifier")])

    await check_disclosure(_request(label, case_id="CASE-BLOCKS"), recorder=recorder)

    blocks = blocked_disclosures(store, "CASE-BLOCKS")
    assert len(blocks) == 1
    assert blocks[0]["rule"] == "pii_high_never_leaves_the_bank"
    assert blocks[0]["destination_region"] == "US"


def test_gateway_is_not_a_keyword_filter():
    """If Gemini is not making the judgment, the differentiator is gone.

    The rules never read the content. They read the label. Two pieces of text
    with identical wording get different verdicts when their derivations differ,
    which is the property no regex can have.
    """
    identical_text = "We are sorry for the difficulty you have experienced."

    tainted = _request(
        Label.make([S.SPECIAL_CATEGORY], ["EU_IE"], []), content=identical_text
    )
    clean = _request(Label.make([S.PII], ["EU_IE"], []), content=identical_text)

    assert apply_rules(tainted) is not None
    assert apply_rules(clean) is None

    # And the rules cannot look at the words even if someone wanted them to.
    # Since Phase 6 the rules are CEL expressions evaluated against a context
    # built by build_policy_context, and the content is not in that context.
    from blackbox.gateway import build_policy_context
    from blackbox.policy import DEFAULT_POLICIES

    context = build_policy_context(tainted)
    assert identical_text not in str(context), "the content reached the rule context"
    assert "content" not in context

    for rule in DEFAULT_POLICIES.rules_in("disclosure"):
        assert "content" not in rule.expression, f"{rule.rule_id} reads the content"


# ----------------------------------------------------------------------
# A block has to be actionable, or it is a wall rather than a control
# ----------------------------------------------------------------------


def test_a_recorded_transfer_basis_clears_the_border_rule():
    """The block asks for a documented basis. Supplying one answers it."""
    label = Label.make([S.SPECIAL_CATEGORY], ["EU_IE"], [])

    without = apply_rules(_request(label))
    assert without is not None and without.decision == Decision.BLOCK

    with_basis = apply_rules(
        _request(label, adequacy_basis="Standard contractual clauses, executed 2026-03-01")
    )
    assert with_basis is not None
    assert with_basis.decision == Decision.ALLOW
    assert "Standard contractual clauses" in with_basis.reasoning


def test_a_transfer_basis_does_not_clear_unrelated_rules():
    """One rule permitting a transfer says nothing about another forbidding it.

    A basis for cross-border transfer is not a basis for sending a national
    identifier, and treating any allow as overriding every block would turn the
    gateway into a single point of bypass.
    """
    label = Label.make([S.SPECIAL_CATEGORY, S.PII_HIGH], ["EU_IE"], [])
    verdict = apply_rules(_request(label, adequacy_basis="Standard contractual clauses"))

    assert verdict is not None
    assert verdict.decision == Decision.BLOCK
    assert verdict.rule_id == "pii_high_never_leaves_the_bank"


@pytest.mark.asyncio
async def test_recording_a_basis_unblocks_the_letter(store, wiki, systems):
    """End to end: refused, basis recorded, then sent."""
    from datetime import datetime, timezone

    from blackbox.agents.fleet_tools import (
        record_transfer_adequacy_basis,
        send_customer_letter,
    )
    from blackbox.wiki import WikiPage

    case_id = "CASE-BASIS"
    recorder = Recorder(case_id=case_id, actor="correspondence_agent", store=store)
    root = recorder.tool_call(tool_name="root", parameters={}, intended_outcome="start")
    recorder.set_cause(root)

    now = datetime.now(timezone.utc)
    wiki.create_page(
        WikiPage(
            page_id=f"case:{case_id}",
            subject=case_id,
            subject_type="case",
            content={
                "customer_id": "CUST-4471",
                "jurisdiction": "EU_IE",
                "vulnerability_indicators": True,
                "vulnerability_reasoning": "Hardship disclosed in the narrative.",
            },
            derived_from=[root],
            version=1,
            created_at=now,
            updated_at=now,
        )
    )

    letter = "We are sorry about a difficult year. The fees have been refunded."

    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        refused = await send_customer_letter("final_response", letter, "final response")
    assert refused["sent"] is False
    assert refused["blocked_by"] == "special_category_third_country_transfer"

    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        record_transfer_adequacy_basis(
            basis="Standard contractual clauses with PrintPost, executed 2026-03-01",
            who_authorised="compliance_lead_okafor",
        )
        allowed = await send_customer_letter("final_response", letter, "final response")

    assert allowed["sent"] is True

    # Both decisions are in the record, and the basis names who stands behind it.
    checks = store.list_events_by_type(case_id, EventType.POLICY_CHECK)
    decisions = [c.payload["policy_id"] for c in checks]
    assert "special_category_third_country_transfer" in decisions
    assert "transfer_adequacy_basis_recorded" in decisions
    assert "special_category_transfer_with_adequacy_basis" in decisions

    basis_event = next(
        c for c in checks if c.payload["policy_id"] == "transfer_adequacy_basis_recorded"
    )
    assert basis_event.payload["input_data"]["authorised_by"] == "compliance_lead_okafor"
