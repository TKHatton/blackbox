"""The Eraser: retraction that cascades through everything derived from a fact.

The compliance failure this exists to prevent is deleting the source record and
leaving the derived summaries intact. A customer invokes erasure, the identity
fields come out of the source system, and six Wiki pages elsewhere still carry
their name, because each of those pages was written by a model months ago and
nobody knows what went into it. BLACKBOX knows, because every page records what
it was derived from.

Four things this module gets deliberately right, each of them a named failure
mode if got wrong:

**The cascade is transitive.** A page derived from a page derived from a
retracted fact is reached. The walk continues until it stops finding new pages,
and a test builds a four-deep chain and checks the far end was reached. A cascade
that ran one level deep would look correct on any example small enough to eyeball.

**Regeneration cannot reintroduce the retracted content.** The regenerator is
never shown the old page. Not shown it and asked to remove things: not shown it.
It is given the page's remaining valid sources and asked to write the page from
those. Reusing the cached summary is how the retracted fact survives an erasure
that reports success, so the old content does not enter the process at all.

**Then it is checked anyway.** After regeneration, the new content is scanned for
the retracted values, and a page that still contains one is held invalid rather
than published. This is a keyword check, and Phase 4 is emphatic that a keyword
check is not a control. It is not the control here either. The control is that
the model never saw the content. This is the verification that the control
worked, which is a different job, and belt and braces is the right posture when
the cost of being wrong is a regulator.

**The Diary still records that a retraction happened.** The append-only log
cannot have content removed from it, which is why the RETRACT event records what
was withdrawn and from where, but never the withdrawn values themselves. History
of the retraction is preserved even though the content is gone.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ulid import ULID

from .event_store import EventStore
from .recorder import Recorder
from .schema import EventType
from .wiki import WikiPage
from .wiki_store import WikiStore

logger = logging.getLogger(__name__)

#: Where a retraction's own events are recorded. Not a case: a retraction can
#: span many cases, and filing it under one of them would hide it from the others.
RETRACTION_LOG = "retractions"


@dataclass
class Retraction:
    """A request to withdraw a fact from the system."""

    subject: str
    fields: List[str]
    reason: str
    requested_by: str
    #: The literal values being withdrawn. Held in memory for the duration of the
    #: cascade so regenerated pages can be checked against them, and never
    #: written to the Diary, which cannot forget.
    values: List[str] = field(default_factory=list)
    retraction_id: str = field(default_factory=lambda: f"RET-{ULID()}")
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_event_payload(self, scope: Dict[str, Any]) -> Dict[str, Any]:
        """The RETRACT payload. Note what is absent: the values."""
        return {
            "subject": self.subject,
            "retracted_fields": list(self.fields),
            "reason": self.reason,
            "requested_by": self.requested_by,
            "scope": scope,
        }


@dataclass
class CascadeResult:
    """What a retraction reached and what happened to each page."""

    retraction_id: str
    subject: str
    retract_event_id: str
    directly_affected: List[str] = field(default_factory=list)
    invalidated: List[Dict[str, Any]] = field(default_factory=list)
    #: Pages rewritten by the model from their remaining valid sources.
    regenerated: List[str] = field(default_factory=list)
    #: Pages that could not be rebuilt and were replaced with a statement that
    #: they no longer hold anything. Not the old content with holes in it.
    redacted: List[Dict[str, Any]] = field(default_factory=list)
    max_depth: int = 0

    @property
    def pages_reached(self) -> int:
        return len(self.invalidated)

    @property
    def held_invalid(self) -> List[Dict[str, Any]]:
        """Kept for callers written against the earlier name."""
        return self.redacted


# ----------------------------------------------------------------------
# Finding what a retraction touches
# ----------------------------------------------------------------------


def _page_mentions(page: WikiPage, retraction: Retraction) -> Optional[str]:
    """Why this page is directly affected, or None.

    Two ways a page can be directly affected: it is about the subject, or its
    content carries one of the retracted values.
    """
    if page.subject == retraction.subject:
        return f"the page is about {retraction.subject}"

    if retraction.subject in str(page.content):
        return f"the content references {retraction.subject}"

    blob = str(page.content)
    for value in retraction.values:
        if value and value in blob:
            # The value itself is not repeated in the reason, because the reason
            # goes into the Diary.
            return "the content carries a retracted value"

    return None


def build_dependency_graph(pages: List[WikiPage]) -> Dict[str, Set[str]]:
    """Map each page to the pages that depend on it.

    ``derived_from`` points backwards, from a page to its sources. The cascade
    travels forwards, so the edges are reversed once here rather than searched
    repeatedly during the walk.
    """
    dependents: Dict[str, Set[str]] = {p.page_id: set() for p in pages}
    for page in pages:
        for source_page_id in page.source_page_ids():
            dependents.setdefault(source_page_id, set()).add(page.page_id)
    return dependents


def find_affected(
    pages: List[WikiPage], retraction: Retraction
) -> Tuple[Dict[str, Tuple[int, str]], int]:
    """Walk the graph forward from the directly affected pages.

    Returns a map of page id to (depth, how it was reached), and the greatest
    depth reached. Depth 0 pages hold the retracted fact themselves; deeper pages
    are derived from a page that does.

    Breadth first, with a visited set, so a page reached by two paths is recorded
    at its shortest depth and a cycle in the graph cannot loop forever.
    """
    by_id = {p.page_id: p for p in pages}
    dependents = build_dependency_graph(pages)

    affected: Dict[str, Tuple[int, str]] = {}
    queue: List[Tuple[str, int, str]] = []

    for page in pages:
        why = _page_mentions(page, retraction)
        if why is not None:
            affected[page.page_id] = (0, why)
            queue.append((page.page_id, 0, why))

    max_depth = 0
    while queue:
        page_id, depth, _ = queue.pop(0)
        max_depth = max(max_depth, depth)
        for dependent_id in sorted(dependents.get(page_id, set())):
            if dependent_id in affected or dependent_id not in by_id:
                continue
            reason = f"derived from {page_id}"
            affected[dependent_id] = (depth + 1, reason)
            queue.append((dependent_id, depth + 1, reason))

    return affected, max_depth


# ----------------------------------------------------------------------
# Regeneration
# ----------------------------------------------------------------------


REGENERATOR_INSTRUCTION = """
You rewrite a summary page after some of the information behind it has been
withdrawn.

You will be given the page's subject and the sources that remain valid. You will
NOT be given the previous version of the page, and that is deliberate. Write the
page fresh from the sources you have.

Rules:

- Use only what is in the sources given to you. If you find yourself reaching for
  a detail that is not there, leave it out.
- Do not guess at what was removed, do not refer to something having been
  removed, and do not leave a placeholder where a fact used to be.
- If the remaining sources do not support a conclusion the page used to state,
  the page simply no longer states it.
- Where a person's identity has been withdrawn, refer to them by role only, for
  example "the complainant".

Answer with one JSON object and nothing else: keys are the fields of the page,
values are strings, numbers, or booleans. Keep it short.
""".strip()


def build_regeneration_prompt(
    page: WikiPage, valid_sources: List[Dict[str, Any]]
) -> str:
    """Render the regeneration request.

    The previous content of the page is not in here. That absence is the whole
    control.
    """
    lines = [
        f"Page: {page.page_id}",
        f"Subject type: {page.subject_type}",
        "",
        "Valid remaining sources:",
    ]
    if not valid_sources:
        lines.append("  (none)")
    for source in valid_sources:
        lines.append(f"  - {source}")
    lines += ["", "Write the page from these sources."]
    return "\n".join(lines)


def content_still_contains_retracted(
    content: Dict[str, Any], retraction: Retraction
) -> List[str]:
    """Which retracted values survive in regenerated content.

    The verification, not the control. See the module docstring.
    """
    blob = str(content)
    found = []
    for value in retraction.values:
        if value and value in blob:
            found.append(value)
    if retraction.subject and retraction.subject in blob:
        found.append(retraction.subject)
    return found


def _redact_placeholder(page: WikiPage, retraction: Retraction) -> Dict[str, Any]:
    """What a page becomes when it cannot be safely regenerated.

    Not the old content with holes in it. A statement that the page used to exist
    and no longer holds anything, which is the honest outcome when the remaining
    sources support nothing.
    """
    return {
        "status": "retracted",
        "subject_type": page.subject_type,
        "note": (
            "The information behind this page was withdrawn and the remaining "
            "sources do not support regenerating it. The retraction itself is "
            "recorded in the Flight Recorder."
        ),
        "retraction_id": retraction.retraction_id,
    }


async def _regenerate_with_gemini(
    page: WikiPage,
    valid_sources: List[Dict[str, Any]],
    model: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Ask Gemini to rewrite the page from what remains. None if it cannot."""
    import json

    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from .config import get_settings

    settings = get_settings()
    agent = LlmAgent(
        name="page_regenerator",
        model=model or settings.gemini_model,
        description="Rewrites a Wiki page from its remaining valid sources.",
        instruction=REGENERATOR_INSTRUCTION,
    )

    session_service = InMemorySessionService()
    session_id = f"regen:{page.page_id}"
    await session_service.create_session(
        app_name="blackbox-eraser", user_id="eraser", session_id=session_id
    )
    runner = Runner(app_name="blackbox-eraser", agent=agent, session_service=session_service)
    message = types.Content(
        role="user", parts=[types.Part(text=build_regeneration_prompt(page, valid_sources))]
    )

    answer = ""
    try:
        async for event in runner.run_async(
            user_id="eraser", session_id=session_id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts).strip()
    except Exception:
        logger.exception("Regeneration failed for %s", page.page_id)
        return None

    cleaned = answer.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()

    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError):
        logger.warning("Regenerator did not return JSON for %s", page.page_id)
        return None

    return parsed if isinstance(parsed, dict) else None


# ----------------------------------------------------------------------
# The cascade
# ----------------------------------------------------------------------


async def retract(
    retraction: Retraction,
    store: EventStore,
    wiki_store: WikiStore,
    model: Optional[Any] = None,
    regenerate: bool = True,
) -> CascadeResult:
    """Withdraw a fact and cascade the consequences through derived memory.

    Args:
        retraction: What is being withdrawn, and why.
        store: The Diary. The retraction is recorded here and stays recorded.
        wiki_store: Derived memory. This is what actually changes.
        model: Override the regenerator's model. Used by tests.
        regenerate: When false, pages are invalidated and left that way. Useful
            for seeing the blast radius before committing to a rebuild.

    Returns:
        What the cascade reached and what became of each page.
    """
    recorder = Recorder(case_id=RETRACTION_LOG, actor="eraser", store=store)

    # Read every page without the region check. A retraction must reach derived
    # pages wherever they are pinned: refusing to erase EU data because the
    # cascade happens to be running in the US would be the wrong way round.
    all_pages: List[WikiPage] = []
    for subject_type in ("case", "customer", "agent_context", "analysis"):
        all_pages.extend(
            wiki_store.list_pages_by_subject_type(subject_type, enforce_region=False)
        )

    affected, max_depth = find_affected(all_pages, retraction)
    by_id = {p.page_id: p for p in all_pages}

    retract_event = recorder.record(
        EventType.RETRACT,
        retraction.to_event_payload(
            {
                "pages_reached": sorted(affected),
                "max_depth": max_depth,
                "page_count": len(affected),
            }
        ),
    )

    result = CascadeResult(
        retraction_id=retraction.retraction_id,
        subject=retraction.subject,
        retract_event_id=retract_event,
        directly_affected=sorted(p for p, (d, _) in affected.items() if d == 0),
        max_depth=max_depth,
    )

    # Invalidate before regenerating any of them. A page rebuilt while one of its
    # own sources is still standing with retracted content would pick the content
    # straight back up.
    for page_id in sorted(affected, key=lambda p: affected[p][0]):
        page = by_id[page_id]
        wiki_store.update_page(page.invalidated(retraction.retraction_id))

    with recorder.under(retract_event):
        for page_id in sorted(affected, key=lambda p: (affected[p][0], p)):
            depth, reached_via = affected[page_id]
            page = by_id[page_id]

            regenerated_ok = False
            new_content: Optional[Dict[str, Any]] = None

            if regenerate:
                new_content = await _rebuild_page(
                    page=page,
                    retraction=retraction,
                    affected=affected,
                    store=store,
                    model=model,
                )
                regenerated_ok = new_content is not None

            if regenerated_ok:
                surviving = content_still_contains_retracted(new_content, retraction)
                if surviving:
                    # The control failed. Hold the page invalid rather than
                    # publishing content that still carries what was withdrawn.
                    logger.error(
                        "Regenerated %s still carried retracted content, holding invalid",
                        page_id,
                    )
                    result.redacted.append(
                        {"page_id": page_id, "why": "regenerated content still matched"}
                    )
                    regenerated_ok = False
                    new_content = None

            if not regenerated_ok and regenerate:
                new_content = _redact_placeholder(page, retraction)
                regenerated_ok = True
                if not any(h["page_id"] == page_id for h in result.redacted):
                    result.redacted.append(
                        {"page_id": page_id, "why": "no valid sources to rebuild from"}
                    )

            if regenerated_ok and new_content is not None:
                surviving_sources = [
                    ref
                    for ref in page.derived_from
                    if ref not in affected  # drop edges to invalidated pages
                ]
                wiki_store.update_page(
                    page.regenerate(
                        new_content=new_content, new_derived_from=surviving_sources
                    )
                )
                if not any(h["page_id"] == page_id for h in result.redacted):
                    result.regenerated.append(page_id)

            recorder.record(
                EventType.INVALIDATE,
                {
                    "page_id": page_id,
                    "caused_by_retraction": retraction.retraction_id,
                    "depth": depth,
                    "reached_via": reached_via,
                    "regenerated": regenerated_ok,
                    "reason": (
                        f"Invalidated by retraction {retraction.retraction_id} "
                        f"against {retraction.subject}, reached at depth {depth} "
                        f"because {reached_via}."
                    ),
                },
            )
            result.invalidated.append(
                {"page_id": page_id, "depth": depth, "reached_via": reached_via}
            )

    return result


async def _rebuild_page(
    page: WikiPage,
    retraction: Retraction,
    affected: Dict[str, Tuple[int, str]],
    store: EventStore,
    model: Optional[Any],
) -> Optional[Dict[str, Any]]:
    """Assemble a page's remaining valid sources and rewrite it from them.

    The previous content of the page is never gathered, never passed, and never
    merged. It is not available to this function at all beyond the page object's
    identity fields.
    """
    valid_sources: List[Dict[str, Any]] = []

    for event_id in page.source_event_ids():
        event = store.get_event(event_id)
        if event is None:
            continue
        # An event that carried the retracted fact is not a valid source for a
        # rebuild, even though it stays in the Diary forever.
        blob = str(event.payload)
        if retraction.subject in blob or any(v and v in blob for v in retraction.values):
            continue
        valid_sources.append(
            {
                "kind": event.event_type.value,
                "actor": event.actor,
                "summary": str(event.payload)[:400],
            }
        )

    for source_page_id in page.source_page_ids():
        if source_page_id in affected:
            # Its own sources were retracted too. Not usable.
            continue
        valid_sources.append({"kind": "wiki_page", "page_id": source_page_id})

    if not valid_sources:
        return None

    return await _regenerate_with_gemini(page, valid_sources, model=model)


def retraction_history(store: EventStore) -> List[Dict[str, Any]]:
    """Every retraction the system has performed.

    The content is gone from the Wiki. This is the proof it was ever there and
    that somebody withdrew it, which is what an auditor asks for.
    """
    history = []
    for event in store.list_events_by_type(RETRACTION_LOG, EventType.RETRACT):
        payload = event.payload
        history.append(
            {
                "event_id": event.event_id,
                "timestamp": event.timestamp.isoformat(),
                "subject": payload.get("subject"),
                "retracted_fields": payload.get("retracted_fields"),
                "reason": payload.get("reason"),
                "requested_by": payload.get("requested_by"),
                "pages_reached": payload.get("scope", {}).get("page_count"),
                "max_depth": payload.get("scope", {}).get("max_depth"),
            }
        )
    return history
