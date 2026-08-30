"""Phase 5 tests: The Eraser.

The failure modes the spec names:

- deleting the source record and leaving derived summaries intact, which is
  exactly the compliance failure this feature exists to prevent
- regenerating a page while quietly reusing the cached summary that contained the
  retracted fact
- region pinning implemented as a config label with no enforcement
- a cascade that runs one level deep instead of transitively
"""

import inspect
from datetime import datetime, timezone

import pytest

from blackbox import eraser
from blackbox.eraser import (
    RETRACTION_LOG,
    CascadeResult,
    Retraction,
    build_dependency_graph,
    build_regeneration_prompt,
    content_still_contains_retracted,
    find_affected,
    retract,
    retraction_history,
)
from blackbox.regions import (
    RegionRoutingRefused,
    evaluate_routing,
    may_read,
    region_for_jurisdiction,
)
from blackbox.schema import EventType
from blackbox.wiki import WikiPage
from blackbox.wiki_store import WikiStore

from fakes import ScriptedLlm, say

CUSTOMER = "CUST-4471"
CUSTOMER_NAME = "Aoife Brennan"


def make_page(
    page_id: str,
    subject: str,
    content: dict,
    derived_from: list,
    subject_type: str = "case",
    jurisdiction: str = None,
) -> WikiPage:
    now = datetime.now(timezone.utc)
    return WikiPage(
        page_id=page_id,
        subject=subject,
        subject_type=subject_type,
        content=content,
        derived_from=derived_from,
        version=1,
        created_at=now,
        updated_at=now,
        jurisdiction=jurisdiction,
    )


def seed_graph(wiki: WikiStore) -> None:
    """A dependency chain four levels deep, plus an unrelated page.

    customer -> case -> pattern -> operating context

    The unrelated page is there so a cascade that reached everything would fail
    the test rather than pass it by accident.
    """
    wiki.create_page(
        make_page(
            "customer:CUST-4471",
            CUSTOMER,
            {"name": CUSTOMER_NAME, "address": "12 Merrion Row, Dublin 2"},
            ["01AAAAAAAAAAAAAAAAAAAAAAAA"],
            subject_type="customer",
            jurisdiction="EU_IE",
        )
    )
    wiki.create_page(
        make_page(
            "case:CASE-0841",
            "CASE-0841",
            {"summary": f"{CUSTOMER_NAME} disputes four fees.", "customer_id": CUSTOMER},
            ["01BBBBBBBBBBBBBBBBBBBBBBBB", "customer:CUST-4471"],
            jurisdiction="EU_IE",
        )
    )
    wiki.create_page(
        make_page(
            "analysis:arrears-pattern",
            "arrears_fee_pattern",
            {"finding": "Three complaints about arrears fees, one from CASE-0841."},
            ["01CCCCCCCCCCCCCCCCCCCCCCCC", "case:CASE-0841"],
            subject_type="analysis",
        )
    )
    wiki.create_page(
        make_page(
            "agent_context:assessment",
            "assessment_agent",
            {"guidance": "Arrears fee complaints are trending upheld."},
            ["analysis:arrears-pattern"],
            subject_type="agent_context",
        )
    )
    wiki.create_page(
        make_page(
            "case:CASE-9999",
            "CASE-9999",
            {"summary": "An unrelated savings interest query."},
            ["01DDDDDDDDDDDDDDDDDDDDDDDD"],
            jurisdiction="UK",
        )
    )


def a_retraction() -> Retraction:
    return Retraction(
        subject=CUSTOMER,
        fields=["name", "address", "date_of_birth"],
        reason="Customer invoked their right to erasure.",
        requested_by="customer",
        values=[CUSTOMER_NAME, "12 Merrion Row, Dublin 2"],
    )


# ----------------------------------------------------------------------
# The graph, and transitivity
# ----------------------------------------------------------------------


def test_dependency_edges_point_forward_after_reversal(wiki):
    """derived_from points back; the cascade travels forward."""
    seed_graph(wiki)
    pages = (
        wiki.list_pages_by_subject_type("case")
        + wiki.list_pages_by_subject_type("customer")
        + wiki.list_pages_by_subject_type("analysis")
        + wiki.list_pages_by_subject_type("agent_context")
    )
    graph = build_dependency_graph(pages)

    assert "case:CASE-0841" in graph["customer:CUST-4471"]
    assert "analysis:arrears-pattern" in graph["case:CASE-0841"]
    assert "agent_context:assessment" in graph["analysis:arrears-pattern"]


def test_page_ids_and_event_ids_are_told_apart():
    """The graph depends on distinguishing the two kinds of reference."""
    page = make_page(
        "case:X",
        "X",
        {},
        ["01AAAAAAAAAAAAAAAAAAAAAAAA", "customer:CUST-1", "01BBBBBBBBBBBBBBBBBBBBBBBB"],
    )
    assert page.source_event_ids() == [
        "01AAAAAAAAAAAAAAAAAAAAAAAA",
        "01BBBBBBBBBBBBBBBBBBBBBBBB",
    ]
    assert page.source_page_ids() == ["customer:CUST-1"]


def test_cascade_is_transitive_not_one_level(wiki):
    """The failure mode that looks correct on any small example.

    The operating-context page is three edges from the customer record and has
    no mention of them at all. It must still be reached.
    """
    seed_graph(wiki)
    pages = (
        wiki.list_pages_by_subject_type("case")
        + wiki.list_pages_by_subject_type("customer")
        + wiki.list_pages_by_subject_type("analysis")
        + wiki.list_pages_by_subject_type("agent_context")
    )
    affected, max_depth = find_affected(pages, a_retraction())

    assert "customer:CUST-4471" in affected
    assert affected["customer:CUST-4471"][0] == 0
    assert "case:CASE-0841" in affected
    assert "analysis:arrears-pattern" in affected
    assert "agent_context:assessment" in affected, "the cascade stopped short"
    assert affected["agent_context:assessment"][0] == 2
    assert max_depth >= 2


def test_cascade_does_not_reach_unrelated_pages(wiki):
    """A cascade that reached everything would pass a transitivity test by luck."""
    seed_graph(wiki)
    pages = wiki.list_pages_by_subject_type("case") + wiki.list_pages_by_subject_type(
        "customer"
    )
    affected, _ = find_affected(pages, a_retraction())
    assert "case:CASE-9999" not in affected


def test_a_cycle_in_the_graph_does_not_loop_forever(wiki):
    """Two pages citing each other must not hang the walk."""
    wiki.create_page(make_page("case:A", CUSTOMER, {"x": 1}, ["case:B"]))
    wiki.create_page(make_page("case:B", "other", {"y": 2}, ["case:A"]))
    pages = wiki.list_pages_by_subject_type("case")

    affected, _ = find_affected(pages, a_retraction())
    assert set(affected) == {"case:A", "case:B"}


def test_a_page_reached_twice_records_its_shortest_depth(wiki):
    """Breadth first, so a diamond does not report the long way round."""
    wiki.create_page(make_page("customer:CUST-4471", CUSTOMER, {"n": 1}, [], "customer"))
    wiki.create_page(make_page("case:mid", "mid", {}, ["customer:CUST-4471"]))
    wiki.create_page(
        make_page("case:end", "end", {}, ["customer:CUST-4471", "case:mid"])
    )
    pages = wiki.list_pages_by_subject_type("case") + wiki.list_pages_by_subject_type(
        "customer"
    )
    affected, _ = find_affected(pages, a_retraction())
    assert affected["case:end"][0] == 1


# ----------------------------------------------------------------------
# Regeneration cannot reintroduce what was retracted
# ----------------------------------------------------------------------


def test_the_regenerator_is_never_shown_the_old_page():
    """The control. Not 'shown it and told to remove things': not shown it."""
    page = make_page(
        "case:X", "X", {"secret": "SENSITIVE_OLD_CONTENT_MARKER"}, ["01A" + "A" * 23]
    )
    prompt = build_regeneration_prompt(page, [{"kind": "TOOL_RESULT", "summary": "fees"}])

    assert "SENSITIVE_OLD_CONTENT_MARKER" not in prompt
    assert "fees" in prompt

    # And the code path itself never gathers it.
    source = inspect.getsource(eraser._rebuild_page)
    assert "page.content" not in source


def test_regenerate_never_merges_with_the_old_content():
    """A merge is how a retracted fact survives a regeneration reporting success."""
    page = make_page("case:X", "X", {"old_field": "old value"}, [])
    fresh = page.regenerate(new_content={"new_field": "new value"}, new_derived_from=[])

    assert fresh.content == {"new_field": "new value"}
    assert "old_field" not in fresh.content


def test_retracted_content_surviving_is_detected():
    """The verification behind the control."""
    retraction = a_retraction()
    assert content_still_contains_retracted({"name": CUSTOMER_NAME}, retraction)
    assert content_still_contains_retracted({"ref": CUSTOMER}, retraction)
    assert not content_still_contains_retracted({"summary": "fees refunded"}, retraction)


@pytest.mark.asyncio
async def test_a_page_regenerating_with_retracted_content_is_held_invalid(store, wiki):
    """If the control fails, the page is not published.

    A regenerator that ignored its instructions and reproduced the customer's
    name must not result in that name being written back to the Wiki.
    """
    seed_graph(wiki)
    disobedient = ScriptedLlm(
        [say('{"summary": "' + CUSTOMER_NAME + ' disputes four fees."}') for _ in range(6)]
    )
    result = await retract(a_retraction(), store=store, wiki_store=wiki, model=disobedient)

    assert result.held_invalid, "a page that kept the retracted name must be flagged"

    for page_id in result.regenerated:
        page = wiki.get_page(page_id, enforce_region=False)
        assert CUSTOMER_NAME not in str(page.content), f"{page_id} kept the name"
        assert CUSTOMER not in str(page.content), f"{page_id} kept the customer id"


@pytest.mark.asyncio
async def test_a_full_cascade_removes_the_fact_everywhere(store, wiki):
    """The headline: retract once, and no derived page still carries it."""
    seed_graph(wiki)
    obedient = ScriptedLlm(
        [say('{"summary": "The complainant disputes four fees.", "status": "open"}')
         for _ in range(8)]
    )
    result = await retract(a_retraction(), store=store, wiki_store=wiki, model=obedient)

    assert result.pages_reached >= 4
    assert result.max_depth >= 2

    for page in (
        wiki.list_pages_by_subject_type("case")
        + wiki.list_pages_by_subject_type("customer")
        + wiki.list_pages_by_subject_type("analysis")
        + wiki.list_pages_by_subject_type("agent_context")
    ):
        assert CUSTOMER_NAME not in str(page.content), f"{page.page_id} still has the name"
        assert "Merrion Row" not in str(page.content), f"{page.page_id} still has the address"

    # The unrelated page is untouched.
    unrelated = wiki.get_page("case:CASE-9999", enforce_region=False)
    assert unrelated.version == 1
    assert "savings interest" in str(unrelated.content)


@pytest.mark.asyncio
async def test_pages_are_invalidated_before_any_are_rebuilt(store, wiki):
    """A page rebuilt while a source still held the fact would pick it back up."""
    source = inspect.getsource(retract)
    invalidate_at = source.index("wiki_store.update_page(page.invalidated(")
    rebuild_at = source.index("_rebuild_page(")
    assert invalidate_at < rebuild_at, "regeneration starts before invalidation completes"


# ----------------------------------------------------------------------
# The Diary still records that a retraction happened
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_retraction_is_recorded_without_the_retracted_values(store, wiki):
    """The Diary is append-only, so anything written there cannot be withdrawn."""
    seed_graph(wiki)
    model = ScriptedLlm([say('{"summary": "The complainant disputes fees."}') for _ in range(8)])
    await retract(a_retraction(), store=store, wiki_store=wiki, model=model)

    retracts = store.list_events_by_type(RETRACTION_LOG, EventType.RETRACT)
    assert len(retracts) == 1
    payload = retracts[0].payload

    assert payload["subject"] == CUSTOMER
    assert "name" in payload["retracted_fields"]
    assert payload["reason"]

    # The values themselves are absent, permanently.
    assert CUSTOMER_NAME not in str(payload)
    assert "Merrion Row" not in str(payload)


@pytest.mark.asyncio
async def test_every_invalidation_is_recorded_with_its_depth(store, wiki):
    """How far the cascade travelled, and by which edge, is auditable."""
    seed_graph(wiki)
    model = ScriptedLlm([say('{"summary": "The complainant disputes fees."}') for _ in range(8)])
    result = await retract(a_retraction(), store=store, wiki_store=wiki, model=model)

    invalidations = store.list_events_by_type(RETRACTION_LOG, EventType.INVALIDATE)
    assert len(invalidations) == result.pages_reached

    deep = [i for i in invalidations if i.payload["page_id"] == "agent_context:assessment"]
    assert deep, "the deepest page has no invalidation event"
    assert deep[0].payload["depth"] == 2
    assert "derived from" in deep[0].payload["reached_via"]

    # Every invalidation hangs off the retraction that caused it.
    retract_event = store.list_events_by_type(RETRACTION_LOG, EventType.RETRACT)[0]
    assert all(i.caused_by == retract_event.event_id for i in invalidations)


@pytest.mark.asyncio
async def test_retraction_history_survives_the_content(store, wiki):
    """The proof that something was there and somebody withdrew it."""
    seed_graph(wiki)
    model = ScriptedLlm([say('{"summary": "The complainant disputes fees."}') for _ in range(8)])
    await retract(a_retraction(), store=store, wiki_store=wiki, model=model)

    history = retraction_history(store)
    assert len(history) == 1
    assert history[0]["subject"] == CUSTOMER
    assert history[0]["reason"] == "Customer invoked their right to erasure."
    assert history[0]["pages_reached"] >= 4


@pytest.mark.asyncio
async def test_a_page_with_no_valid_sources_is_not_left_half_erased(store, wiki):
    """It becomes a statement that it no longer holds anything, not holes."""
    wiki.create_page(
        make_page("customer:CUST-4471", CUSTOMER, {"name": CUSTOMER_NAME}, [], "customer")
    )
    result = await retract(
        a_retraction(), store=store, wiki_store=wiki, model=ScriptedLlm([])
    )

    page = wiki.get_page("customer:CUST-4471", enforce_region=False)
    assert page.content["status"] == "retracted"
    assert CUSTOMER_NAME not in str(page.content)
    assert result.held_invalid


# ----------------------------------------------------------------------
# Region pinning, enforced rather than documented
# ----------------------------------------------------------------------


def test_jurisdictions_map_to_regions():
    assert region_for_jurisdiction("EU_IE") == "EU"
    assert region_for_jurisdiction("US_CA") == "US"
    assert region_for_jurisdiction("UK") == "UK"


def test_an_unknown_jurisdiction_gets_the_strictest_treatment():
    """An unrecognised value must not become a way of moving data to the US."""
    assert region_for_jurisdiction("MADE_UP") == "EU"
    assert region_for_jurisdiction(None) == "EU"
    assert not may_read("EU", "US")


def test_a_us_worker_may_not_read_eu_data():
    decision = evaluate_routing("case:X", "EU_IE", worker_region="US")
    assert decision.allowed is False
    assert "may not cross" in decision.reasoning
    assert "Route it to a EU instance" in decision.reasoning


def test_an_eu_worker_may_read_eu_data():
    assert evaluate_routing("case:X", "EU_IE", worker_region="EU").allowed


def test_region_pinning_refuses_the_read_rather_than_labelling_it(store):
    """The difference between a control and a diagram.

    A US worker asking for an EU-pinned page does not get a warning and the page.
    It gets an exception and no page.
    """
    eu_wiki = WikiStore(
        project_id="blackbox-test", event_store=store, in_memory=True, worker_region="EU"
    )
    eu_wiki.create_page(
        make_page("case:EU-1", "CASE-EU-1", {"summary": "x"}, [], jurisdiction="EU_IE")
    )

    # Same underlying data, a worker in the wrong region.
    us_wiki = WikiStore(
        project_id="blackbox-test", event_store=store, in_memory=True, worker_region="US"
    )
    us_wiki._memory = eu_wiki._memory

    assert eu_wiki.get_page("case:EU-1") is not None

    with pytest.raises(RegionRoutingRefused) as refused:
        us_wiki.get_page("case:EU-1")

    assert refused.value.page_region == "EU"
    assert refused.value.worker_region == "US"


def test_a_routing_refusal_is_recorded_with_its_reasoning(store):
    """A control that refuses silently cannot be audited."""
    us_wiki = WikiStore(
        project_id="blackbox-test", event_store=store, in_memory=True, worker_region="US"
    )
    us_wiki.create_page(
        make_page("case:EU-2", "CASE-EU-2", {"summary": "x"}, [], jurisdiction="EU_IE")
    )

    with pytest.raises(RegionRoutingRefused):
        us_wiki.get_page("case:EU-2")

    checks = store.list_events_by_type("CASE-EU-2", EventType.POLICY_CHECK)
    assert len(checks) == 1
    assert checks[0].payload["policy_id"] == "region_pinning"
    assert checks[0].payload["decision"] == "block"
    assert checks[0].payload["input_data"]["worker_region"] == "US"
    assert checks[0].payload["reasoning"]


@pytest.mark.asyncio
async def test_the_eraser_reaches_pages_pinned_to_other_regions(store, wiki):
    """A retraction must not be defeated by where the cascade happens to run.

    Refusing to erase EU data because the machine doing the erasing is in the US
    would be the region control working exactly backwards.
    """
    us_wiki = WikiStore(
        project_id="blackbox-test", event_store=store, in_memory=True, worker_region="US"
    )
    us_wiki.create_page(
        make_page(
            "customer:CUST-4471",
            CUSTOMER,
            {"name": CUSTOMER_NAME},
            [],
            "customer",
            jurisdiction="EU_IE",
        )
    )

    # A normal read from here is refused.
    with pytest.raises(RegionRoutingRefused):
        us_wiki.get_page("customer:CUST-4471")

    # The cascade still reaches it.
    result = await retract(
        a_retraction(), store=store, wiki_store=us_wiki, model=ScriptedLlm([])
    )
    assert "customer:CUST-4471" in [i["page_id"] for i in result.invalidated]

    page = us_wiki.get_page("customer:CUST-4471", enforce_region=False)
    assert CUSTOMER_NAME not in str(page.content)


def test_a_listing_cannot_be_used_to_get_around_region_pinning(store):
    """A control that guards single reads but not listings is not a control."""
    us_wiki = WikiStore(
        project_id="blackbox-test", event_store=store, in_memory=True, worker_region="US"
    )
    us_wiki.create_page(
        make_page("case:EU-3", "CASE-EU-3", {"summary": "eu"}, [], jurisdiction="EU_IE")
    )
    us_wiki.create_page(
        make_page("case:US-3", "CASE-US-3", {"summary": "us"}, [], jurisdiction="US_CA")
    )

    visible = us_wiki.list_pages_by_subject_type("case")
    assert [p.page_id for p in visible] == ["case:US-3"]

    # And the shortfall is recorded, so a scan that saw less than it should have
    # is answerable afterwards rather than silently incomplete.
    checks = store.list_events_by_type("region:US", EventType.POLICY_CHECK)
    assert len(checks) == 1
    assert checks[0].payload["policy_id"] == "region_pinning_listing"
    assert checks[0].payload["input_data"]["withheld_page_ids"] == ["case:EU-3"]


def test_an_eu_worker_sees_the_whole_listing(store):
    eu_wiki = WikiStore(
        project_id="blackbox-test", event_store=store, in_memory=True, worker_region="EU"
    )
    eu_wiki.create_page(
        make_page("case:EU-4", "CASE-EU-4", {"summary": "eu"}, [], jurisdiction="EU_IE")
    )
    eu_wiki.create_page(
        make_page("case:US-4", "CASE-US-4", {"summary": "us"}, [], jurisdiction="US_CA")
    )
    assert len(eu_wiki.list_pages_by_subject_type("case")) == 2
