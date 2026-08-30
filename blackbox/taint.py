"""The taint path: from a blocked action back to the sentence that caused it.

A block that says "special category data" is a refusal. A block that says "special
category data, which entered at 09:12 when the customer wrote this sentence, was
extracted into structured facts here, correlated with an archived call here,
summarised into an assessment here, and paraphrased into this letter" is an
explanation. The second one is what makes the control credible, and it is only
possible because ``caused_by`` was populated on every event from Phase 1.

The path is built by walking the causal chain from the blocked POLICY_CHECK back
to the root of the case, then reading the label recorded on each event along the
way. Watching the label grow down that chain is the demonstration: you can see
the exact hop where each restriction attached, and see that it never came off.
"""

from typing import Any, Dict, List, Optional

from .event_store import EventStore
from .labels import Label, Sensitivity
from .schema import Event, EventType


def _hop_description(event: Event) -> str:
    """Say what happened at one hop, in the terms a reader cares about."""
    payload = event.payload
    if event.event_type == EventType.THOUGHT:
        return f"{event.actor} reasoned, then decided to {payload.get('decision', 'act')}"
    if event.event_type == EventType.TOOL_CALL:
        return f"{event.actor} called {payload.get('tool_name')}"
    if event.event_type == EventType.TOOL_RESULT:
        name = payload.get("tool_name")
        return f"{name} answered" + ("" if payload.get("success", True) else " with an error")
    if event.event_type == EventType.MEMORY_WRITE:
        return f"{event.actor} rewrote {payload.get('memory_key')}"
    if event.event_type == EventType.MEMORY_READ:
        return f"{event.actor} read {payload.get('memory_key')}"
    if event.event_type == EventType.POLICY_CHECK:
        return (
            f"gateway {payload.get('decision')}ed the disclosure "
            f"({payload.get('policy_id')})"
        )
    if event.event_type == EventType.MESSAGE_SENT:
        return f"{event.actor} sent to {payload.get('recipient')}"
    if event.event_type == EventType.SUSPEND:
        return f"{event.actor} suspended: {payload.get('reason', '')[:80]}"
    if event.event_type == EventType.RESUME:
        return f"{event.actor} resumed"
    if event.event_type == EventType.ESCALATE:
        return f"{event.actor} escalated"
    return event.event_type.value


def _quote_source(event: Event) -> Optional[str]:
    """The original text at this hop, when there is one.

    This is what lets the trail end on the customer's actual sentence rather than
    on an event id.
    """
    payload = event.payload
    if event.event_type == EventType.TOOL_RESULT:
        result = payload.get("result")
        if isinstance(result, dict) and result.get("narrative"):
            return str(result["narrative"])
    if event.event_type == EventType.MESSAGE_SENT:
        return str(payload.get("content", ""))[:600]
    return None


def taint_path(store: EventStore, event_id: str) -> Dict[str, Any]:
    """Trace a labelled action back to everything that shaped it.

    Args:
        store: The Diary.
        event_id: Usually a blocked POLICY_CHECK, but any event works.

    Returns:
        The chain from the root of the case to this event, one entry per hop,
        each carrying the label as it stood at that point, plus a summary of
        where each restricting class first attached.
    """
    chain = store.get_causal_chain(event_id)
    if not chain:
        return {"event_id": event_id, "found": False, "hops": []}

    # The causal chain alone is not the whole story. A label's classes usually
    # attach at an event that is a sibling of the chain rather than an ancestor:
    # the CRM360 lookup that returned a vulnerability flag is caused by the same
    # parent as everything else the agent did, not by the event before it. Those
    # events are named in the label's origins, so they are pulled in and merged
    # into creation order. Reporting only the ancestry would show the block
    # without showing where the restriction came from, which is the one question
    # a taint path exists to answer.
    case_id = chain[-1].case_id
    known = {e.event_id for e in chain}
    for event in list(chain):
        for origin in Label.from_dict(event.labels).origins:
            if origin.event_id and origin.event_id not in known:
                sourced = store.get_event(origin.event_id)
                if sourced is not None and sourced.case_id == case_id:
                    chain.append(sourced)
                    known.add(sourced.event_id)

    # ULIDs sort in creation order, so this puts the merged events where they
    # actually happened.
    chain.sort(key=lambda e: e.event_id)

    hops: List[Dict[str, Any]] = []
    # Where each sensitivity class was first seen on the chain.
    first_seen: Dict[str, Dict[str, Any]] = {}
    running = Label.public()

    for index, event in enumerate(chain):
        label = Label.from_dict(event.labels)
        before = running
        running = running.join(label)

        newly_added = sorted(
            c.value for c in running.classes if not before.has(c)
        )
        for sensitivity in newly_added:
            first_seen.setdefault(
                sensitivity,
                {
                    "hop": index,
                    "event_id": event.event_id,
                    "actor": event.actor,
                    "what_happened": _hop_description(event),
                    "timestamp": event.timestamp.isoformat(),
                },
            )

        hops.append(
            {
                "hop": index,
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "actor": event.actor,
                "timestamp": event.timestamp.isoformat(),
                "what_happened": _hop_description(event),
                "label_at_this_hop": label.to_dict() if event.labels else None,
                "accumulated_classes": sorted(c.value for c in running.classes),
                "accumulated_jurisdictions": sorted(running.jurisdictions),
                "newly_restricted_by": newly_added,
                "source_text": _quote_source(event),
            }
        )

    final = chain[-1]
    return {
        "event_id": event_id,
        "case_id": final.case_id,
        "found": True,
        "hop_count": len(hops),
        "final_classes": sorted(c.value for c in running.classes),
        "final_jurisdictions": sorted(running.jurisdictions),
        "restrictions_attached_at": first_seen,
        "origins": Label.from_dict(final.labels).to_dict().get("origins", []),
        "hops": hops,
    }


def blocked_disclosures(store: EventStore, case_id: str) -> List[Dict[str, Any]]:
    """Every disclosure the gateway refused on a case.

    The entry point for "what did this system stop, and why".
    """
    blocked = []
    for event in store.list_events_by_type(case_id, EventType.POLICY_CHECK):
        payload = event.payload
        if payload.get("check_type") != "data_disclosure":
            continue
        if payload.get("decision") != "block":
            continue
        blocked.append(
            {
                "event_id": event.event_id,
                "timestamp": event.timestamp.isoformat(),
                "actor": event.actor,
                "rule": payload.get("policy_id"),
                "judged_by": payload.get("input_data", {}).get("judged_by"),
                "destination": payload.get("input_data", {}).get("destination_system"),
                "destination_region": payload.get("input_data", {}).get("destination_region"),
                "reasoning": payload.get("reasoning"),
            }
        )
    return blocked


def summarise_path(path: Dict[str, Any]) -> str:
    """Render a taint path as lines a person can read out loud."""
    if not path.get("found"):
        return f"No event found for {path.get('event_id')}"

    lines = [
        f"Taint path for {path['event_id']} on case {path['case_id']}",
        f"{path['hop_count']} hops, ending with "
        f"[{', '.join(path['final_classes'])}] "
        f"jurisdiction {', '.join(path['final_jurisdictions']) or 'none'}",
        "",
    ]
    for hop in path["hops"]:
        marker = "  * " if hop["newly_restricted_by"] else "    "
        lines.append(f"{marker}{hop['hop']}. {hop['what_happened']}")
        if hop["newly_restricted_by"]:
            lines.append(
                f"        attaches: {', '.join(hop['newly_restricted_by'])}"
            )
        if hop["source_text"]:
            text = hop["source_text"].replace("\n", " ")
            lines.append(f'        text: "{text[:150]}..."')
    return "\n".join(lines)
