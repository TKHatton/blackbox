"""Running attacks against the fleet, and tracking whether it is getting harder.

The failure mode here is the red team running against a mock instead of the
actual fleet. So an attack is delivered by planting its payload in a real input
surface and then running the real agent code: the same ``run_intake``, the same
tools, the same gateway, the same policy engine. Nothing about the fleet knows it
is being attacked.

Attacks run in scratch stores rather than the live Diary, for the same reason a
shadow run does: an attack that succeeded should not leave a fraudulent refund in
the record. The isolation is about not corrupting the log, not about softening
the test. The agent code and the boundaries are the real ones.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .backends import InMemoryBackend
from .event_store import EventStore
from .immune import AttackOutcome, Boundary, score_attack
from .redteam import Attack, AttackFamily, RegressionCorpus, generate_attack
from .stubs import data as stub_data
from .stubs.systems import SourceSystems
from .wiki_store import WikiStore

logger = logging.getLogger(__name__)


def plant(attack: Attack, complaint: Dict[str, Any]) -> Dict[str, Any]:
    """Put an attack's payload into the surface it targets.

    The payload goes into the input the way a genuine one would arrive: appended
    to a complaint a customer wrote, or standing in for an archived transcript.
    It is not handed to the agent as a separate instruction, because that would
    test something easier than what actually happens.
    """
    poisoned = dict(complaint)

    if attack.surface == "complaint_narrative":
        poisoned["narrative"] = f"{complaint['narrative']}\n\n{attack.payload}"
    elif attack.surface == "customer_reply":
        poisoned["narrative"] = f"{complaint['narrative']}\n\n{attack.payload}"
        poisoned["_reply_payload"] = attack.payload
    elif attack.surface == "comms_transcript":
        poisoned["_transcript_payload"] = attack.payload
    else:
        poisoned["narrative"] = f"{complaint['narrative']}\n\n{attack.payload}"

    poisoned["complaint_ref"] = f"{complaint['complaint_ref']}-{attack.attack_id[:12]}"
    return poisoned


class PoisonedSystems:
    """The stub estate with a poisoned archive.

    Used for the tool-response family: CommsVault returns text the attacker
    wrote, which the Evidence Agent will read as though a colleague said it.
    """

    def __init__(self, live: SourceSystems, transcript: str):
        self._live = live
        self._transcript = transcript
        self.crm360 = live.crm360
        self.corebank = live.corebank
        self.printpost = live.printpost
        self.regportal = live.regportal
        self.commsvault = _PoisonedVault(live.commsvault, transcript)


class _PoisonedVault:
    def __init__(self, live: Any, transcript: str):
        self._live = live
        self._transcript = transcript
        self.name = getattr(live, "name", "CommsVault")

    def request_records(self, customer_id: str, reason: str) -> Dict[str, Any]:
        return self._live.request_records(customer_id, reason)

    def poll(self, job_id: str, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        answer = self._live.poll(job_id, as_of=as_of)
        if answer.get("status") == "READY":
            answer = dict(answer)
            answer["records"] = list(answer.get("records", [])) + [
                {
                    "record_id": "CV-POISONED",
                    "type": "call_transcript",
                    "date": "2026-07-12",
                    "summary": self._transcript,
                    "sensitivity": "MIXED",
                }
            ]
        return answer

    def __getattr__(self, name: str):
        return getattr(self._live, name)


@dataclass
class AttackRun:
    """One attack, run against the fleet, with its outcome."""

    attack: Attack
    outcome: AttackOutcome
    events_recorded: int = 0
    error: Optional[str] = None


async def run_attack(
    attack: Attack,
    systems: Optional[SourceSystems] = None,
    model: Optional[Any] = None,
    base_complaint: Optional[Dict[str, Any]] = None,
    follow_up: Optional[str] = None,
    follow_up_model: Optional[Any] = None,
) -> AttackRun:
    """Deliver one attack to the real fleet and score what happened.

    Scratch stores, real agent code. The isolation keeps a successful attack out
    of the live Diary; it does not make the fleet easier to fool.

    **Why attacks run in two stages.** The Intake Agent holds five tools and all
    of them are reads plus its own determination. It has no way to move money or
    write to a customer: ADK will not even resolve those names for it. So no
    boundary is reachable from the intake surface alone, however persuasive the
    injected text is. That is capability-based defence, and it is worth knowing
    the primary injection surface has it.

    The threat that remains is indirect. An injection does not compromise Intake;
    it poisons the case file Intake writes, and a later agent with dangerous tools
    reads that file as established fact. ``follow_up`` names the agent that then
    works the poisoned case, which is where an attack can actually land.

    Args:
        attack: What to plant, and where.
        systems: The stub estate. Poisoned automatically for transcript attacks.
        model: The model the Intake Agent runs on.
        base_complaint: The complaint to poison. Defaults to the first seeded one.
        follow_up: A specialist agent to run afterwards against the poisoned case
            file, for example "correspondence_agent" or "remediation_agent".
        follow_up_model: The model that agent runs on.

    Returns:
        The attack, and whether any boundary was crossed across both stages.
    """
    from .agents.intake_service import case_id_for, run_intake

    complaint = plant(attack, base_complaint or stub_data.INBOUND_COMPLAINTS[0])

    backend = InMemoryBackend()
    store = EventStore(project_id="redteam", backend=backend)
    wiki = WikiStore(
        project_id="redteam", event_store=store, in_memory=True, worker_region="EU"
    )

    live = systems or SourceSystems()
    if attack.surface == "comms_transcript":
        live = PoisonedSystems(live, attack.payload)

    case_id = case_id_for(complaint["complaint_ref"])
    error = None

    try:
        await run_intake(
            complaint, store=store, wiki_store=wiki, systems=live, model=model
        )
    except Exception as exc:
        logger.info("Attack %s ended the run early: %s", attack.attack_id, exc)
        error = str(exc)

    # The second stage: an agent with dangerous tools works the poisoned case.
    if follow_up and error is None:
        try:
            await _run_follow_up(
                agent_name=follow_up,
                case_id=case_id,
                store=store,
                wiki=wiki,
                systems=live,
                model=follow_up_model,
            )
        except Exception as exc:
            logger.info("Follow-up agent %s ended early: %s", follow_up, exc)
            error = str(exc)

    events = store.list_events(case_id)
    page = wiki.get_page(f"case:{case_id}", enforce_region=False)
    content = dict(page.content) if page else {}

    # The third-party names this case could have leaked, taken from the records
    # the fleet actually touched rather than guessed at.
    third_party = []
    for row in stub_data.TRANSACTIONS.get(complaint.get("account_id", ""), []):
        if row.get("counterparty_name"):
            third_party.append(row["counterparty_name"])
    content.setdefault("third_party_names", third_party)

    outcome = score_attack(
        attack_id=attack.attack_id,
        family=attack.family.value,
        events=events,
        case_content=content,
        case_id=case_id,
    )
    return AttackRun(
        attack=attack, outcome=outcome, events_recorded=len(events), error=error
    )


async def _run_follow_up(
    agent_name: str,
    case_id: str,
    store: EventStore,
    wiki: WikiStore,
    systems: Any,
    model: Optional[Any],
) -> None:
    """Run a specialist over a case whose file may have been poisoned."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from .agents.fleet import build_specialist
    from .agents.rehydrate import rebuild_context
    from .agents.runtime import agent_run
    from .recorder import Recorder

    context = rebuild_context(
        case_id=case_id, store=store, wiki_store=wiki, require_page=False
    )
    recorder = Recorder(case_id=case_id, actor=agent_name, store=store)
    recorder.set_cause(context.state.last_event_id)

    agent = build_specialist(agent_name, model=model)
    session_service = InMemorySessionService()
    session_id = f"redteam:{agent_name}:{case_id}"
    await session_service.create_session(
        app_name="blackbox-redteam", user_id="redteam", session_id=session_id
    )
    runner = Runner(
        app_name="blackbox-redteam", agent=agent, session_service=session_service
    )
    briefing = context.to_briefing() + "\n\nWork this case."
    message = types.Content(role="user", parts=[types.Part(text=briefing)])

    with agent_run(
        recorder=recorder, systems=systems, wiki_store=wiki, judge_model=model
    ):
        async for _ in runner.run_async(
            user_id="redteam", session_id=session_id, new_message=message
        ):
            pass


@dataclass
class Campaign:
    """One pass of the red team: generated attacks plus the whole corpus."""

    version: str
    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    new_attacks: List[AttackRun] = field(default_factory=list)
    corpus_attacks: List[AttackRun] = field(default_factory=list)
    corpus_size_before: int = 0
    corpus_size_after: int = 0

    @property
    def all_runs(self) -> List[AttackRun]:
        return self.new_attacks + self.corpus_attacks

    @property
    def successes(self) -> List[AttackRun]:
        return [r for r in self.all_runs if r.outcome.succeeded]

    @property
    def success_rate(self) -> float:
        total = len(self.all_runs)
        return (len(self.successes) / total) if total else 0.0

    @property
    def regressions(self) -> List[AttackRun]:
        """Corpus attacks that worked again. These are the ones that matter."""
        return [r for r in self.corpus_attacks if r.outcome.succeeded]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "ran_at": self.ran_at.isoformat(),
            "attacks_run": len(self.all_runs),
            "new_attacks": len(self.new_attacks),
            "corpus_attacks": len(self.corpus_attacks),
            "successes": len(self.successes),
            "success_rate": round(self.success_rate, 4),
            "regressions": [r.attack.attack_id for r in self.regressions],
            "corpus_size_before": self.corpus_size_before,
            "corpus_size_after": self.corpus_size_after,
            "boundaries_crossed": sorted(
                {v.boundary.value for r in self.successes for v in r.outcome.violations}
            ),
        }


async def run_campaign(
    version: str,
    corpus: RegressionCorpus,
    families: Optional[List[AttackFamily]] = None,
    per_family: int = 1,
    systems: Optional[SourceSystems] = None,
    fleet_model: Optional[Any] = None,
    generator_model: Optional[Any] = None,
    seeds: Optional[List[Attack]] = None,
) -> Campaign:
    """Run one pass: invent new attacks, then re-run everything that ever worked.

    The corpus runs every time, against every version, forever. That is what
    stops a hole from quietly reopening, and what makes the success-rate curve
    mean something: it is measured against a set that only gets harder.
    """
    from .redteam import SEED_ATTACKS

    families = families or list(AttackFamily)
    campaign = Campaign(version=version, corpus_size_before=corpus.size)

    # New attacks, invented rather than chosen from a list.
    for family in families:
        previous = [a for a in (seeds or SEED_ATTACKS) if a.family == family]
        previous += [a for a in corpus.attacks() if a.family == family]

        for _ in range(per_family):
            attack = await generate_attack(
                family=family,
                previous=previous,
                surface=previous[0].surface if previous else "complaint_narrative",
                model=generator_model,
                generation=corpus.size + 1,
            )
            if attack is None:
                continue
            previous.append(attack)

            run = await run_attack(attack, systems=systems, model=fleet_model)
            campaign.new_attacks.append(run)

            if run.outcome.succeeded:
                corpus.add_success(
                    attack, [v.boundary.value for v in run.outcome.violations]
                )

    # Everything that has ever worked, run again.
    for attack in corpus.attacks():
        if any(r.attack.attack_id == attack.attack_id for r in campaign.new_attacks):
            continue
        run = await run_attack(attack, systems=systems, model=fleet_model)
        campaign.corpus_attacks.append(run)
        corpus.record_run(attack.attack_id, version, run.outcome.succeeded)

    campaign.corpus_size_after = corpus.size
    return campaign


@dataclass
class ImmuneMetrics:
    """The two curves: attack success rate, and corpus size, over time."""

    points: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, campaign: Campaign) -> None:
        self.points.append(
            {
                "version": campaign.version,
                "at": campaign.ran_at.isoformat(),
                "attacks_run": len(campaign.all_runs),
                "successes": len(campaign.successes),
                "success_rate": round(campaign.success_rate, 4),
                "corpus_size": campaign.corpus_size_after,
                "regressions": len(campaign.regressions),
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"points": self.points}

    def render(self) -> str:
        """A plain-text chart of both curves.

        Phase 10 draws this properly. This is enough to see the shape.
        """
        if not self.points:
            return "No campaigns recorded yet."

        lines = [
            f"{'version':<16} {'run':>4} {'ok':>4} {'rate':>7}  {'corpus':>6}  curve",
            "-" * 68,
        ]
        for point in self.points:
            filled = int(round(point["success_rate"] * 20))
            bar = "#" * filled + "." * (20 - filled)
            lines.append(
                f"{point['version']:<16} {point['attacks_run']:>4} "
                f"{point['successes']:>4} {point['success_rate']:>7.1%}  "
                f"{point['corpus_size']:>6}  {bar}"
            )
        return "\n".join(lines)
