"""One complaint, start to finish, with the calendar compressed.

Runs the full workflow in a few seconds: intake, evidence, a wait on CommsVault,
a heartbeat that wakes it days later, assessment, a human approval gate, the
approval arriving, remediation, the final letter, the appeal window, and closure.

Two things are faked and both are stated on screen as they happen:

- **The model.** Gemini's turns are scripted, so the run is deterministic and
  needs no credentials. The scripted text is what a model would say; the
  machinery around it is the real machinery.
- **The clock.** Each wait is evaluated at a later time rather than waited
  through. The wait itself is not simulated: the agent genuinely suspends, the
  suspension genuinely lives in the Diary, and a heartbeat with no memory of it
  genuinely finds it. Only the passage of time is hurried.

Nothing else is stood in for. The events, the causal tree, the Wiki rewrites, and
the approval gates are the same code paths that run on Cloud Run.

Run it:

    BLACKBOX_IN_MEMORY=1 TRACE_EXPORTER=none python demo_lifecycle.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BLACKBOX_IN_MEMORY", "1")
os.environ.setdefault("TRACE_EXPORTER", "none")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "blackbox-demo")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

from fakes import ScriptedLlm, say, think_and_call  # noqa: E402

from blackbox.agents.intake_service import case_id_for, run_intake  # noqa: E402
from blackbox.approvals import handle_approval  # noqa: E402
from blackbox.event_store import EventStore  # noqa: E402
from blackbox.heartbeat import run_heartbeat  # noqa: E402
from blackbox.recorder import Recorder  # noqa: E402
from blackbox.schema import EventType  # noqa: E402
from blackbox.stubs import data  # noqa: E402
from blackbox.stubs.systems import SourceSystems  # noqa: E402
from blackbox.wake import find_open_suspensions  # noqa: E402
from blackbox.wiki_store import WikiStore  # noqa: E402

COMPLAINT = data.INBOUND_COMPLAINTS[0]
CASE_ID = case_id_for(COMPLAINT["complaint_ref"])
DAY0 = datetime.now(timezone.utc)


def banner(day: int, title: str) -> None:
    print(f"\n{'=' * 74}\nDAY {day:<3} {title}\n{'=' * 74}")


def note(text: str) -> None:
    print(f"   {text}")


def show_waits(store: EventStore) -> None:
    waits = find_open_suspensions(store)
    if not waits:
        note("Nothing is waiting. No process is resident.")
        return
    for w in waits:
        note(f"WAITING  {w.condition.resume_agent} <- {w.condition.description}")
        note(f"         stored as event {w.suspend_event_id} in the Diary, not in memory")


async def main() -> None:
    store = EventStore(project_id="blackbox-demo")
    wiki = WikiStore(project_id="blackbox-demo", event_store=store, in_memory=True)
    systems = SourceSystems()

    # ---------------------------------------------------------------
    banner(0, "A complaint arrives. Nobody pressed a button.")
    note(f"{COMPLAINT['complaint_ref']} via {COMPLAINT['channel']}")
    note(f"'{COMPLAINT['narrative'][:88]}...'")

    intake_model = ScriptedLlm(
        [
            think_and_call(
                "Arrears fees, three months running. I need to know where she lives "
                "and whether anyone has flagged her already.",
                "lookup_customer",
                {"customer_id": COMPLAINT["customer_id"]},
            ),
            think_and_call(
                "Resident in Ireland, hardship flag already on file. The account may "
                "sit somewhere else, so I need to check.",
                "get_account_summary",
                {"account_id": COMPLAINT["account_id"]},
            ),
            think_and_call(
                "The account is domiciled in the UK while she is resident in Ireland. "
                "Consumer protection follows residence, so EU_IE governs.",
                "record_intake_determination",
                {
                    "category": "billing_dispute",
                    "severity": "high",
                    "jurisdiction": "EU_IE",
                    "jurisdiction_reasoning": (
                        "Customer resident in Ireland, account domiciled in the UK. "
                        "Consumer protection attaches to residence, so EU_IE governs."
                    ),
                    "vulnerability_indicators": True,
                    "vulnerability_reasoning": (
                        "CRM360 carries a financial hardship flag and the narrative "
                        "discloses a health condition affecting her income."
                    ),
                    "summary": "Three arrears management fees and one unpaid direct "
                    "debit fee, charged during disclosed financial difficulty.",
                    "acknowledgment_due_days": 3,
                    "final_response_due_days": 56,
                },
            ),
            say("Case opened and the statutory clock is running."),
        ]
    )

    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=intake_model
    )
    note(f"Intake Agent opened {result['case_id']}")
    note(f"jurisdiction={result['determination']['jurisdiction']}  "
         f"vulnerable={result['determination']['vulnerability_indicators']}")

    # ---------------------------------------------------------------
    banner(1, "Evidence Agent hits the slow system and decides to wait.")
    job = systems.commsvault.request_records(COMPLAINT["customer_id"], "July call")
    note(f"CommsVault returned a job id, not records: {job['job_id']}")
    note(f"records expected in {job['estimated_delay_days']} days")

    from blackbox.agents.fleet_tools import suspend_until_evidence_ready
    from blackbox.agents.runtime import agent_run

    recorder = Recorder(case_id=CASE_ID, actor="evidence_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        suspend_until_evidence_ready(
            job["job_id"], job["ready_at"], "the July call she says she made"
        )
    note("Evidence Agent suspended. It is no longer running.")
    show_waits(store)

    # ---------------------------------------------------------------
    banner(2, "A heartbeat fires. The records are not ready, so nothing happens.")
    beat = await run_heartbeat(
        store=store, wiki_store=wiki, systems=systems, now=DAY0 + timedelta(days=2)
    )
    note(f"evaluated {len(beat['evaluated'])} wait(s), resumed {len(beat['resumed'])}")
    for e in beat["evaluated"]:
        note(f"decision: {e['reasoning']}")

    # ---------------------------------------------------------------
    banner(5, "A later heartbeat, on a fresh instance with no memory.")
    reborn = EventStore(project_id="blackbox-demo", backend=store._backend)
    note("This EventStore object has never seen this case before.")
    note(f"It found {len(find_open_suspensions(reborn))} open wait by reading the Diary.")

    evidence_model = ScriptedLlm(
        [
            think_and_call(
                "The archive is back. The July call confirms she told us about her "
                "circumstances and nobody recorded it. That is enough to assess on.",
                "record_evidence_gathered",
                {
                    "summary": "CommsVault confirms a July call disclosing hardship "
                    "that was never logged in CRM360. Four fees charged after it.",
                    "sufficient_to_assess": True,
                    "outstanding_items": "",
                },
            ),
            say("Evidence recorded."),
        ]
    )
    beat = await run_heartbeat(
        store=reborn,
        wiki_store=wiki,
        systems=systems,
        model=evidence_model,
        now=DAY0 + timedelta(days=5),
    )
    r = beat["resumed"][0]
    note(f"RESUMED {r['resumed_agent']} with full context rebuilt from the Wiki + fold")
    resumes = store.list_events_by_type(CASE_ID, EventType.RESUME)
    note(f"RESUME event {resumes[0].event_id} caused_by {resumes[0].caused_by}")
    show_waits(store)

    # ---------------------------------------------------------------
    banner(6, "Assessment. The remedy is over the threshold, so a human is needed.")
    from blackbox.agents.fleet_tools import record_assessment, suspend_until_approved

    recorder = Recorder(case_id=CASE_ID, actor="assessment_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        assessment = record_assessment(
            outcome="upheld",
            reasoning="INTERNAL: the July call was not logged, so the arrears process "
            "ran on incomplete information. The bank's failure, not hers.",
            proposed_remedy="Refund all four fees plus distress compensation",
            remedy_amount=617.00,
            looks_systemic=False,
            systemic_reasoning="A single unlogged call, not a process defect.",
        )
        note(f"outcome={assessment['outcome']}  gate A required={assessment['gate_a_required']}")
        suspend_until_approved("A", "Refund of 617.00 EUR plus compensation")
    note("Assessment Agent suspended. An approval cannot be hurried.")
    show_waits(store)

    # ---------------------------------------------------------------
    banner(8, "Heartbeats keep firing. An approval cannot be polled into existence.")
    beat = await run_heartbeat(
        store=store, wiki_store=wiki, systems=systems, now=DAY0 + timedelta(days=8)
    )
    for e in beat["evaluated"]:
        note(f"decision: {e['reasoning']}")

    # ---------------------------------------------------------------
    banner(10, "A human approves. The message arriving is the wake condition.")
    approval_model = ScriptedLlm(
        [
            think_and_call(
                "Gate A is approved. The remedy stands as assessed and the case can "
                "move to remediation.",
                "read_case_file",
                {},
            ),
            say("Approved. Handing on."),
        ]
    )
    out = await handle_approval(
        {
            "case_id": CASE_ID,
            "gate": "A",
            "approved": True,
            "approver": "adjudicator_kim",
            "note": "Fees charged after a disclosed hardship. Refund is right.",
        },
        store=store,
        wiki_store=wiki,
        systems=systems,
        model=approval_model,
    )
    note(f"resumed={out['resumed']} agent={out.get('resumed_agent')}")
    show_waits(store)

    # ---------------------------------------------------------------
    banner(10, "Remediation. The only agent that can move money.")
    from blackbox.agents.fleet_tools import execute_remedy, send_customer_letter
    from blackbox.agents.fleet_tools import suspend_for_appeal_window

    recorder = Recorder(case_id=CASE_ID, actor="remediation_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        paid = execute_remedy("ACC-88214", 617.00, "Refund of arrears fees")
    note(f"executed={paid['executed']} amount={paid.get('amount')}")

    # ---------------------------------------------------------------
    banner(11, "The final letter. No medical word appears in it.")
    recorder = Recorder(case_id=CASE_ID, actor="correspondence_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    letter = (
        "Dear Ms Brennan,\n\n"
        "Thank you for writing to us, and I am sorry for what you have been "
        "through this year. We should have listened properly when you called in "
        "July, and we did not.\n\n"
        "We have refunded all four fees and added compensation for the distress "
        "this caused, totalling EUR 617.00. You will see it in your account within "
        "three working days.\n\n"
        "If you disagree with this outcome you have 30 days to tell us."
    )
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        send_customer_letter("final_response", letter, "final response")
        suspend_for_appeal_window()
    note("Letter sent via PrintPost. Case asleep for the appeal window.")
    show_waits(store)

    # ---------------------------------------------------------------
    banner(41, "The appeal window closes with no reply. The case wakes itself.")
    closing_model = ScriptedLlm(
        [
            think_and_call(
                "Thirty days have passed with no reply. The remedy was paid and the "
                "final response was sent, so there is nothing outstanding.",
                "close_case",
                {"why": "Appeal window elapsed with no customer reply."},
            ),
            say("Case closed."),
        ]
    )
    beat = await run_heartbeat(
        store=store,
        wiki_store=wiki,
        systems=systems,
        model=closing_model,
        now=DAY0 + timedelta(days=41),
    )
    note(f"resumed {len(beat['resumed'])} case(s)")

    # ---------------------------------------------------------------
    print(f"\n{'=' * 74}\nWHAT THE RECORDER HOLDS\n{'=' * 74}")
    events = store.list_events(CASE_ID)
    print(f"   {len(events)} events, one causal tree, nothing overwritten")

    counts: dict = {}
    for e in events:
        counts[e.event_type.value] = counts.get(e.event_type.value, 0) + 1
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"      {kind:<14} {n}")

    inspector = Recorder(case_id=CASE_ID, actor="inspector", store=store)
    inspector.assert_causally_complete()
    print("   causal tree: exactly one root, no orphans")

    page = wiki.get_page(f"case:{CASE_ID}")
    print(f"   Wiki page version {page.version}, derived from {len(page.derived_from)} events")
    print(f"   final status: {page.content['status']}")

    suspends = store.list_events_by_type(CASE_ID, EventType.SUSPEND)
    resumes = store.list_events_by_type(CASE_ID, EventType.RESUME)
    print(f"   {len(suspends)} suspensions, {len(resumes)} resumptions, "
          f"{len(find_open_suspensions(store))} still open")

    sent = store.list_events_by_type(CASE_ID, EventType.MESSAGE_SENT)
    leaked = [m for m in sent if "INTERNAL" in m.payload["content"]]
    print(f"   {len(sent)} message(s) to the customer, "
          f"{len(leaked)} containing internal reasoning")

    print("\n   Every decision above has Gemini's reasoning attached to it.")
    print("   Read it with: GET /cases/{case_id}/reasoning\n")


if __name__ == "__main__":
    asyncio.run(main())
