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
from typing import Any, Dict

from fastapi import Body, FastAPI, HTTPException, Response

from . import approvals, ingest
from .agents.intake_service import case_id_for, run_intake
from .config import get_settings
from .event_store import EventStore
from .fold import fold_events
from .heartbeat import run_heartbeat
from .opentelemetry_setup import configure_tracing
from .recorder import Recorder
from .stubs import data
from .stubs.systems import SourceSystemError, get_source_systems
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
    description="The flight recorder for AI agents. Phase 3: the fleet wakes itself up.",
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    """Liveness probe, and a readable summary of how this instance is configured."""
    settings = get_settings()
    return {
        "status": "ok",
        "phase": "3",
        "project_id": settings.project_id or None,
        "firestore_database": settings.firestore_database,
        "gemini_model": settings.gemini_model,
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
        result = await run_intake(complaint, store=store, wiki_store=get_wiki())
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
        store=get_store(), wiki_store=get_wiki(), systems=get_source_systems()
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


@app.get("/wiki/{page_id:path}")
def get_wiki_page(page_id: str) -> Dict[str, Any]:
    """A Wiki page: what agents read during normal operation."""
    page = get_wiki().get_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"No Wiki page {page_id}")
    return page.to_firestore_dict()


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

    return await run_intake(complaint, store=store, wiki_store=get_wiki())


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
