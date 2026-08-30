"""Running the fleet: advancing a case, and resuming a suspended one.

Two entry points, and the difference between them is the whole of Phase 3.

``advance_case`` runs the coordinator over a case that is awake. The coordinator
reads the case file, decides who should act, and ADK transfers to them.

``resume_case`` runs a specific agent over a case that was asleep. It is called
when something has made a wake condition true. The agent that wakes has no memory
of the case, so its context is rebuilt from the Wiki and the fold first, and the
RESUME event is written before it acts.

Neither function polls. Neither holds a process open across a wait. An agent that
suspends writes its SUSPEND event and the run ends normally.
"""

import logging
from typing import Any, Dict, Optional

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from ..config import get_settings
from ..event_store import EventStore
from ..recorder import Recorder
from ..stubs.systems import SourceSystems
from ..wake import OpenSuspension
from ..wiki_store import WikiStore
from .fleet import build_coordinator, build_specialist
from .rehydrate import ContextUnavailable, rebuild_context, verify_context_sufficient
from .runtime import agent_run

logger = logging.getLogger(__name__)

APP_NAME = "blackbox-fleet"


async def _run_agent(
    agent: Any,
    case_id: str,
    prompt: str,
    recorder: Recorder,
    systems: Optional[SourceSystems],
    wiki_store: WikiStore,
    session_suffix: str,
    judge_model: Optional[Any] = None,
) -> Dict[str, Any]:
    """Drive one ADK turn and report what came of it."""
    session_service = InMemorySessionService()
    session_id = f"{case_id}:{session_suffix}"
    await session_service.create_session(
        app_name=APP_NAME, user_id="fleet", session_id=session_id
    )
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    final_text = ""
    acting_agents = []
    with agent_run(
        recorder=recorder, systems=systems, wiki_store=wiki_store, judge_model=judge_model
    ) as run:
        async for event in runner.run_async(
            user_id="fleet", session_id=session_id, new_message=message
        ):
            author = getattr(event, "author", None)
            if author and author not in acting_agents:
                acting_agents.append(author)
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(p.text or "" for p in event.content.parts).strip()

        return {
            "case_id": case_id,
            "agents_that_acted": acting_agents,
            "suspended_on": run.suspended_on,
            "outputs": dict(run.outputs),
            "final_text": final_text,
        }


async def advance_case(
    case_id: str,
    store: EventStore,
    wiki_store: WikiStore,
    systems: Optional[SourceSystems] = None,
    model: Optional[Any] = None,
    trigger: str = "fleet_tick",
) -> Dict[str, Any]:
    """Let the fleet decide what this case needs next, and do it.

    The coordinator reads the case file and routes. Nothing here says which agent
    should run, which is the point: a switch statement making that call would
    turn the fleet into a pipeline.
    """
    context = rebuild_context(
        case_id=case_id, store=store, wiki_store=wiki_store, require_page=False
    )

    recorder = Recorder(case_id=case_id, actor="case_coordinator", store=store)
    recorder.set_cause(context.state.last_event_id)

    prompt = (
        f"{context.to_briefing()}\n\n"
        f"This case has come up for attention ({trigger}). Decide what it needs "
        f"next and hand it to the right agent."
    )

    agent = build_coordinator(model=model)
    return await _run_agent(
        agent=agent,
        case_id=case_id,
        prompt=prompt,
        recorder=recorder,
        systems=systems,
        wiki_store=wiki_store,
        session_suffix="advance",
        judge_model=model,
    )


async def resume_case(
    suspension: OpenSuspension,
    trigger: Dict[str, Any],
    store: EventStore,
    wiki_store: WikiStore,
    systems: Optional[SourceSystems] = None,
    model: Optional[Any] = None,
) -> Dict[str, Any]:
    """Wake a suspended case and let the agent that stopped carry on.

    The order matters. Context is rebuilt before the RESUME event is written, so
    that a case which cannot be rebuilt is left suspended rather than resumed
    onto nothing. A RESUME closes the suspension permanently, and the Diary is
    append-only, so writing one first and discovering the problem after would
    strand the case with no way back.
    """
    agent_name = suspension.condition.resume_agent
    recorder = Recorder(case_id=suspension.case_id, actor=agent_name, store=store)

    try:
        context = rebuild_context(
            case_id=suspension.case_id,
            store=store,
            wiki_store=wiki_store,
            suspension=suspension,
        )
        verify_context_sufficient(context)
    except ContextUnavailable as exc:
        # The suspension stays open. The case is visibly stuck, which is the
        # outcome we want over an agent improvising from a blank sheet.
        recorder.set_cause(suspension.suspend_event_id)
        recorder.escalate(
            reason=(
                f"Wake condition was met but the case could not be rebuilt, so it "
                f"was not resumed: {exc}"
            ),
            escalation_type="human",
            context={
                "case_id": suspension.case_id,
                "suspend_event_id": suspension.suspend_event_id,
            },
            urgency="high",
        )
        logger.error("Cannot resume %s: %s", suspension.case_id, exc)
        return {
            "case_id": suspension.case_id,
            "resumed": False,
            "reason": str(exc),
            "suspended_on": suspension.suspend_event_id,
        }

    resume_event = recorder.resume(
        suspend_event_id=suspension.suspend_event_id,
        reason=f"Wake condition met: {suspension.condition.description}",
        wake_trigger=trigger,
        state_restored=True,
    )

    prompt = (
        f"{context.to_briefing()}\n\n"
        f"## What changed while you were asleep\n"
        f"{trigger.get('summary', 'The condition you were waiting on is now met.')}\n"
    )

    agent = build_specialist(agent_name, model=model)
    result = await _run_agent(
        agent=agent,
        case_id=suspension.case_id,
        prompt=prompt,
        recorder=recorder,
        systems=systems,
        wiki_store=wiki_store,
        session_suffix=f"resume:{suspension.suspend_event_id}",
        judge_model=model,
    )
    result["resumed"] = True
    result["resume_event_id"] = resume_event
    result["resumed_agent"] = agent_name
    return result
