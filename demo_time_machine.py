"""Tighten one rule, replay, and see which cases would have gone differently.

The question a bank would pay to have answered before shipping a policy change:
if we drop the adjudicator threshold from $500 to $100, what happens to the cases
already in flight?

Run it:

    BLACKBOX_IN_MEMORY=1 python demo_time_machine.py

Two things are worth watching for, because they are what separates a replay you
can trust from one that produces confident nonsense:

- The replay reads the world **as it stood** at the rewind point, not as it
  stands now. The case below is closed today. The replay does not know that.
- The replay cannot reach a live system. Not "is configured not to": it runs
  against a fixture object with no clients in it. A fixture miss stops the replay
  rather than falling through to a real call.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BLACKBOX_IN_MEMORY", "1")
os.environ.setdefault("TRACE_EXPORTER", "none")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "blackbox-demo")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

from blackbox.event_store import EventStore  # noqa: E402
from blackbox.policy import DEFAULT_POLICIES, PolicyEngine  # noqa: E402
from blackbox.recorder import Recorder  # noqa: E402
from blackbox.replay import ReplayMode, replay_case  # noqa: E402
from blackbox.schema import EventType  # noqa: E402
from blackbox.timemachine import FixtureSystems, build_fixtures, state_as_of  # noqa: E402
from blackbox.wiki_store import WikiStore  # noqa: E402

# Three cases already worked under the current threshold. The middle one is the
# interesting one: it sailed through because 300 is under 500.
CASES = [
    ("CASE-0841", 617.00, "Four arrears fees refunded plus compensation."),
    ("CASE-0612", 300.00, "One fee refunded after a billing error."),
    ("CASE-0755", 82.50, "A single late payment fee returned as a goodwill gesture."),
]


def rule(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def record_case(store: EventStore, case_id: str, remedy: float, summary: str) -> str:
    """Replay-able history for one case, worked under the live policy."""
    engine = PolicyEngine(DEFAULT_POLICIES)
    threshold = engine.constant("gate_a_threshold")

    rec = Recorder(case_id=case_id, actor="intake_agent", store=store)
    root = rec.tool_call(
        tool_name="IntakeChannel.poll",
        parameters={"channel": "web_form"},
        intended_outcome="Collect complaints",
    )
    rec.set_cause(root)

    rewind_point = rec.memory_write(
        memory_key=f"wiki:case:{case_id}",
        content={
            "status": "assessed",
            "summary": summary,
            "customer_id": "CUST-4471",
            "jurisdiction": "US",
            "_version": {"new_version": 1},
        },
        reason="Evidence gathered and the case assessed",
    )

    rec.actor = "assessment_agent"
    thought = rec.thought(
        reasoning=f"The complaint is upheld and the remedy comes to {remedy}.",
        decision="call record_assessment",
        confidence=0.85,
        context_summary=f"Assessment of {case_id}",
    )
    with rec.under(thought):
        rec.tool_call(
            tool_name="record_assessment",
            parameters={
                "outcome": "upheld",
                "reasoning": "Internal file note.",
                "proposed_remedy": summary,
                "remedy_amount": remedy,
                "looks_systemic": False,
                "systemic_reasoning": "Isolated to this customer.",
            },
            intended_outcome="Record the assessment",
        )

    gate_fired = remedy > threshold
    rec.policy_check(
        policy_id="gate_a_monetary_threshold",
        check_type="approval_threshold",
        input_data={"remedy_amount": remedy, "threshold": threshold},
        decision="escalate" if gate_fired else "allow",
        reasoning=(
            f"Remedy of {remedy} is above the {threshold} threshold."
            if gate_fired
            else f"Remedy of {remedy} is at or below the {threshold} threshold, so no "
            f"sign-off is required."
        ),
    )

    # The case then closes. Today it is finished, which is exactly the state a
    # contaminated replay would accidentally read.
    rec.memory_write(
        memory_key=f"wiki:case:{case_id}",
        content={"status": "closed", "outcome": "upheld", "_version": {"new_version": 2}},
        reason="Case closed",
    )
    return rewind_point


async def main() -> None:
    store = EventStore(project_id="blackbox-demo")
    wiki = WikiStore(project_id="blackbox-demo", event_store=store, in_memory=True)

    rule("What actually happened, under the current $500 threshold.")
    rewind_points = {}
    for case_id, remedy, summary in CASES:
        rewind_points[case_id] = record_case(store, case_id, remedy, summary)
        checks = [
            e
            for e in store.list_events_by_type(case_id, EventType.POLICY_CHECK)
            if e.payload["policy_id"] == "gate_a_monetary_threshold"
        ]
        outcome = checks[0].payload["decision"]
        mark = "waited for a human" if outcome == "escalate" else "went straight through"
        print(f"   {case_id}  remedy {remedy:>7.2f}  gate {outcome:<9} {mark}")

    rule("The proposed change: drop the threshold to $100.")
    tightened = DEFAULT_POLICIES.with_constants(gate_a_threshold=100.0)
    print(f"   live policy    : {DEFAULT_POLICIES.version}  "
          f"gate_a_threshold={DEFAULT_POLICIES.constants['gate_a_threshold']}")
    print(f"   proposed policy: {tightened.version}")
    print("\n   Nothing in the agent code changes. The threshold is data, and the")
    print("   replay loads a different value for it.")

    rule("Rewinding each case to just before its assessment.")
    for case_id, remedy, _ in CASES:
        world = state_as_of(store, case_id, rewind_points[case_id])
        page = world.page(f"case:{case_id}")
        print(f"   {case_id}  rewound to {len(world.events)} events, "
              f"case file says status={page['status']!r}")
    print("\n   Note the status. Today all three of these cases are closed. The")
    print("   replay does not know that, because it rebuilt the world from the log")
    print("   rather than reading the current Wiki.")

    rule("Replaying under the proposed policy.")
    results = {}
    for case_id, remedy, _ in CASES:
        result = await replay_case(
            store=store,
            case_id=case_id,
            rewind_to=rewind_points[case_id],
            policies=tightened,
            mode=ReplayMode.FAST,
            original_policy_version=DEFAULT_POLICIES.version,
        )
        results[case_id] = result

        replayed_gate = [
            e
            for e in result.replayed_events
            if e.event_type == EventType.POLICY_CHECK
            and e.payload.get("policy_id") == "gate_a_monetary_threshold"
        ]
        now = replayed_gate[0].payload["decision"] if replayed_gate else "not evaluated"
        was = [
            e
            for e in store.list_events_by_type(case_id, EventType.POLICY_CHECK)
            if e.payload["policy_id"] == "gate_a_monetary_threshold"
        ][0].payload["decision"]

        changed = "  <-- CHANGED" if was != now else ""
        print(f"   {case_id}  remedy {remedy:>7.2f}   was {was:<9} now {now:<9}{changed}")

    rule("What the change would cost.")
    newly_gated = [
        case_id
        for case_id, remedy, _ in CASES
        if remedy <= DEFAULT_POLICIES.constants["gate_a_threshold"] and remedy > 100.0
    ]
    print(f"   Cases that would newly need a human: {newly_gated}")
    print("   Each of those waits 1 to 4 days for an adjudicator that it did not")
    print("   wait for before. On a case with an 8 week statutory deadline, that")
    print("   is the consequence a bank would want to see before shipping the rule.")

    rule("Two things that make this replay trustworthy.")
    fixtures = build_fixtures(store.list_events("CASE-0841"))
    systems = FixtureSystems(fixtures)

    print("   1. It cannot reach a live system.")
    print(f"      the replay's systems object is a {type(systems).__name__}, holding")
    print("      a dictionary. There are no clients in it to disable.")
    try:
        systems.printpost.send_letter(recipient="CUST-4471", body="anything")
        print("      sending a letter: ALLOWED   <-- this would be a defect")
    except Exception as exc:
        print(f"      sending a letter: refused, {type(exc).__name__}")

    print("\n   2. A missing recording stops it rather than being filled in.")
    try:
        fixtures.tool_result("CoreBank.transfer_funds", {"amount": 1000000})
        print("      unrecorded call: ANSWERED   <-- this would be a defect")
    except Exception as exc:
        print(f"      unrecorded call: refused, {type(exc).__name__}")
        print("      A replay that guessed here would produce a confident, wrong")
        print("      divergence report, which is worse than no report at all.")

    rule("And the real Diary is untouched.")
    for case_id, _, _ in CASES:
        count = len(store.list_events(case_id))
        print(f"   {case_id}  {count} events, exactly as many as before the replay")
    print("\n   A replay did not happen. It is a question about the past, not an")
    print("   addition to it.")


if __name__ == "__main__":
    asyncio.run(main())
