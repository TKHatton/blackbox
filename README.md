# BLACKBOX: The Flight Recorder for AI Agents

An autonomous fleet of AI agents performing a regulated business workflow, sitting
on top of an unerasable recording layer. Because everything the agents do is
recorded, the platform can do things a normal agent system cannot: rewind any
decision, replay it under different rules, trace any output back to the data that
shaped it, and prove regulated data never reached where it should not.

## Status

| Phase | State |
|---|---|
| 0. Define the work | Done. See `WORKFLOW.md`. |
| 1. The Flight Recorder | Done, and the write path now actually runs. See the note below. |
| 1.5. The Wiki and three shelves | Wiki done. Tiering is still a stub. |
| 2. One agent, deployed | Built and verified. Cloud Run deploy blocked on billing, see below. |
| 3. The fleet wakes itself up | Done. Six agents, suspend and resume, heartbeat. |
| 4. Invisible Ink | Next. |

### A correction to the earlier Phase 1 claim

Phase 1 and 1.5 were previously marked complete on the strength of 39 passing
tests. Those tests were structural. They inspected method signatures and asserted
that no update or delete method existed, and never called `append_event` once. The
write path had never run, and it could not have: it read `trace_id` and `span_id`
off an OpenTelemetry `Tracer` object, which has neither, so every write raised
`AttributeError`. The Wiki's update recorder imported a function that did not
exist. The tracer built a new provider per call and exported to the console rather
than to Cloud Trace.

Phase 2 fixed all of that, because none of it could be built on otherwise. The
Phase 2 suite exercises the write path end to end against a scripted model.

## What Phase 3 built

The fleet does work while nobody is watching.

**Six agents.** Intake, Evidence, Assessment, Remediation, Correspondence, and
the Compliance Officer. Each has one job and only the tools for that job. The
Remediation Agent is the only one that can move money, because it is the only one
given a tool that does, and a test asserts the other five cannot.

**Waiting without staying resident.** An agent that cannot continue writes a
`SUSPEND` event carrying a wake condition and stops. No process, thread, or
coroutine remains. Four kinds of wait exist and they resume by different routes:
a CommsVault batch job is asked, a human approval arrives on its own, a statutory
clock passes, and a 30 day appeal window can be cut short by a customer replying.

**The wake condition is an event, never a variable.** This is the part that
matters. If a pending case lived in process memory, a Cloud Run instance
recycling would lose it silently. Because the condition is in the Diary, an
instance that has been alive for four seconds finds exactly the same outstanding
work as one that has been up for a week. There is a test for precisely that: it
suspends a case, throws the store object away, builds a new one, and finds the
work.

**Resuming.** Context is rebuilt from the Wiki page plus the fold, never from raw
events. The briefing states which conclusions are already settled, so a resumed
agent extends the case instead of contradicting the version of itself that ran
three days earlier. A case whose Wiki page is missing or empty is not resumed at
all: it escalates and stays suspended, because an agent resuming onto a blank
sheet would improvise.

**The heartbeat is not a polling loop.** Cloud Scheduler gives suspended agents
an opportunity to evaluate conditions they themselves defined. The heartbeat has
no opinion about any case and cannot start work no agent asked for. A test
asserts that a beat with nothing suspended and no deadline near does nothing at
all. Every wake decision is recorded as a `POLICY_CHECK` with its reasoning, so
"why did this case wake on Thursday and not Wednesday" is answerable.

**Routing is judgment, not a switch statement.** The coordinator has one tool,
which reads the case file, and five sub-agents. It decides who should act next
and ADK carries the transfer. Its reasoning is recorded before the handoff. There
is no ordered list of steps anywhere, and a test fails if one appears.

**Gates are enforced in code, not only in the prompt.** Gate A blocks a remedy
above $500 without sign-off. Gate B stops every customer-facing statement while a
case is flagged as possibly systemic. Both checks live in the tools, so an agent
that talked itself past its instructions still cannot move money or write to a
customer.

### Seeing it work

```bash
BLACKBOX_IN_MEMORY=1 python demo_lifecycle.py
```

Runs one complaint through the whole workflow in a few seconds: intake, a wait on
CommsVault, a heartbeat that wakes it days later on a fresh instance, assessment,
an approval gate, the approval arriving, remediation, the final letter, the
appeal window, and closure. The model is scripted and the calendar is compressed,
and the script says so on screen as it happens. The waits themselves are not
simulated: the agents genuinely suspend, and the suspensions genuinely live in
the Diary.

It ends with 47 events in one causal tree, three suspensions, three resumptions,
and no internal reasoning in the letter that reached the customer.

## What Phase 2 built

One agent, deployed, with nothing in its path that needs a human to press a button.

**The Intake Agent** reads an arriving complaint, decides what it is, decides
which jurisdiction governs it, judges whether the customer shows vulnerability
indicators, and opens the case. It reasons through Gemini on Vertex AI and runs
through Google ADK.

**How work starts.** Cloud Scheduler wakes a poller on a timer. The poller checks
the inbound channels and publishes anything new to Pub/Sub. A message landing on
that topic is what causes the agent to run. The poller never calls the agent
directly, which is what lets Phase 3 add five more agents on their own topics
without rewriting the ingress.

**What gets recorded.** Every model turn becomes a `THOUGHT` event carrying
Gemini's own words, not a summary of them. Every tool call becomes a `TOOL_CALL`,
and its answer becomes a `TOOL_RESULT` recorded as that call's child. This happens
in ADK callbacks, so there is no code path by which an agent can think or act
without an event being written.

**The causal tree.** A case has exactly one root event, the complaint arriving.
Everything else names a parent that exists. `Recorder.assert_causally_complete()`
checks this, the tests assert it, and `/cases/{id}/trace` reports it. Null
`caused_by` on most events is the Phase 1 failure mode that would make the Time
Machine impossible later, so it is checked rather than hoped for.

### The three source systems

Stubs, with the personality that matters to the workflow:

- **CoreBank** answers in under a second. Its transaction records name third
  parties the bank has no right to disclose to the complainant.
- **CRM360** answers immediately, and is the origin of vulnerability flags, which
  are special category data.
- **CommsVault** does not answer. It returns a job id and a ready time two to
  three days out. This is what makes step 5 of the workflow a genuine
  asynchronous wait, and it is what Phase 3 builds suspend and resume on.

Two outbound stubs exist for later phases: **PrintPost**, a US-based letter
vendor, and **RegPortal**, the regulator endpoint.

## Layout

```
blackbox/
  config.py            Settings, from the environment
  schema.py            The event schema. 10 types, frozen, ULID ids
  backends.py          Append-only storage. Firestore create(), never set()
  event_store.py       The one write method
  recorder.py          Agent-facing recorder. Keeps caused_by populated
  fold.py              State computed from events, never stored
  wiki.py              Wiki page schema, with derived_from
  wiki_store.py        Wiki storage. Rewrites in place, records each rewrite
  tiering.py           Three shelves. Still a stub, see below
  wake.py              Wake conditions, and finding open suspensions
  heartbeat.py         The beat that lets suspended agents evaluate their own waits
  approvals.py         Approvals and customer replies arriving by Pub/Sub
  ingest.py            The poller and the Pub/Sub decode
  main.py              The Cloud Run service
  opentelemetry_setup.py
  agents/
    intake_agent.py    The ADK agent and its instruction
    intake_tools.py    The tools Gemini can call
    intake_service.py  Opens the case, runs ADK, writes the Wiki page
    fleet.py           The five remaining agents and the routing coordinator
    fleet_tools.py     Their tools, including the two that suspend
    fleet_service.py   Advancing a case, and resuming a suspended one
    rehydrate.py       Rebuilding context from the Wiki plus the fold
    callbacks.py       ADK hooks that write to the Flight Recorder
    runtime.py         Per-run context, so tools can reach the recorder
  stubs/
    systems.py         CoreBank, CRM360, CommsVault, PrintPost, RegPortal
    data.py            Synthetic customers, accounts, and complaints
tests/
  test_schema.py       Phase 1 schema
  test_destructive.py  Phase 1 append-only guarantee
  test_phase1_5.py     Wiki and tiering
  test_phase2.py       The agent, recorded end to end
  test_phase3.py       Suspend, resume, wake decisions, routing, gates
  conftest.py          In-memory fixtures, so the suite needs no credentials
  fakes.py             A scripted stand-in for Gemini, tests only
demo_lifecycle.py      One complaint, start to finish, with time compressed
```

## Running it locally

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

The tests need no Google Cloud credentials and make no network calls. They run
against the in-memory backend with a scripted model.

```bash
.venv/Scripts/python -m pytest -q
```

86 tests, all passing.

To run the service locally against the in-memory store:

```bash
BLACKBOX_IN_MEMORY=1 GOOGLE_CLOUD_PROJECT=local .venv/Scripts/python -m uvicorn blackbox.main:app --reload
```

Note that `/debug/intake/{ref}` will then try to reach Vertex AI, which needs
credentials. Everything else works offline.

## Deploying

See `DEPLOY.md`. Short version: fill in `.env`, then `bash deploy.sh`.

## Inspecting a running system

| Endpoint | What it shows |
|---|---|
| `GET /healthz` | How this instance is configured |
| `GET /cases` | Every seeded complaint, and whether a case is open |
| `GET /cases/{id}` | State, computed by folding the log |
| `GET /cases/{id}/trace` | The causal tree, and whether it is intact |
| `GET /cases/{id}/reasoning` | Just the THOUGHT events, in order |
| `GET /suspensions` | Every wait the fleet is holding, and what would end it |
| `GET /wiki/{page_id}` | What agents read during normal operation |
| `GET /stubs/...` | The source systems, for inspection |

Four endpoints cause work, and all four are called by machines:

| Endpoint | Called by | Effect |
|---|---|---|
| `POST /ingest/poll` | Cloud Scheduler, every 10 min | Publishes newly arrived complaints |
| `POST /heartbeat` | Cloud Scheduler, every 5 min | Lets suspended agents evaluate their waits |
| `POST /pubsub/approval` | Pub/Sub | An approval wakes the case waiting on it |
| `POST /pubsub/customer-reply` | Pub/Sub | A reply cuts an appeal window short |

None of them is meant for a person.

## Hard constraints, and where they are held

- **Gemini only.** The model id lives in one place, `config.gemini_model`.
  Nothing else in the codebase names a model. `tests/fakes.py` scripts a stand-in
  for tests and is never imported by shipped code.
- **Google ADK.** The agent is an `LlmAgent`, run by ADK's `Runner`. A test
  asserts this, because a hand-rolled inference loop would pass every other test
  while failing the requirement.
- **Google Cloud only.** Cloud Run, Firestore, Pub/Sub, Cloud Scheduler, Vertex
  AI, Cloud Trace.
- **No manual trigger.** Work begins when a message lands on a topic.
  `/debug/intake` exists to check a fresh deployment and is not on the autonomous
  path.
- **Reasoning is a recorded artifact.** THOUGHT events carry what the model said.

## Known gaps

- **Tiering is not implemented.** `tiering.tier_old_events()` prints a line and
  returns zero. It neither copies to BigQuery nor deletes from Firestore, so the
  Filing Cabinet is empty and Firestore grows without limit. Phase 1.5's own
  failure mode list names this exact shape. It needs building before any claim
  about flat Firestore document counts is true.
- **CommsVault job records live in process memory.** The wake condition moved
  into the Diary in Phase 3, so no case is lost when an instance recycles. What
  is still in memory is the stub's own map of job ids to ready times, so a
  recycled instance answers "unknown job" and the case stays suspended rather
  than resuming wrongly. Moving the stub's state into Firestore is small work
  and is not done.
- **Labels are empty.** Every event carries a `labels` field and nothing fills it
  yet. The source systems already mark their fields with sensitivity classes, so
  Phase 4 has somewhere to start from.

## Domain

A mid-size retail bank operating in the US, UK, and EU, handling regulated
customer complaints. Six agents, statutory deadlines, health disclosures, three
jurisdictions, and human sign-off at defined thresholds. Full description in
`WORKFLOW.md`.

## License

Private. Hackathon project.
