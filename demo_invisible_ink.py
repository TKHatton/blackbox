"""The moment worth remembering: a leak no keyword filter could catch.

An EU-resident customer mentions a cancer diagnosis in a complaint about bank
fees. Four hops later, a different agent writes her a kind letter that does not
contain a single medical word. The gateway blocks it, and can say exactly why.

Run it:

    BLACKBOX_IN_MEMORY=1 python demo_invisible_ink.py

The model is scripted so the run is deterministic and needs no credentials. The
labelling, the propagation, the gateway rules and the taint path are the same
code that runs on Cloud Run.
"""

import asyncio
import os
import sys

os.environ.setdefault("BLACKBOX_IN_MEMORY", "1")
os.environ.setdefault("TRACE_EXPORTER", "none")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "blackbox-demo")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

from fakes import ScriptedLlm, say, think_and_call  # noqa: E402

from blackbox.agents import fleet_tools  # noqa: E402
from blackbox.agents.intake_service import run_intake  # noqa: E402
from blackbox.agents.runtime import agent_run  # noqa: E402
from blackbox.event_store import EventStore  # noqa: E402
from blackbox.labels import Label  # noqa: E402
from blackbox.recorder import Recorder  # noqa: E402
from blackbox.schema import EventType  # noqa: E402
from blackbox.stubs import data  # noqa: E402
from blackbox.stubs.systems import SourceSystems  # noqa: E402
from blackbox.taint import taint_path  # noqa: E402
from blackbox.wiki_store import WikiStore  # noqa: E402

COMPLAINT = data.INBOUND_COMPLAINTS[0]

# Words a keyword filter would look for. None of them appear in the letter.
MEDICAL_VOCABULARY = [
    "cancer", "diagnosis", "diagnosed", "medical", "health", "illness",
    "treatment", "hospital", "disease", "sick", "chemotherapy", "oncology",
]

LETTER = """Dear Ms Brennan,

Thank you for writing to us, and I am sorry this landed on you at what sounds
like an already hard year.

You told us in July that things had become difficult and that your income had
dropped. We should have acted on that, and we did not. The fees kept coming
because nobody recorded what you said, which is our failure and not yours.

We have refunded all four fees and added compensation for the trouble this
caused, EUR 617.00 in total. It will reach your account within three working
days.

If you disagree with any of this, you have 30 days to tell us and we will look
again."""


def rule(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


async def main() -> None:
    store = EventStore(project_id="blackbox-demo")
    wiki = WikiStore(project_id="blackbox-demo", event_store=store, in_memory=True)
    systems = SourceSystems()

    rule("HOP 0. The customer writes to us.")
    print("   Jurisdiction: EU_IE (resident in Ireland), account domiciled in the UK.")
    print("   Her complaint is about fees. In passing, she mentions this:\n")
    sentence = [s for s in COMPLAINT["narrative"].split(".") if "cancer" in s.lower()][0]
    print(f'      "{sentence.strip()}."\n')
    print("   At this moment the label says only MIXED: unexamined free text.")
    print("   Nothing in the system knows yet that it contains health information.")

    intake_model = ScriptedLlm(
        [
            think_and_call(
                "She is disputing arrears fees, but she also says her income dropped "
                "because of a health condition. That is a vulnerability indicator "
                "whether or not anyone logged it.",
                "lookup_customer",
                {"customer_id": COMPLAINT["customer_id"]},
            ),
            think_and_call(
                "CRM360 already carries a financial hardship flag, and she is "
                "resident in Ireland. Consumer protection follows residence, so "
                "EU_IE governs even though the account sits in the UK.",
                "record_intake_determination",
                {
                    "category": "billing_dispute",
                    "severity": "high",
                    "jurisdiction": "EU_IE",
                    "jurisdiction_reasoning": "Resident in Ireland, account domiciled in the UK.",
                    "vulnerability_indicators": True,
                    "vulnerability_reasoning": "Reduced income through a period of ill health, "
                    "and a hardship flag already on file.",
                    "summary": "Four fees charged during disclosed financial difficulty.",
                    "acknowledgment_due_days": 3,
                    "final_response_due_days": 56,
                },
            ),
            say("Case opened."),
        ]
    )

    rule("HOPS 1 to 3. The fact moves through the fleet, changing form each time.")
    result = await run_intake(
        COMPLAINT, store=store, wiki_store=wiki, systems=systems, model=intake_model
    )
    case_id = result["case_id"]

    for event in store.list_events(case_id):
        if not event.labels:
            continue
        label = Label.from_dict(event.labels)
        name = event.payload.get("tool_name") or event.payload.get("decision", "")
        print(
            f"   {event.event_type.value:<12} {str(name)[:34]:<34} "
            f"[{', '.join(sorted(c.value for c in label.classes))}]"
        )

    print("\n   Note what happened between the first line and the last. The words")
    print("   changed completely. The label did not.")

    rule("HOP 4. A different agent writes to her. Read it.")
    print(LETTER)

    rule("Would a keyword filter have caught that letter?")
    found = [w for w in MEDICAL_VOCABULARY if w in LETTER.lower()]
    print(f"   Searched for {len(MEDICAL_VOCABULARY)} medical terms. Found: {found or 'none'}.")
    print("   A regex sees an apology about fees. There is nothing here to match on.")

    rule("The gateway blocks it anyway.")
    recorder = Recorder(case_id=case_id, actor="correspondence_agent", store=store)
    recorder.set_cause(recorder.events()[-1].event_id)
    with agent_run(recorder=recorder, systems=systems, wiki_store=wiki):
        # Nothing is planted here. The Correspondence Agent has not read the
        # complaint and does not know what it said. It reads the case file, and
        # the case file's own label is what restricts the letter.
        out = await fleet_tools.send_customer_letter("final_response", LETTER, "final response")

    print(f"   sent: {out['sent']}")
    print(f"   rule: {out['blocked_by']}")
    print(f"\n   {out['reason']}")

    rule("Why, in four hops.")
    path = taint_path(store, out["policy_check_event_id"])
    for hop in path["hops"]:
        marker = ">>" if hop["newly_restricted_by"] else "  "
        print(f"   {marker} {hop['hop']}. {hop['what_happened']}")
        if hop["newly_restricted_by"]:
            print(f"         attaches {', '.join(hop['newly_restricted_by'])}")
        if hop["source_text"] and "cancer" in hop["source_text"].lower():
            quoted = [s for s in hop["source_text"].split(".") if "cancer" in s.lower()][0]
            print(f'         the original sentence: "{quoted.strip()}."')

    print(f"\n   Final label: [{', '.join(path['final_classes'])}]")
    print(f"   Jurisdictions: {', '.join(path['final_jurisdictions'])}")

    rule("What actually happened here.")
    print("   The label was never attached to the words. It was attached to the fact")
    print("   that an agent read the source. That stays true through any amount of")
    print("   rewording, which is why two model calls and a paraphrase did not")
    print("   shake it off.")
    print()
    print("   Nothing was sent:", len(store.list_events_by_type(case_id, EventType.MESSAGE_SENT)),
          "messages to the customer.")
    print("   The refusal is in the Diary, so it is auditable years from now.")


if __name__ == "__main__":
    asyncio.run(main())
