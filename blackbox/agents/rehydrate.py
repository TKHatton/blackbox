"""Rebuilding a resumed agent's context.

An agent that suspended on Monday and resumes on Thursday has no memory of
Monday. Its process is long gone. What it gets instead is built here, from two
sources and no others:

- **The Wiki page** for the case, which is the condensed current state of what is
  known. This is the primary source.
- **The fold** over the case's events, which gives status, outstanding waits, and
  the last event id to hang new work from.

It does not get the raw Diary. Reading raw events to reconstruct context is the
Phase 1.5 failure mode that makes the system feel fine in a demo and unusable in
month nine, because the log grows without bound while the Wiki page does not.

The other half of the problem is rehydration that loses detail, so that the
resumed agent repeats work or contradicts its earlier decision. Two things guard
against that: the briefing states explicitly what has already been decided and
must not be revisited, and ``verify_context_sufficient`` refuses to resume at all
when the Wiki page is missing, rather than letting an agent continue on a blank
sheet and improvise.
"""

from typing import Any, Dict, List, Optional

from ..event_store import EventStore
from ..fold import CaseState, fold_events
from ..wake import OpenSuspension
from ..wiki import WikiPage
from ..wiki_store import WikiStore


class ContextUnavailable(RuntimeError):
    """Raised when a case cannot be rebuilt well enough to resume safely.

    Resuming without context is worse than not resuming: the agent would act
    confidently on nothing. The suspension stays open and the failure is
    recorded, so the case is visibly stuck rather than quietly wrong.
    """


class ResumeContext:
    """Everything a resuming agent knows, and where each piece came from."""

    def __init__(
        self,
        case_id: str,
        page: Optional[WikiPage],
        state: CaseState,
        suspension: Optional[OpenSuspension] = None,
    ):
        self.case_id = case_id
        self.page = page
        self.state = state
        self.suspension = suspension

    @property
    def wiki_content(self) -> Dict[str, Any]:
        return dict(self.page.content) if self.page else {}

    def decisions_already_made(self) -> Dict[str, Any]:
        """The conclusions this case has already reached.

        Handed to the resuming agent as settled, so it extends the case rather
        than re-deciding it and contradicting the version of itself that ran
        three days ago.
        """
        content = self.wiki_content
        settled = {}
        for key in (
            "category",
            "severity",
            "jurisdiction",
            "jurisdiction_reasoning",
            "vulnerability_indicators",
            "outcome",
            "proposed_remedy",
            "remedy_amount",
            "systemic_flag",
        ):
            if key in content:
                settled[key] = content[key]
        return settled

    def to_briefing(self) -> str:
        """Render the context as the text an agent reads on waking.

        Written for a reader with no memory of this case at all, because that is
        exactly the situation.
        """
        content = self.wiki_content
        lines: List[str] = [
            f"You are resuming case {self.case_id}. You have no memory of working "
            f"on it. Everything you know about it is below.",
            "",
            "## Where the case stands",
            f"Status: {self.state.current_status}",
            f"Events recorded so far: {len(self.state.events)}",
            f"Last activity: {self.state.last_updated.isoformat()}",
            "",
        ]

        if self.suspension is not None:
            lines += [
                "## Why you stopped, and why you are awake",
                f"You suspended at {self.suspension.suspended_at.isoformat()} because: "
                f"{self.suspension.reason}",
                f"You were waiting for: {self.suspension.condition.description}",
                "That condition has now been met, which is why you are running again.",
                "",
            ]

        settled = self.decisions_already_made()
        if settled:
            lines += [
                "## Already decided. Do not revisit these.",
                "An earlier run of this case reached these conclusions. Treat them as "
                "settled facts and build on them. Contradicting them without new "
                "evidence would put two incompatible decisions in the record.",
                "",
            ]
            for key, value in settled.items():
                lines.append(f"- {key}: {value}")
            lines.append("")

        remaining = {k: v for k, v in content.items() if k not in settled}
        if remaining:
            lines += ["## What is known about the case", ""]
            for key, value in remaining.items():
                lines.append(f"- {key}: {value}")
            lines.append("")

        if self.state.pending_actions:
            lines += ["## Other waits still outstanding on this case", ""]
            for action in self.state.pending_actions:
                lines.append(f"- {action.get('reason')} (since {action.get('suspended_at')})")
            lines.append("")

        lines += [
            "## Now",
            "Carry on from here. Do not repeat work that the record above shows is "
            "already done.",
        ]
        return "\n".join(lines)


def rebuild_context(
    case_id: str,
    store: EventStore,
    wiki_store: WikiStore,
    suspension: Optional[OpenSuspension] = None,
    require_page: bool = True,
) -> ResumeContext:
    """Rebuild a case's context from the Wiki plus the fold.

    Args:
        case_id: The case to rebuild.
        store: Event store, read only for the fold.
        wiki_store: Where the case's page lives.
        suspension: The suspension being answered, if this is a resume.
        require_page: Refuse to build a context with no Wiki page. Left true on
            the resume path, because an agent resuming onto a blank sheet is the
            failure this guards against.

    Returns:
        The context, ready to be rendered into a briefing.

    Raises:
        ContextUnavailable: If the case has no events, or has no Wiki page while
            ``require_page`` is set.
    """
    events = store.list_events(case_id)
    if not events:
        raise ContextUnavailable(
            f"Case {case_id} has no events. There is nothing to resume into."
        )

    state = fold_events(events)
    page = wiki_store.get_page(f"case:{case_id}")

    if page is None and require_page:
        raise ContextUnavailable(
            f"Case {case_id} has no Wiki page. Resuming would mean working from a "
            f"blank sheet, so the suspension is being left open instead."
        )

    return ResumeContext(case_id=case_id, page=page, state=state, suspension=suspension)


def verify_context_sufficient(context: ResumeContext) -> None:
    """Check the rebuilt context is enough to act on.

    Catches the quiet version of the failure: a Wiki page that exists but has
    been reduced to nothing useful, which would let an agent resume and then
    invent its way forward.
    """
    if context.page is None:
        raise ContextUnavailable(f"Case {context.case_id} has no Wiki page")
    if not context.page.content:
        raise ContextUnavailable(f"Case {context.case_id} has an empty Wiki page")
    if not context.page.derived_from:
        raise ContextUnavailable(
            f"Case {context.case_id} has a Wiki page that cites no source events, so "
            f"nothing can vouch for it"
        )
