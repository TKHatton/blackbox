"""Per-run context shared between ADK callbacks and tool functions.

ADK calls tool functions with the arguments the model chose, and nothing else.
There is no place in that signature to pass a Recorder. A context variable holds
the current run instead, so a tool can reach the Flight Recorder without the
model having to know it exists.

A context variable rather than a module global because Cloud Run serves requests
concurrently, and two cases being worked at once must not write into each other's
logs.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

from ..labels import Label
from ..recorder import Recorder
from ..stubs.systems import SourceSystems, get_source_systems
from ..wiki_store import WikiStore


@dataclass
class AgentRun:
    """Everything one agent invocation needs, and the trail it leaves behind."""

    recorder: Recorder
    systems: SourceSystems
    wiki_store: Optional[WikiStore] = None
    # Maps an ADK function_call_id to the TOOL_CALL event it produced, so the
    # matching TOOL_RESULT can be recorded as its child rather than its sibling.
    tool_call_events: Dict[str, str] = field(default_factory=dict)
    # Filled in by the determination tool. The HTTP layer reads it to answer.
    determination: Optional[Dict[str, Any]] = None
    # Set when an agent decides to stop and wait. The runner reads this after the
    # turn ends, so a suspension is a decision the agent records, not a control
    # flow exception thrown through ADK.
    suspended_on: Optional[str] = None
    # What the agent produced this turn, for the caller to act on.
    outputs: Dict[str, Any] = field(default_factory=dict)
    # Invisible Ink. The join of every label this agent has seen this run.
    #
    # It only ever grows, and that is correct rather than lazy: a language model
    # conditions on its whole context, so anything the agent has read could have
    # shaped anything it writes. Tracking which fact influenced which sentence
    # would mean trusting the model's own account of its influences, and a model
    # that under-reports produces a label that is quietly too loose.
    taint: Label = field(default_factory=Label.public)
    #: Model the disclosure judge uses. None means the configured Gemini model.
    #: Tests set this so a gateway decision needs no network call.
    judge_model: Optional[Any] = None

    def absorb(self, label: Label) -> Label:
        """Join a new label into what this run carries. Returns the new total."""
        self.taint = self.taint.join(label)
        return self.taint

    def require_wiki(self) -> WikiStore:
        """The Wiki store, or a clear failure.

        A tool that needs to read or rewrite a page cannot fall back to the
        Diary, so the absence of a Wiki store is an error rather than a reason
        to reach for raw events.
        """
        if self.wiki_store is None:
            raise RuntimeError(
                "This agent run has no Wiki store, so the case page cannot be read "
                "or rewritten."
            )
        return self.wiki_store


_CURRENT_RUN: ContextVar[Optional[AgentRun]] = ContextVar("blackbox_current_run", default=None)


@contextmanager
def agent_run(
    recorder: Recorder,
    systems: Optional[SourceSystems] = None,
    wiki_store: Optional[WikiStore] = None,
    judge_model: Optional[Any] = None,
) -> Iterator[AgentRun]:
    """Make a run current for the duration of the block."""
    run = AgentRun(
        recorder=recorder,
        systems=systems or get_source_systems(),
        wiki_store=wiki_store,
        judge_model=judge_model,
    )
    token = _CURRENT_RUN.set(run)
    try:
        yield run
    finally:
        _CURRENT_RUN.reset(token)


def current_run() -> AgentRun:
    """The run in progress on this task.

    Raises RuntimeError rather than returning None. A tool that ran outside a
    recorded run would do real work and leave no trace, which is the one outcome
    this system exists to prevent.
    """
    run = _CURRENT_RUN.get()
    if run is None:
        raise RuntimeError(
            "No BLACKBOX agent run is active. Tools must be called inside agent_run(), "
            "otherwise their work would go unrecorded."
        )
    return run
