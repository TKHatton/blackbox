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
| 4. Invisible Ink | Done. Label lattice, propagation, exit checks, taint path. |
| 5. The Eraser | Done. Transitive cascade, regeneration, region pinning. |
| 6. The Time Machine | Next. |

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

## What Phase 5 built

Retraction that cascades correctly through everything derived from a fact.

The failure this prevents: you delete the source record, report success, and six
summaries elsewhere still carry the customer's name, because a model wrote them
months ago and nobody knows what went into them. BLACKBOX knows, because every
Wiki page records what it was built from.

**The cascade is transitive.** `derived_from` now holds both event ids and other
page ids, which makes it a graph rather than a list. The walk reverses those
edges and travels forward, breadth first, until it stops finding new pages. In
the demo a retraction against a customer reaches an operating-context page three
edges away that never mentioned them at all. A one-level cascade would look
correct on any example small enough to eyeball, so there is a test for depth and
a test that unrelated pages are left alone.

**Regeneration cannot reintroduce what was retracted.** The regenerator is never
shown the old page. Not shown it and asked to remove things: not shown it. It
gets the page's remaining valid sources, and sources that carried the retracted
fact are excluded even though they stay in the Diary forever. A test asserts the
old content never enters the prompt, and that `regenerate()` replaces content
rather than merging with it, because a merge is exactly how a retracted fact
survives an erasure that reports success.

**Then it is checked anyway.** Regenerated content is scanned for the retracted
values, and a page that still carries one is redacted rather than published. That
scan is a keyword check, and Phase 4 is emphatic that a keyword check is not a
control. It is not the control here either. The control is that the model never
saw the content; this is the verification that the control held.

**The Diary still records that it happened.** The append-only log cannot have
content removed from it, which is exactly why the `RETRACT` event records what
was withdrawn, from where, and by whose request, but never the withdrawn values
themselves. Every page the cascade reached gets its own `INVALIDATE` event naming
the depth and the edge it came by.

**Region pinning is enforced, not labelled.** The check lives in the Wiki store's
read path. A US worker asking for an EU-pinned page gets an exception and no
page, and the refusal is recorded as an event with its reasoning. Listing is
guarded too, because a control that guards single reads while letting a caller
list its way around them is not a control. The Eraser deliberately bypasses the
region filter: refusing to erase EU data because the machine running the cascade
sits in the US would be the control working backwards.

### Seeing it

```bash
BLACKBOX_IN_MEMORY=1 python demo_eraser.py
```

Eight Wiki pages, three carrying the customer's name. She invokes erasure. Six
pages are reached across three levels of derivation, five are rewritten from
their surviving sources, one has nothing left and becomes a statement that it no
longer holds anything. The two unrelated pages stay at version 1. Then a US
worker is refused the EU-pinned case, and refused again when it tries listing
instead.

## What Phase 4 built

Sensitive data carries a stamp that survives summarising, merging, and rephrasing.

**The stamp is on the derivation, not the words.** This is the single idea the
phase rests on. A model output carries the join of the labels of everything that
model turn could see. Not "everything relevant": everything, because a language
model conditions on its whole context. That is why the label does not wash off at
the first summarisation. It was never attached to the wording, so rewording
cannot remove it.

**Sensitivity is a set, not a number.** The obvious design is one value per label
ordered least to most restrictive, combined by taking the maximum. That design
under-restricts, silently. `INTERNAL_ONLY` means never to a customer;
`PII_HIGH` means never outside the bank; `THIRD_PARTY_PII` means not to *this*
recipient. Ask which is higher and the question has no answer, and forcing them
onto one axis loses whichever comes second. So a label carries a set of classes,
combination is union, and the set is reduced to a maximal antichain so
`{PII, SPECIAL_CATEGORY}` collapses to `{SPECIAL_CATEGORY}` without dropping the
PII restriction. Tests assert the join is commutative, associative, idempotent,
has an identity, and never loosens either input.

**Rules for the unambiguous, Gemini for the rest.** Three things are decided by
rule, because each has a correct answer that does not depend on wording: a
national identifier leaving the bank, special category data crossing to a third
country with no adequacy basis, and a third party's name going to the
complainant. `INTERNAL_ONLY` is deliberately not a rule: every final response is
derived from the assessment, so a rule on that derivation would block every
letter the bank ever sends. Whether a letter states the outcome, which the
customer is entitled to, or repeats the file note, which they are not, is a
judgment about the content. Gemini makes it, and its reasoning is recorded as the
basis for the decision. An unparseable answer blocks, because a gateway that
fails open would be worse than no gateway.

**A block is a gate, not a wall.** The cross-border rule asks for a documented
transfer basis. Recording one clears it, and the recording names who authorised
it. The data is no less sensitive; the decision now has a person attached.

### The moment worth watching

```bash
BLACKBOX_IN_MEMORY=1 python demo_invisible_ink.py
```

An EU-resident customer mentions a cancer diagnosis in a complaint about fees.
Four hops later a different agent, which has never seen the complaint, writes her
a kind letter containing no medical word at all. Twelve medical terms searched
for; none present. The gateway blocks it anyway, because the letter descends from
that sentence, and it prints the trail back to her exact words.

Then it shows the hop where the restriction attached and the hop where the block
happened. No keyword filter could have made that call.

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
  labels.py            The label lattice and the combination rule
  eraser.py            Retraction, the transitive cascade, regeneration
  regions.py           Region pinning, enforced in the Wiki read path
  propagation.py       How a label survives a model call
  gateway.py           Exit checks. Rules first, Gemini for the ambiguous
  taint.py             From a blocked action back to the source sentence
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
  test_phase4.py       The lattice, propagation, the gateway, the taint path
  test_phase5.py       Cascade transitivity, regeneration, region pinning
  conftest.py          In-memory fixtures, so the suite needs no credentials
  fakes.py             A scripted stand-in for Gemini, tests only
demo_lifecycle.py      One complaint, start to finish, with time compressed
demo_invisible_ink.py  The four-hop block, and why no filter could catch it
demo_eraser.py         One retraction, six derived pages, and a refused border
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

142 tests, all passing.

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
| `GET /cases/{id}/blocked` | Disclosures the gateway refused, and why |
| `GET /taint/{event_id}` | The chain from a blocked action back to its source |
| `GET /retractions` | Every retraction performed, after the content is gone |
| `GET /regions/check` | Whether this instance may read a given jurisdiction |
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
- **Propagation is conservative.** A turn that saw special category data carries
  that class even if it only wrote about fees. Over-restriction is the safe
  direction and the label's origins say which source caused it, so a cautious
  block is legible rather than mysterious. A finer-grained model would need to
  ask Gemini which facts it actually used, and a model that under-reports its own
  influences produces a label that is quietly too loose.

## Domain

A mid-size retail bank operating in the US, UK, and EU, handling regulated
customer complaints. Six agents, statutory deadlines, health disclosures, three
jurisdictions, and human sign-off at defined thresholds. Full description in
`WORKFLOW.md`.

## License

Private. Hackathon project.
