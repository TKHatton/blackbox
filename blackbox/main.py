"""The Cloud Run service.

Two endpoints do the work, and neither is meant for a human to click:

- ``POST /ingest/poll`` is Cloud Scheduler's target. It checks the inbound
  channels and publishes anything new to Pub/Sub.
- ``POST /pubsub/complaint`` is the Pub/Sub push subscription. A complaint
  landing on the topic is what causes the Intake Agent to run.

The rest are for looking at what happened: the folded state of a case, its causal
tree, and its Wiki page. They read. They never start work.

``POST /debug/intake`` runs the agent directly on a seeded complaint, for
verifying a fresh deployment before the scheduler's first run. It is not part of
the autonomous path.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from . import approvals, ingest
from .agents.fleet_service import advance_case
from .agents.intake_service import case_id_for, run_intake
from .config import get_settings
from .event_store import EventStore
from .fold import fold_events
from .heartbeat import run_heartbeat
from .opentelemetry_setup import configure_tracing
from .eraser import Retraction, retract, retraction_history
from .policy import DEFAULT_POLICIES, get_policy_engine
from .recorder import Recorder
from .replay import ReplayMode, replay_case
from .shadow_service import (
    decide_promotion,
    judge_candidate,
    record_shadow_run,
    run_shadow,
)
from .degradation import score_degradation
from .faults import Fault, FaultType, get_fault_registry
from .immune_service import ImmuneMetrics, run_campaign
from .redteam import AttackFamily, RegressionCorpus
from .schema import EventType as EventTypeRef
from .stunt import AgentVersion
from .tiering import TieringManager
from .timemachine import state_as_of
from .regions import RegionRoutingRefused, evaluate_routing
from .stubs import data
from .stubs.systems import SourceSystemError, get_source_systems


def get_fleet_systems():
    """The source systems the live fleet runs against.

    Wrapped in the fault layer so a fault armed during a demo reaches the agents
    on the next run. With nothing armed, every call passes straight through to
    the genuine stub.
    """
    from .faults import FaultySystems

    return FaultySystems(get_source_systems())
from .taint import blocked_disclosures, summarise_path, taint_path
from .wake import find_open_suspensions
from .wiki_store import WikiStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_stores: Dict[str, Any] = {}


def get_store() -> EventStore:
    """One EventStore for the process, so the in-memory backend persists locally."""
    if "events" not in _stores:
        settings = get_settings()
        _stores["events"] = EventStore(project_id=settings.project_id)
    return _stores["events"]


def get_tiering() -> TieringManager:
    """One TieringManager for the process."""
    if "tiering" not in _stores:
        settings = get_settings()
        _stores["tiering"] = TieringManager(
            project_id=settings.project_id,
            event_store=get_store(),
            hot_ttl_days=settings.hot_ttl_days,
            cold_ttl_days=settings.cold_ttl_days,
            bucket_name=settings.warehouse_bucket or None,
            in_memory=settings.in_memory,
        )
    return _stores["tiering"]


def get_wiki() -> WikiStore:
    """One WikiStore for the process."""
    if "wiki" not in _stores:
        settings = get_settings()
        _stores["wiki"] = WikiStore(project_id=settings.project_id, event_store=get_store())
    return _stores["wiki"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings().apply_genai_env()
    configure_tracing()
    settings = get_settings()
    logger.info(
        "BLACKBOX starting. project=%s database=%s model=%s trace=%s",
        settings.project_id or "(unset)",
        settings.firestore_database,
        settings.gemini_model,
        settings.trace_exporter,
    )
    yield


app = FastAPI(
    title="BLACKBOX",
    description="The flight recorder for AI agents. Phase 10: The Split Screen.",
    version="0.10.0",
    lifespan=lifespan,
)


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    """Liveness probe, and a readable summary of how this instance is configured."""
    settings = get_settings()
    return {
        "status": "ok",
        "phase": "10",
        "project_id": settings.project_id or None,
        "firestore_database": settings.firestore_database,
        "gemini_model": settings.gemini_model,
        "worker_region": settings.worker_region,
        "policy_version": get_policy_engine().policies.version,
        "trace_exporter": settings.trace_exporter,
        "in_memory": settings.in_memory,
    }


# ----------------------------------------------------------------------
# The autonomous path
# ----------------------------------------------------------------------


@app.post("/ingest/poll")
def ingest_poll() -> Dict[str, Any]:
    """Cloud Scheduler target. Publishes complaints that have no case yet."""
    return ingest.poll_channels(store=get_store())


@app.post("/pubsub/complaint")
async def pubsub_complaint(envelope: Dict[str, Any] = Body(...)) -> Response:
    """Pub/Sub push subscription. Runs the Intake Agent on an arriving complaint.

    Answers 204 on success and on a duplicate, so Pub/Sub stops redelivering.
    Answers 400 on a malformed envelope, because retrying it would never help.
    A failure inside the agent answers 500 so Pub/Sub retries.
    """
    try:
        complaint = ingest.decode_push_message(envelope)
    except ValueError as exc:
        logger.error("Rejecting malformed push message: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = get_store()
    case_id = case_id_for(complaint["complaint_ref"])

    if ingest.case_already_open(store, case_id):
        logger.info("Case %s already open, acknowledging duplicate delivery", case_id)
        return Response(status_code=204)

    try:
        result = await run_intake(
            complaint, store=store, wiki_store=get_wiki(), systems=get_fleet_systems()
        )
    except Exception:
        logger.exception("Intake failed for %s", complaint["complaint_ref"])
        raise HTTPException(status_code=500, detail="Intake failed, message will be retried")

    logger.info(
        "Opened %s with %s recorded events", result["case_id"], result["event_count"]
    )
    return Response(status_code=204)


@app.post("/heartbeat")
async def heartbeat() -> Dict[str, Any]:
    """Cloud Scheduler target. Gives suspended agents a chance to wake.

    This is the beat the fleet runs on. It starts no work of its own: it reads
    the wake conditions agents wrote when they suspended, and resumes the ones
    whose condition is now met.
    """
    return await run_heartbeat(
        store=get_store(), wiki_store=get_wiki(), systems=get_fleet_systems()
    )


@app.post("/pubsub/approval")
async def pubsub_approval(envelope: Dict[str, Any] = Body(...)) -> Response:
    """Pub/Sub push. A human approval arriving wakes the case waiting on it."""
    try:
        approval = approvals.decode_envelope(envelope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await approvals.handle_approval(
            approval, store=get_store(), wiki_store=get_wiki(), systems=get_source_systems()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("Approval handling failed")
        raise HTTPException(status_code=500, detail="Approval failed, will be retried")

    logger.info("Approval processed for %s, resumed=%s", result.get("case_id"), result.get("resumed"))
    return Response(status_code=204)


@app.post("/pubsub/customer-reply")
async def pubsub_customer_reply(envelope: Dict[str, Any] = Body(...)) -> Response:
    """Pub/Sub push. A customer replying cuts the appeal window short."""
    try:
        reply = approvals.decode_envelope(envelope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        await approvals.handle_customer_reply(
            reply, store=get_store(), wiki_store=get_wiki(), systems=get_source_systems()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("Customer reply handling failed")
        raise HTTPException(status_code=500, detail="Reply handling failed, will be retried")

    return Response(status_code=204)


# ----------------------------------------------------------------------
# Inspection. Read-only.
# ----------------------------------------------------------------------


@app.get("/cases")
def list_cases() -> Dict[str, Any]:
    """Every seeded complaint and whether a case has been opened for it."""
    store = get_store()
    rows = []
    for complaint in data.INBOUND_COMPLAINTS:
        case_id = case_id_for(complaint["complaint_ref"])
        rows.append(
            {
                "complaint_ref": complaint["complaint_ref"],
                "case_id": case_id,
                "open": ingest.case_already_open(store, case_id),
            }
        )
    return {"cases": rows}


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> Dict[str, Any]:
    """The folded state of a case, computed from its events rather than stored."""
    events = get_store().list_events(case_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"No events for case {case_id}")
    state = fold_events(events)
    return {
        "case_id": state.case_id,
        "current_status": state.current_status,
        "last_updated": state.last_updated.isoformat(),
        "pending_actions": state.pending_actions,
        "event_count": len(state.events),
    }


@app.get("/cases/{case_id}/trace")
def get_case_trace(case_id: str) -> Dict[str, Any]:
    """The case's events as a causal tree, root first."""
    recorder = Recorder(case_id=case_id, actor="inspector", store=get_store())
    if not recorder.events():
        raise HTTPException(status_code=404, detail=f"No events for case {case_id}")
    tree = recorder.causal_tree()
    try:
        recorder.assert_causally_complete()
        tree["causally_complete"] = True
    except AssertionError as exc:
        tree["causally_complete"] = False
        tree["causal_defect"] = str(exc)
    return tree


@app.get("/cases/{case_id}/reasoning")
def get_case_reasoning(case_id: str) -> Dict[str, Any]:
    """Just the THOUGHT events: Gemini's rationale, in order.

    The reasoning chain is the point of the recorder, so it gets its own endpoint
    rather than being something you filter out of the full log by hand.
    """
    from .schema import EventType

    events = get_store().list_events_by_type(case_id, EventType.THOUGHT)
    if not events:
        raise HTTPException(status_code=404, detail=f"No reasoning recorded for {case_id}")
    return {
        "case_id": case_id,
        "thoughts": [
            {
                "event_id": e.event_id,
                "actor": e.actor,
                "timestamp": e.timestamp.isoformat(),
                "reasoning": e.payload.get("reasoning"),
                "decision": e.payload.get("decision"),
            }
            for e in events
        ],
    }


@app.get("/cases/{case_id}/blocked")
def get_blocked_disclosures(case_id: str) -> Dict[str, Any]:
    """Every disclosure the gateway refused on this case, and why."""
    return {"case_id": case_id, "blocked": blocked_disclosures(get_store(), case_id)}


@app.get("/taint/{event_id}")
def get_taint_path(event_id: str) -> Dict[str, Any]:
    """Trace a labelled action back to the data that shaped it.

    Point this at a blocked POLICY_CHECK and it returns the chain from the
    original source field to the attempted disclosure, one entry per hop, showing
    where each restriction attached and that it never came off.
    """
    path = taint_path(get_store(), event_id)
    if not path.get("found"):
        raise HTTPException(status_code=404, detail=f"No event {event_id}")
    path["rendered"] = summarise_path(path)
    return path


@app.get("/wiki/{page_id:path}")
def get_wiki_page(page_id: str) -> Dict[str, Any]:
    """A Wiki page: what agents read during normal operation.

    Answers 451 when region pinning refuses the read from this instance. That is
    the honest status for content withheld on legal grounds, and it makes the
    refusal visible to a caller rather than looking like a server fault.
    """
    try:
        page = get_wiki().get_page(page_id)
    except RegionRoutingRefused as refused:
        raise HTTPException(
            status_code=451,
            detail={
                "refused": "region_pinning",
                "page_region": refused.page_region,
                "worker_region": refused.worker_region,
                "reasoning": refused.reasoning,
            },
        ) from refused
    if page is None:
        raise HTTPException(status_code=404, detail=f"No Wiki page {page_id}")
    return page.to_firestore_dict()


@app.post("/retractions")
async def create_retraction(request: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Withdraw a fact and cascade the consequences through derived memory.

    The values being withdrawn are used to verify the regenerated pages and are
    never written to the Diary, which cannot forget.
    """
    subject = request.get("subject")
    if not subject:
        raise HTTPException(status_code=400, detail="A retraction needs a subject")

    retraction = Retraction(
        subject=subject,
        fields=request.get("fields", []),
        reason=request.get("reason", "Not stated"),
        requested_by=request.get("requested_by", "unknown"),
        values=request.get("values", []),
    )
    result = await retract(
        retraction, store=get_store(), wiki_store=get_wiki(),
        regenerate=request.get("regenerate", True),
    )
    return {
        "retraction_id": result.retraction_id,
        "subject": result.subject,
        "retract_event_id": result.retract_event_id,
        "pages_reached": result.pages_reached,
        "max_depth": result.max_depth,
        "directly_affected": result.directly_affected,
        "invalidated": result.invalidated,
        "regenerated": result.regenerated,
        "held_invalid": result.held_invalid,
    }


@app.get("/retractions")
def list_retractions() -> Dict[str, Any]:
    """Every retraction performed.

    The content is gone from the Wiki. This is the proof it was there and that
    somebody withdrew it, which is what an auditor asks for.
    """
    return {"retractions": retraction_history(get_store())}


_immune: Dict[str, Any] = {}


def get_corpus() -> RegressionCorpus:
    """The regression corpus, which only ever grows."""
    if "corpus" not in _immune:
        from pathlib import Path

        _immune["corpus"] = RegressionCorpus(Path("/tmp/blackbox-corpus.json"))
    return _immune["corpus"]


def get_immune_metrics() -> ImmuneMetrics:
    if "metrics" not in _immune:
        _immune["metrics"] = ImmuneMetrics()
    return _immune["metrics"]


@app.get("/", response_class=HTMLResponse)
def split_screen() -> str:
    """The Split Screen: nine phases of plumbing, made visible.

    Served from the same process as the API it reads, so everything on the page
    is live data rather than a mock.
    """
    from .ui import SPLIT_SCREEN_HTML

    return SPLIT_SCREEN_HTML


@app.get("/stream/reasoning")
async def stream_reasoning(request: Request, case_id: str = "", after: str = ""):
    """Stream Gemini's reasoning as it is recorded.

    Server-sent events. Reasoning shown as a collapsed log requiring clicks to
    expand is a named failure mode for the Split Screen, so this pushes each
    THOUGHT as it lands rather than waiting to be asked.
    """
    import asyncio
    import json as _json

    async def events():
        cursor = after
        idle = 0
        while idle < 600:
            if await request.is_disconnected():
                break
            try:
                store = get_store()
                if case_id:
                    found = store.list_events_by_type(case_id, EventTypeRef.THOUGHT)
                else:
                    found = store.scan_events_by_type(EventTypeRef.THOUGHT, limit=200)
                fresh = [e for e in found if e.event_id > cursor]
                fresh.sort(key=lambda e: e.event_id)
            except Exception as exc:  # pragma: no cover - depends on the store
                problem = _json.dumps({"error": str(exc)})
                yield "event: error\ndata: " + problem + "\n\n"
                await asyncio.sleep(3)
                continue

            for event in fresh[-40:]:
                cursor = event.event_id
                idle = 0
                payload = {
                    "event_id": event.event_id,
                    "case_id": event.case_id,
                    "actor": event.actor,
                    "timestamp": event.timestamp.isoformat(),
                    "reasoning": event.payload.get("reasoning", ""),
                    "decision": event.payload.get("decision", ""),
                    "labels": event.labels or {},
                }
                yield "data: " + _json.dumps(payload) + "\n\n"

            if not fresh:
                idle += 1
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/overview")
def overview() -> Dict[str, Any]:
    """Everything the Split Screen needs to open, in one call."""
    store = get_store()
    settings = get_settings()

    cases = []
    for complaint in data.INBOUND_COMPLAINTS:
        case_id = case_id_for(complaint["complaint_ref"])
        events = store.list_events(case_id)
        if not events:
            cases.append({"case_id": case_id, "open": False, "events": 0})
            continue
        state = fold_events(events)
        page = None
        try:
            page = get_wiki().get_page(f"case:{case_id}", enforce_region=False)
        except Exception:  # pragma: no cover
            page = None
        cases.append(
            {
                "case_id": case_id,
                "open": True,
                "events": len(events),
                "status": state.current_status,
                "jurisdiction": (page.content.get("jurisdiction") if page else None),
                "vulnerable": (page.content.get("vulnerability_indicators") if page else None),
                "blocked": len(blocked_disclosures(store, case_id)),
            }
        )

    try:
        suspensions = len(find_open_suspensions(store))
    except Exception:  # pragma: no cover
        suspensions = 0

    return {
        "phase": "10",
        "project": settings.project_id or None,
        "model": settings.gemini_model,
        "worker_region": settings.worker_region,
        "policy_version": get_policy_engine().policies.version,
        "cases": cases,
        "open_suspensions": suspensions,
        "corpus_size": get_corpus().size,
        "faults_armed": len(get_fault_registry().active()),
    }


@app.post("/faults/arm")
def arm_fault(request: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Break something, live, without a redeploy.

    The fault surfaces as a tool result the agent reads, not as an exception the
    infrastructure swallows. A fault the agents never see would prove nothing.

    Body: fault_type, optionally system, method, count, and detail.
    """
    try:
        fault_type = FaultType(request.get("fault_type", ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown fault type. Known: {[f.value for f in FaultType]}",
        ) from exc

    fault = get_fault_registry().arm(
        Fault(
            fault_type=fault_type,
            target_system=request.get("system", ""),
            target_method=request.get("method", ""),
            remaining=request.get("count"),
            detail=request.get("detail") or {},
        )
    )
    return {"armed": fault.to_dict(), "active": len(get_fault_registry().active())}


@app.post("/faults/disarm")
def disarm_faults() -> Dict[str, Any]:
    """Put everything back."""
    return {"disarmed": get_fault_registry().disarm_all()}


@app.get("/faults")
def list_faults() -> Dict[str, Any]:
    """What is currently broken."""
    return get_fault_registry().to_dict()


@app.get("/cases/{case_id}/degradation")
def case_degradation(case_id: str) -> Dict[str, Any]:
    """How the fleet behaved when something broke on this case.

    Four outcomes and only one is a failure: recovered, escalated, halted safely,
    or proceeded on bad data. The last is the one this phase exists to detect,
    because it is the outcome that looks fine in a log.
    """
    events = get_store().list_events(case_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"No events for case {case_id}")
    return score_degradation(events).to_dict()


@app.post("/redteam/campaign")
async def redteam_campaign(request: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Run one red team pass: invent new attacks, then re-run the whole corpus.

    An attack counts as a success only when a policy boundary was crossed,
    checked from recorded events. An agent that sounded rattled while holding
    every boundary is scored a failure, which is the honest measure.

    Deliberately its own endpoint. Attacks run in scratch stores, so a successful
    one leaves no fraudulent refund in the live Diary.
    """
    version = request.get("version") or "unversioned"
    per_family = int(request.get("per_family", 1))
    families = request.get("families")
    selected = [AttackFamily(f) for f in families] if families else None

    corpus = get_corpus()
    campaign = await run_campaign(
        version=version,
        corpus=corpus,
        families=selected,
        per_family=per_family,
        systems=get_source_systems(),
    )
    corpus.save()
    get_immune_metrics().record(campaign)
    return campaign.to_dict()


@app.get("/redteam/corpus")
def redteam_corpus() -> Dict[str, Any]:
    """Every attack that has ever worked, kept forever."""
    corpus = get_corpus()
    return {
        "size": corpus.size,
        "attacks": [
            {
                "attack_id": entry.attack.attack_id,
                "family": entry.attack.family.value,
                "payload": entry.attack.payload,
                "first_succeeded_at": entry.first_succeeded_at.isoformat(),
                "boundaries": entry.boundaries,
                "runs": entry.history,
            }
            for entry in corpus.entries.values()
        ],
    }


@app.get("/redteam/metrics")
def redteam_metrics() -> Dict[str, Any]:
    """Attack success rate and corpus size over time.

    The two curves only mean something together: a falling rate against a
    growing corpus is hardening, while a falling rate against a fixed set of
    attacks just means somebody patched those attacks.
    """
    metrics = get_immune_metrics()
    return {"points": metrics.points, "chart": metrics.render()}


@app.post("/tiering/run")
def run_tiering() -> Dict[str, Any]:
    """Cloud Scheduler target. Moves aged events outward through the shelves.

    Copies to BigQuery, reads each event back to confirm it arrived intact, and
    only then removes it from Firestore. An event that fails verification stays
    on the Desk and is reported, because Firestore staying too big is a cost
    problem while a lost event is not recoverable.
    """
    tiering = get_tiering()
    try:
        tiering.ensure_schema()
    except Exception as exc:
        logger.warning("Could not ensure the BigQuery schema: %s", exc)

    filing = tiering.tier_to_filing_cabinet()
    warehouse = tiering.archive_to_warehouse()

    return {
        "filing_cabinet": filing.to_dict(),
        "warehouse": warehouse.to_dict(),
        "shelf_counts": tiering.shelf_counts(),
    }


@app.get("/shelves")
def shelf_counts() -> Dict[str, Any]:
    """How many events sit on each shelf.

    The number that matters is the Desk's. It should stay roughly flat as the
    system runs rather than climbing forever, which is the whole point of Phase
    1.5 and the thing the earlier stub failed to deliver.
    """
    return get_tiering().shelf_counts()


@app.post("/shadow")
async def shadow_candidate(request: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Run a candidate agent version in shadow across cases, and judge it.

    The candidate cannot change anything: it writes into scratch stores and its
    outbound calls are refused. Deliberately its own endpoint rather than
    something the live path invokes, so a slow candidate cannot add latency to
    the fleet.

    Body: version_id, agent_name, optionally instruction, and case_ids.
    """
    version_id = request.get("version_id")
    agent_name = request.get("agent_name")
    case_ids = request.get("case_ids") or []
    if not version_id or not agent_name:
        raise HTTPException(
            status_code=400, detail="version_id and agent_name are required"
        )
    if not case_ids:
        raise HTTPException(status_code=400, detail="case_ids must name at least one case")

    candidate = AgentVersion(
        version_id=version_id,
        agent_name=agent_name,
        description=request.get("description", ""),
        instruction=request.get("instruction"),
    )

    store = get_store()
    runs = []
    for case_id in case_ids:
        try:
            run = await run_shadow(
                case_id=case_id,
                candidate=candidate,
                live_store=store,
                live_wiki=get_wiki(),
                systems=get_source_systems(),
            )
        except Exception:
            logger.exception("Shadow run failed for %s", case_id)
            continue
        record_shadow_run(run, store)
        runs.append(run)

    if not runs:
        raise HTTPException(status_code=400, detail="No case could be shadowed")

    report = await judge_candidate(runs, version_id=version_id)
    decision = decide_promotion(report)

    return {
        "version_id": version_id,
        "cases_shadowed": [r.case_id for r in runs],
        "blocked_writes": sum(len(r.blocked_writes) for r in runs),
        "promotion": decision.to_dict(),
    }


@app.get("/policies")
def get_policies() -> Dict[str, Any]:
    """The rules the fleet is running under, as data.

    Governance rules live here rather than compiled into the agents, which is
    what makes a replay under an altered rule possible at all.
    """
    return get_policy_engine().policies.to_dict()


@app.get("/cases/{case_id}/as-of/{event_id}")
def get_state_as_of(case_id: str, event_id: str) -> Dict[str, Any]:
    """The world as it stood immediately after an event.

    Rebuilt from the log, not read from current state. This is the ground a
    replay stands on, so it is worth being able to look at directly.
    """
    try:
        world = state_as_of(get_store(), case_id, event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "case_id": world.case_id,
        "rewind_to": world.rewind_to,
        "rewind_at": world.rewind_at.isoformat() if world.rewind_at else None,
        "events_in_window": len(world.events),
        "status_at_that_point": world.state.current_status,
        "wiki_as_it_stood": world.wiki_pages,
    }


@app.post("/replay")
async def run_replay(request: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Rewind a case, alter a rule, and report what would have happened instead.

    The replay reads recorded tool responses and never touches a live system. A
    missing recording stops it rather than falling through to a real call.

    Body: case_id, rewind_to, and either constants (a mapping of policy constants
    to override) or nothing for a control run. mode is "fast" or "fresh".
    """
    case_id = request.get("case_id")
    rewind_to = request.get("rewind_to")
    if not case_id or not rewind_to:
        raise HTTPException(status_code=400, detail="case_id and rewind_to are required")

    overrides = request.get("constants") or {}
    policies = DEFAULT_POLICIES.with_constants(**overrides) if overrides else DEFAULT_POLICIES

    try:
        mode = ReplayMode(request.get("mode", "fast"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown replay mode: {exc}") from exc

    try:
        result = await replay_case(
            store=get_store(),
            case_id=case_id,
            rewind_to=rewind_to,
            policies=policies,
            mode=mode,
            original_policy_version=DEFAULT_POLICIES.version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result.to_dict()


@app.get("/regions/check")
def check_region_routing(page_id: str, jurisdiction: str = "") -> Dict[str, Any]:
    """Would this instance be allowed to read a page pinned to that jurisdiction?"""
    settings = get_settings()
    decision = evaluate_routing(page_id, jurisdiction or None, settings.worker_region)
    return {
        "allowed": decision.allowed,
        "page_region": decision.page_region,
        "worker_region": decision.worker_region,
        "reasoning": decision.reasoning,
    }


# ----------------------------------------------------------------------
# Stub source systems, exposed so the estate is inspectable
# ----------------------------------------------------------------------


@app.get("/stubs/crm360/customers/{customer_id}")
def stub_customer(customer_id: str) -> Dict[str, Any]:
    """CRM360 customer profile."""
    try:
        return get_source_systems().crm360.get_customer(customer_id)
    except SourceSystemError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/stubs/corebank/accounts/{account_id}")
def stub_account(account_id: str) -> Dict[str, Any]:
    """CoreBank account record."""
    try:
        return get_source_systems().corebank.get_account(account_id)
    except SourceSystemError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/stubs/commsvault/jobs/{job_id}")
def stub_commsvault_job(job_id: str) -> Dict[str, Any]:
    """CommsVault retrieval job status. PENDING until its ready-at time."""
    try:
        return get_source_systems().commsvault.poll(job_id)
    except SourceSystemError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ----------------------------------------------------------------------
# Deployment check. Not part of the autonomous path.
# ----------------------------------------------------------------------


@app.post("/debug/intake/{complaint_ref}")
async def debug_intake(complaint_ref: str) -> Dict[str, Any]:
    """Run the Intake Agent on one seeded complaint and return what it decided.

    For confirming a fresh deployment can reach Vertex AI and Firestore. The
    autonomous path does not use this, and removing it would not change how the
    fleet behaves.
    """
    try:
        complaint = data.find_complaint(complaint_ref)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    store = get_store()
    case_id = case_id_for(complaint_ref)
    if ingest.case_already_open(store, case_id):
        raise HTTPException(
            status_code=409,
            detail=f"Case {case_id} is already open. The Diary is append-only, so it "
            f"cannot be reopened. Inspect it at /cases/{case_id}/trace.",
        )

    return await run_intake(
        complaint, store=store, wiki_store=get_wiki(), systems=get_fleet_systems()
    )


@app.post("/debug/advance/{case_id}")
async def debug_advance(case_id: str) -> Dict[str, Any]:
    """Give the coordinator one tick over an open, non-suspended case.

    In the autonomous path, a case advances when the heartbeat's compliance
    review picks it up (a deadline coming due) or when a suspension wakes. A
    freshly opened case with a deadline weeks out matches neither, so it would
    otherwise sit at Intake until something else nudges it. This is that nudge,
    for confirming a deployment behaves as documented. The autonomous path does
    not call this, and removing it would not change how the fleet behaves once
    a case is already in motion.

    One call is one hop: the coordinator decides one next agent and hands off,
    same as one heartbeat tick would. A case with several hops ahead of it, like
    Invisible Ink's cross-border trace, needs this called more than once.
    """
    return await advance_case(
        case_id=case_id,
        store=get_store(),
        wiki_store=get_wiki(),
        systems=get_fleet_systems(),
        trigger="debug: manual advance",
    )


@app.post("/debug/heartbeat_fast_forward")
async def debug_heartbeat_fast_forward(days: int = 10) -> Dict[str, Any]:
    """Run a heartbeat beat as of some days in the future, not the wall clock.

    ``run_heartbeat`` already accepts ``now`` so a demonstration can compress a
    wait without faking the mechanism; nothing wired this to an endpoint. Wake
    conditions like CommsVault's real turnaround (days) are genuine, not
    theatrical, so there is no honest way to make one fire on the actual clock
    inside a recording window. This evaluates the same real condition, just
    checked as of a later moment, exactly what the production heartbeat would
    do days from now. The autonomous path calls ``/heartbeat`` with no override
    and never this endpoint.
    """
    future = datetime.now(timezone.utc) + timedelta(days=days)
    return await run_heartbeat(
        store=get_store(), wiki_store=get_wiki(), systems=get_fleet_systems(), now=future
    )


@app.post("/debug/commsvault/reseed_job")
def debug_reseed_commsvault_job(
    job_id: str, customer_id: str, ready_at: str
) -> Dict[str, Any]:
    """Put a CommsVault job back into memory after an instance recycled.

    CommsVault job state lives only in the process, documented as a known gap:
    a recycled instance answers "unknown job" to a job another instance created,
    and the case stays correctly suspended rather than resuming without records.
    That is the safe failure, but it means a suspend event survives a redeploy
    and its underlying job does not. This reconstructs a lost job from the exact
    values the original SUSPEND event recorded (job_id, ready_at) plus the
    customer_id the case is actually about, so the same real CommsVault wait can
    resolve instead of hanging forever. Nothing on the autonomous path calls
    this or needs to.
    """
    try:
        ready = datetime.fromisoformat(ready_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Bad ready_at: {exc}") from exc

    # get_fleet_systems() wraps the real stub estate in a fault-injection proxy
    # whose __getattr__ treats any attribute as a method call. Reach through
    # ._target to the actual CommsVault singleton to touch its job dict.
    commsvault = get_fleet_systems().commsvault._target
    commsvault._jobs[job_id] = {
        "customer_id": customer_id,
        "reason": "reseeded after instance recycle",
        "ready_at": ready,
    }
    return {"reseeded": job_id, "customer_id": customer_id, "ready_at": ready.isoformat()}


@app.get("/suspensions")
def list_suspensions() -> Dict[str, Any]:
    """Every wait the fleet is currently holding, and what would end it.

    Read straight out of the Diary, which is the point: this list is identical
    no matter which instance answers or how long it has been running.
    """
    rows = []
    for suspension in find_open_suspensions(get_store()):
        rows.append(
            {
                "case_id": suspension.case_id,
                "suspend_event_id": suspension.suspend_event_id,
                "suspended_at": suspension.suspended_at.isoformat(),
                "waiting_agent": suspension.condition.resume_agent,
                "condition_type": suspension.condition.type.value,
                "wakes_when": suspension.condition.description,
                "not_before": (
                    suspension.condition.earliest_wake_at.isoformat()
                    if suspension.condition.earliest_wake_at
                    else None
                ),
            }
        )
    return {"open_suspensions": len(rows), "suspensions": rows}
