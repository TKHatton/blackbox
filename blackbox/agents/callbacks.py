"""ADK callbacks that write the fleet's activity into the Flight Recorder.

This module is the reason the recorder is not a decorative log. Every model turn
and every tool invocation passes through ADK's callback hooks, so there is no
path by which an agent can think or act without an event being written.

What each hook records:

- ``after_model``  a THOUGHT carrying Gemini's own words. Both the visible text
  and any thinking parts the model emitted are kept. Logging only the final
  action here is the Phase 2 failure mode, so the rationale is stored verbatim
  rather than summarised.
- ``before_tool``  a TOOL_CALL with the arguments the model chose.
- ``after_tool``   a TOOL_RESULT recorded as a child of its TOOL_CALL, so the
  causal tree shows the call and its answer as parent and child rather than as
  two unrelated siblings.

Callback parameter names are keyword-matched by ADK, so they must not be renamed.
"""

import logging
from typing import Any, Dict, Optional

from ..labels import Label
from ..propagation import label_for_tool_result
from .runtime import current_run

logger = logging.getLogger(__name__)

# A tool result can be large. The Diary keeps the whole thing, but a runaway
# payload would bloat the hot path, so oversized results are recorded with a
# marker rather than silently truncated to look complete.
MAX_RECORDED_RESULT_CHARS = 20000


def _describe_response(llm_response: Any) -> Dict[str, Any]:
    """Pull the model's text, its thinking, and its intended calls out of a response."""
    said: list[str] = []
    thoughts: list[str] = []
    tool_names: list[str] = []

    content = getattr(llm_response, "content", None)
    for part in getattr(content, "parts", None) or []:
        text = getattr(part, "text", None)
        if text:
            # ADK marks reasoning parts with .thought when the model emits them.
            if getattr(part, "thought", False):
                thoughts.append(text)
            else:
                said.append(text)
        function_call = getattr(part, "function_call", None)
        if function_call is not None and getattr(function_call, "name", None):
            tool_names.append(function_call.name)

    return {
        "said": "\n".join(said).strip(),
        "thinking": "\n".join(thoughts).strip(),
        "tool_names": tool_names,
    }


def after_model(callback_context: Any, llm_response: Any) -> None:
    """Record what Gemini said and what it decided to do next.

    Returns None so ADK keeps the model's own response. This callback observes;
    it never rewrites what the model produced.
    """
    try:
        run = current_run()
    except RuntimeError:
        logger.warning("Model response outside a recorded run, not written to the Diary")
        return None

    described = _describe_response(llm_response)
    reasoning = described["thinking"] or described["said"]

    if not reasoning and not described["tool_names"]:
        # An empty turn carries nothing worth a THOUGHT event.
        return None

    if described["tool_names"]:
        decision = f"call {', '.join(described['tool_names'])}"
    else:
        decision = "respond without calling a tool"

    # Invisible Ink. What the model just said is derived from everything it could
    # see, so the THOUGHT carries the run's accumulated label. This is the hop
    # where the stamp survives summarisation: the wording changes completely and
    # the label does not, because it was never attached to the words.
    run.recorder.thought(
        labels=run.taint.to_dict(),
        reasoning=reasoning or "(model returned no text with this turn)",
        decision=decision,
        # ADK does not surface a calibrated confidence. Recording a made-up number
        # would be worse than recording that none was reported, so the schema's
        # required field carries a documented placeholder.
        confidence=0.5,
        context_summary=(
            f"Turn by {getattr(callback_context, 'agent_name', 'unknown agent')} "
            f"on case {run.recorder.case_id}"
        ),
    )
    return None


def before_tool(tool: Any, args: Dict[str, Any], tool_context: Any) -> None:
    """Record the tool call before it happens, and remember it as the cause."""
    try:
        run = current_run()
    except RuntimeError:
        logger.warning("Tool call outside a recorded run, not written to the Diary")
        return None

    tool_name = getattr(tool, "name", str(tool))
    event_id = run.recorder.tool_call(
        labels=run.taint.to_dict(),
        tool_name=tool_name,
        parameters=dict(args),
        intended_outcome=(getattr(tool, "description", "") or "").strip().split("\n")[0]
        or f"Invoke {tool_name}",
    )

    call_id = getattr(tool_context, "function_call_id", None) or tool_name
    run.tool_call_events[call_id] = event_id
    return None


def after_tool(
    tool: Any, args: Dict[str, Any], tool_context: Any, tool_response: Any
) -> None:
    """Record what the tool returned, as a child of the TOOL_CALL."""
    try:
        run = current_run()
    except RuntimeError:
        logger.warning("Tool result outside a recorded run, not written to the Diary")
        return None

    tool_name = getattr(tool, "name", str(tool))
    call_id = getattr(tool_context, "function_call_id", None) or tool_name
    cause = run.tool_call_events.pop(call_id, None)

    result: Any = tool_response
    error_message: Optional[str] = None
    success = True

    if isinstance(tool_response, dict) and "error" in tool_response:
        success = False
        error_message = str(tool_response["error"])

    serialized = repr(result)
    if len(serialized) > MAX_RECORDED_RESULT_CHARS:
        result = {
            "recorded": "elided",
            "reason": "tool result exceeded the recorded size limit",
            "size_characters": len(serialized),
            "preview": serialized[:2000],
        }

    # The label the source system's answer carries. This is where new sensitivity
    # enters the run: CRM360 returning a vulnerability flag, CoreBank returning a
    # transaction that names somebody else.
    result_label = label_for_tool_result(tool_name, tool_response, event_id=None)
    combined = run.absorb(result_label)

    result_event = run.recorder.tool_result(
        labels=combined.to_dict(),
        tool_name=tool_name,
        success=success,
        result=result,
        error_message=error_message,
        caused_by=cause,
    )

    # Re-point the provenance at the event that actually recorded it, so the
    # taint path can name the hop rather than saying "somewhere in this run".
    if result_label.origins:
        located = Label.make(
            result_label.classes,
            result_label.jurisdictions,
            [
                type(o)(o.system, o.field, result_event, o.note)
                for o in result_label.origins
            ],
            result_label.retention,
        )
        run.taint = run.taint.join(located)

    return None
