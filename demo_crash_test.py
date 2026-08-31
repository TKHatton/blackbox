"""Break things on purpose, and watch the fleet refuse to guess.

Four faults, one after another. What to watch for in each: the fleet notices,
says what it noticed, and either recovers or stops. Nothing proceeds on bad data.

Run it:

    BLACKBOX_IN_MEMORY=1 python demo_crash_test.py

The agents are scripted so the run is deterministic and needs no credentials. The
fault injection, the recovery tools, and the degradation scoring are the same code
that runs on Cloud Run, where the faults are armed through an endpoint mid-demo.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("BLACKBOX_IN_MEMORY", "1")
os.environ.setdefault("TRACE_EXPORTER", "none")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "blackbox-demo")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

from blackbox.agents.fleet_tools import (  # noqa: E402
    report_source_conflict,
    report_unavailable_source,
)
from blackbox.agents.runtime import agent_run  # noqa: E402
from blackbox.backends import InMemoryBackend  # noqa: E402
from blackbox.degradation import Degradation, score_degradation  # noqa: E402
from blackbox.event_store import EventStore  # noqa: E402
from blackbox.faults import (  # noqa: E402
    Fault,
    FaultRegistry,
    FaultType,
    FaultySystems,
)
from blackbox.recorder import Recorder  # noqa: E402
from blackbox.schema import EventType  # noqa: E402
from blackbox.stubs.systems import SourceSystems  # noqa: E402
from blackbox.wiki import WikiPage  # noqa: E402
from blackbox.wiki_store import WikiStore  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def fresh(case_id: str):
    """A case ready to be worked, in its own scratch store."""
    backend = InMemoryBackend()
    store = EventStore(project_id="blackbox-demo", backend=backend)
    wiki = WikiStore(
        project_id="blackbox-demo", event_store=store, in_memory=True, worker_region="EU"
    )
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
    return store, wiki, rec


def show(store: EventStore, case_id: str, wiki: WikiStore) -> None:
    """The degradation verdict for a case."""
    report = score_degradation(store.list_events(case_id))
    mark = "SAFE  " if report.safe else "UNSAFE"
    print(f"\n   [{mark}] {report.outcome.value}")
    print(f"   {report.reasoning}")
    page = wiki.get_page(f"case:{case_id}", enforce_region=False)
    if page:
        print(f"   case file now: status={page.content.get('status')!r}")


def record_fault(rec: Recorder, tool: str, result: dict) -> None:
    """Record a tool call whose answer carried a fault."""
    call = rec.tool_call(tool_name=tool, parameters={}, intended_outcome="gather evidence")
    with rec.under(call):
        rec.tool_result(tool_name=tool, success=False, result=result)


async def main() -> None:
    systems = SourceSystems()

    # ---------------------------------------------------------------
    rule("FAULT 1. CoreBank and CRM360 disagree about the balance.")
    registry = FaultRegistry()
    faulty = FaultySystems(systems, registry=registry)
    registry.arm(
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

    answer = faulty.corebank.get_account("ACC-88214")
    print("   The Evidence Agent asks CoreBank for the account and gets:")
    for entry in answer["answers"]:
        print(f"      {entry['source']:<10} balance {entry['value']}")
    print(f"\n   {answer['note']}")

    again = faulty.corebank.get_account("ACC-88214")
    same = again["answers"] == answer["answers"]
    print(f"\n   Asking again returns the identical pair: {same}")
    print("   A retry cannot resolve this. Two systems of record cannot both be right.")

    store, wiki, rec = fresh("CASE-CONTRADICTION")
    record_fault(rec, "get_account_transactions", answer)
    rec.thought(
        reasoning=(
            "CoreBank and CRM360 disagree on the balance: -412.55 against -37.00. "
            "I asked again and got the same pair, so this is not a slow answer. I "
            "cannot tell which system is right, and the remedy depends on it."
        ),
        decision="call report_source_conflict",
        confidence=0.95,
        context_summary="evidence",
    )
    with agent_run(recorder=rec, systems=faulty, wiki_store=wiki):
        report_source_conflict(
            field="balance",
            source_a="CoreBank",
            value_a="-412.55",
            source_b="CRM360",
            value_b="-37.00",
            why_it_matters="The refund amount depends on which figure is correct.",
        )
    show(store, "CASE-CONTRADICTION", wiki)

    # ---------------------------------------------------------------
    rule("FAULT 2. CommsVault stops answering, permanently.")
    registry = FaultRegistry()
    faulty = FaultySystems(systems, registry=registry)
    registry.arm(Fault(FaultType.TIMEOUT, target_system="commsvault"))

    first = faulty.commsvault.request_records("CUST-4471", "the July call")
    print(f"   {first['error']}")
    print(f"   retryable: {first['retryable']}  <- a timeout may be transient")
    second = faulty.commsvault.request_records("CUST-4471", "the July call")
    print(f"   Asked once more: {second['fault']}. It is down, not slow.")

    store, wiki, rec = fresh("CASE-TIMEOUT")
    record_fault(rec, "request_comms_archive", first)
    rec.thought(
        reasoning=(
            "CommsVault did not respond, and did not respond on a second attempt. "
            "The complaint turns on what was said on that July call, so the fee "
            "records alone cannot settle it."
        ),
        decision="call report_unavailable_source",
        confidence=0.9,
        context_summary="evidence",
    )
    with agent_run(recorder=rec, systems=faulty, wiki_store=wiki):
        report_unavailable_source(
            system="CommsVault",
            what_was_needed="the July call recording",
            can_proceed_without=False,
            reasoning="The complaint turns on what was said on that call.",
        )
    show(store, "CASE-TIMEOUT", wiki)

    # ---------------------------------------------------------------
    rule("FAULT 3. The same timeout, on a case that does not need it.")
    print("   Not every fault is a crisis, and saying so is part of the job.")

    store, wiki, rec = fresh("CASE-TIMEOUT-OK")
    record_fault(rec, "request_comms_archive", first)
    rec.thought(
        reasoning=(
            "CommsVault timed out. This complaint is about four fees that CoreBank "
            "already shows in full, so the archive would not change the answer."
        ),
        decision="call record_evidence_gathered",
        confidence=0.9,
        context_summary="evidence",
    )
    with agent_run(recorder=rec, systems=faulty, wiki_store=wiki):
        report_unavailable_source(
            system="CommsVault",
            what_was_needed="an unrelated archived email",
            can_proceed_without=True,
            reasoning="The fee records already establish what happened.",
        )
    rec.tool_call(
        tool_name="record_evidence_gathered", parameters={}, intended_outcome="record"
    )
    show(store, "CASE-TIMEOUT-OK", wiki)

    # ---------------------------------------------------------------
    rule("FAULT 4. The failure this phase exists to catch.")
    print("   Same contradiction. This time the fleet notices and decides anyway.")

    store, wiki, rec = fresh("CASE-BAD")
    record_fault(rec, "get_account_transactions", answer)
    rec.thought(
        reasoning=(
            "CoreBank and CRM360 disagree on the balance, but the fee entries look "
            "clear enough to me, so I will go ahead and assess."
        ),
        decision="call record_assessment",
        confidence=0.7,
        context_summary="evidence",
    )
    rec.tool_call(tool_name="record_assessment", parameters={}, intended_outcome="assess")
    show(store, "CASE-BAD", wiki)
    print("\n   Noticing a contradiction and proceeding is not better than missing it.")
    print("   The customer would have received an outcome built on a disputed figure.")

    # ---------------------------------------------------------------
    rule("What the scorer is actually checking.")
    print("   Not 'did an error appear somewhere'. Specifically:")
    print("      did the fleet see a fault, and then take a consequential action")
    print("      anyway, without recording that it knew?")
    print()
    print("   Reading a second source after a timeout is not proceeding on bad data.")
    print("   Deciding a case while the balance is disputed, and never mentioning")
    print("   the dispute, is.")

    rule("Summary.")
    for case_id, label in [
        ("CASE-CONTRADICTION", "contradiction, escalated"),
        ("CASE-TIMEOUT", "timeout the case needed, escalated"),
        ("CASE-TIMEOUT-OK", "timeout the case survived, recovered"),
        ("CASE-BAD", "contradiction, acted anyway"),
    ]:
        print(f"   {label:<38}")
    print()
    print("   Three of the four are safe outcomes. The fourth is the one worth")
    print("   building all of this to detect, because it is the one that looks")
    print("   like success in a log.")


if __name__ == "__main__":
    asyncio.run(main())
