"""Retract one customer record, and watch six derived conclusions come apart.

The compliance failure this prevents: you delete the source record, report
success, and six summaries elsewhere still carry the customer's name because a
model wrote them months ago and nobody knows what went into them.

Also shows region pinning refusing to move EU data to a US worker, which is the
other half of Phase 5.

Run it:

    BLACKBOX_IN_MEMORY=1 python demo_eraser.py

The regenerating model is scripted so the run is deterministic and needs no
credentials. The cascade, the graph walk, the verification and the region checks
are the same code that runs on Cloud Run.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("BLACKBOX_IN_MEMORY", "1")
os.environ.setdefault("TRACE_EXPORTER", "none")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "blackbox-demo")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

from fakes import ScriptedLlm, say  # noqa: E402

from blackbox.eraser import Retraction, retract, retraction_history  # noqa: E402
from blackbox.event_store import EventStore  # noqa: E402
from blackbox.recorder import Recorder  # noqa: E402
from blackbox.regions import RegionRoutingRefused  # noqa: E402
from blackbox.schema import EventType  # noqa: E402
from blackbox.wiki import WikiPage  # noqa: E402
from blackbox.wiki_store import WikiStore  # noqa: E402

CUSTOMER = "CUST-4471"
NAME = "Aoife Brennan"
ADDRESS = "12 Merrion Row, Dublin 2"


def rule(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def page(page_id, subject, subject_type, content, derived_from, jurisdiction=None):
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


def seed(store: EventStore, wiki: WikiStore) -> None:
    """Six pages, four levels of derivation, plus two that are none of its business.

    The pages cite real events, so regeneration has genuine sources to rebuild
    from rather than falling straight through to a placeholder.
    """
    rec = Recorder(case_id="CASE-0841", actor="intake_agent", store=store)

    # Events that carry her identity. These stay in the Diary forever, and the
    # Eraser will refuse to rebuild anything from them.
    identity_event = rec.tool_result(
        tool_name="CRM360.get_customer", success=True,
        result={"customer_id": CUSTOMER, "name": NAME, "address": ADDRESS},
    )
    # Events that do not. These survive as valid sources.
    fees_event = rec.tool_result(
        tool_name="CoreBank.get_transactions", success=True,
        result={"fees": 4, "total": 117.00, "description": "arrears management fees"},
    )
    outcome_event = rec.thought(
        reasoning="Fees continued after a hardship disclosure that was never logged, "
                  "so the complaint is upheld and the fees should be returned.",
        decision="uphold and refund", confidence=0.9,
        context_summary="Assessment of the arrears fee complaint",
    )
    march_event = rec.tool_result(
        tool_name="CoreBank.get_transactions", success=True,
        result={"query": "direct debit, March", "finding": "the debit was correctly applied"},
    )
    pattern_event = rec.thought(
        reasoning="Across eleven arrears cases, three show fees applied after a "
                  "disclosed hardship. That is worth watching but not yet a pattern.",
        decision="flag as moderate confidence", confidence=0.6,
        context_summary="Systemic pattern scan",
    )

    wiki.create_page(page(
        "customer:CUST-4471", CUSTOMER, "customer",
        {"name": NAME, "address": ADDRESS, "country": "Ireland",
         "vulnerability_flags": ["financial_hardship"]},
        [identity_event], "EU_IE"))

    wiki.create_page(page(
        "case:CASE-0841", "CASE-0841", "case",
        {"summary": f"{NAME} of {ADDRESS} disputes four arrears fees.",
         "customer_id": CUSTOMER, "outcome": "upheld", "remedy_amount": 617.00},
        [identity_event, fees_event, outcome_event, "customer:CUST-4471"], "EU_IE"))

    wiki.create_page(page(
        "case:CASE-0612", "CASE-0612", "case",
        {"summary": f"{NAME} queried a direct debit in March.",
         "customer_id": CUSTOMER, "outcome": "not_upheld"},
        [identity_event, march_event, "customer:CUST-4471"], "EU_IE"))

    wiki.create_page(page(
        "analysis:arrears-pattern", "arrears_fee_pattern", "analysis",
        {"finding": "Arrears fees applied after a disclosed hardship in 3 of 11 cases.",
         "example_case": "CASE-0841", "confidence": "moderate"},
        [pattern_event, "case:CASE-0841"]))

    wiki.create_page(page(
        "agent_context:assessment", "assessment_agent", "agent_context",
        {"guidance": "Where hardship was disclosed and fees continued, lean towards upheld.",
         "based_on": "arrears_fee_pattern"},
        [pattern_event, "analysis:arrears-pattern"]))

    wiki.create_page(page(
        "agent_context:correspondence", "correspondence_agent", "agent_context",
        {"guidance": "Acknowledge hardship warmly in fee-reversal letters."},
        [pattern_event, "agent_context:assessment"]))

    # Nothing to do with this customer.
    wiki.create_page(page(
        "case:CASE-9999", "CASE-9999", "case",
        {"summary": "Savings interest query from another customer."},
        [march_event], "UK"))
    wiki.create_page(page(
        "customer:CUST-1180", "CUST-1180", "customer",
        {"name": "Marcus Webb", "country": "United States"},
        [march_event], "US_CA"))


def show(wiki: WikiStore, title: str) -> None:
    print(f"\n   {title}")
    for p in sorted(
        wiki._raw_list("subject_type", "customer")
        + wiki._raw_list("subject_type", "case")
        + wiki._raw_list("subject_type", "analysis")
        + wiki._raw_list("subject_type", "agent_context"),
        key=lambda p: p.page_id,
    ):
        blob = str(p.content)
        mark = "HAS NAME" if NAME in blob or ADDRESS in blob else "clean   "
        print(f"      v{p.version} {mark}  {p.page_id:<32} {blob[:52]}")


async def main() -> None:
    store = EventStore(project_id="blackbox-demo")
    wiki = WikiStore(project_id="blackbox-demo", event_store=store, in_memory=True,
                     worker_region="EU")
    seed(store, wiki)

    rule("Before. Her name is in four places, only one of which is her own record.")
    show(wiki, "Wiki pages:")
    print("\n   The pattern analysis and both operating-context pages never mention")
    print("   her at all. They are derived from a case that does.")

    rule("She invokes her right to erasure.")
    retraction = Retraction(
        subject=CUSTOMER,
        fields=["name", "address", "date_of_birth", "vulnerability_flags"],
        reason="Customer invoked their right to erasure under GDPR Article 17.",
        requested_by="customer_via_web_form",
        values=[NAME, ADDRESS],
    )
    print(f"   subject : {retraction.subject}")
    print(f"   fields  : {', '.join(retraction.fields)}")
    print(f"   reason  : {retraction.reason}")

    # Pages are rebuilt in (depth, page_id) order. The customer's own page has
    # no source that survives the retraction, so it never reaches the model and
    # consumes no scripted turn.
    regenerator = ScriptedLlm([
        # depth 0, case:CASE-0612
        say('{"summary": "The complainant queried a direct debit in March.", "outcome": "not_upheld"}'),
        # depth 0, case:CASE-0841
        say('{"summary": "The complainant disputed four arrears fees.", "outcome": "upheld", "remedy_amount": 617.0}'),
        # depth 1, analysis:arrears-pattern
        say('{"finding": "Arrears fees applied after a disclosed hardship in 3 of 11 cases.", "confidence": "moderate"}'),
        # depth 2, agent_context:assessment
        say('{"guidance": "Where hardship was disclosed and fees continued, lean towards upheld."}'),
        # depth 3, agent_context:correspondence
        say('{"guidance": "Acknowledge hardship warmly in fee-reversal letters."}'),
    ])

    result = await retract(retraction, store=store, wiki_store=wiki, model=regenerator)

    rule("The cascade.")
    for item in sorted(result.invalidated, key=lambda i: (i["depth"], i["page_id"])):
        print(f"   depth {item['depth']}  {item['page_id']:<32} {item['reached_via']}")
    print(f"\n   {result.pages_reached} pages reached, {result.max_depth} levels deep.")
    print(f"   {len(result.regenerated)} rewritten by the model from their remaining valid sources.")
    print(f"   {len(result.redacted)} had nothing valid left to rebuild from.")
    print("")
    print("   The rewritten pages were never shown their old content. They were")
    print("   handed only the sources that did not carry her identity, and asked")
    print("   to write the page fresh.")

    rule("After.")
    show(wiki, "Wiki pages:")

    rule("What survived, and what did not.")
    leaked = [
        p.page_id
        for p in wiki._raw_list("subject_type", "customer")
        + wiki._raw_list("subject_type", "case")
        + wiki._raw_list("subject_type", "analysis")
        + wiki._raw_list("subject_type", "agent_context")
        if NAME in str(p.content) or ADDRESS in str(p.content)
    ]
    print(f"   Pages still carrying her name or address: {leaked or 'none'}")

    untouched = wiki._raw_list("subject_type", "case")
    other = [p for p in untouched if p.page_id == "case:CASE-9999"][0]
    print(f"   Unrelated case untouched: v{other.version}, {str(other.content)[:44]}")

    rule("The Diary still knows this happened.")
    for entry in retraction_history(store):
        print(f"   {entry['timestamp'][:19]}  {entry['subject']}")
        print(f"      fields  : {', '.join(entry['retracted_fields'])}")
        print(f"      reason  : {entry['reason']}")
        print(f"      reached : {entry['pages_reached']} pages, depth {entry['max_depth']}")
    invalidations = store.list_events_by_type("retractions", EventType.INVALIDATE)
    print(f"\n   {len(invalidations)} invalidation events, each naming the edge it came by.")
    print("   The values themselves were never written here. The Diary is append-only,")
    print("   so anything put into it could not later be taken out.")

    rule("Region pinning. The other half of Phase 5.")
    us_worker = WikiStore(project_id="blackbox-demo", event_store=store,
                          in_memory=True, worker_region="US")
    us_worker._memory = wiki._memory

    print("   A worker in the EU asks for an EU-pinned case:")
    ok = wiki.get_page("case:CASE-0841")
    print(f"      allowed, v{ok.version}")

    print("\n   The same page, asked for by a worker running in the US:")
    try:
        us_worker.get_page("case:CASE-0841")
        print("      allowed  <- this would be a defect")
    except RegionRoutingRefused as refused:
        print(f"      REFUSED  {refused.page_region} data, {refused.worker_region} worker")
        print(f"      {refused.reasoning[:180]}...")

    print("\n   And it cannot be worked around by listing instead of reading:")
    visible = [p.page_id for p in us_worker.list_pages_by_subject_type("case")]
    print(f"      a US worker listing cases sees: {visible}")
    print(f"      an EU worker sees: {[p.page_id for p in wiki.list_pages_by_subject_type('case')]}")

    refusals = [
        e for e in store.list_events_by_type("CASE-0841", EventType.POLICY_CHECK)
        if e.payload.get("policy_id") == "region_pinning"
    ]
    print(f"\n   {len(refusals)} routing refusal recorded, with its reasoning.")
    print("   The refusal is an event, not a log line, so the routing decision is")
    print("   auditable and cannot be confused with a machine that never tried.")


if __name__ == "__main__":
    asyncio.run(main())
