# BLACKBOX: The Flight Recorder for AI Agents

An autonomous fleet of AI agents performing a regulated business workflow, sitting
on top of an unerasable recording layer. Because everything the agents do is
recorded, the platform can do things a normal agent system cannot: rewind any
decision, replay it under different rules, trace any output back to the data that
shaped it, and prove regulated data never reached where it should not.

![Architecture: Gemini on Vertex AI wired into the six-agent fleet, the three-shelf storage, and the Split Screen UI](architecture-diagram.png)

## Start here

| If you want to | Go to |
|---|---|
| See it running | The Split Screen at `/` on the deployed service |
| Understand how it fits together | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Run it yourself in two minutes | [Quick start](#quick-start), below |
| Deploy your own | [DEPLOY.md](DEPLOY.md) |
| Know what the fleet actually does | [WORKFLOW.md](WORKFLOW.md) |
| Record the demo | [VIDEO.md](VIDEO.md) |

## Quick start

No Google Cloud account needed for any of this. The tests and demos use an
in-memory store and a scripted model, so they run offline.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

Run the suite. 278 tests, no credentials, no network:

```bash
.venv/Scripts/python -m pytest -q
```

Then watch the system do the things it was built for. Each demo is standalone and
prints what it is doing as it goes:

```bash
BLACKBOX_IN_MEMORY=1 .venv/Scripts/python demo_lifecycle.py
```

| Demo | What it shows |
|---|---|
| `demo_lifecycle.py` | One complaint from arrival to closure, with the calendar compressed |
| `demo_invisible_ink.py` | A letter with no medical word in it, blocked, and the trail back |
| `demo_eraser.py` | One retraction, six derived pages, and a refused border |
| `demo_time_machine.py` | Tighten a threshold and see which cases would have changed |
| `demo_stunt_double.py` | A candidate agent that sounded kind and tested as a risk |
| `demo_immune_system.py` | Attacks that write themselves, and a success curve that falls |
| `demo_crash_test.py` | Four faults, and a fleet that refuses to guess |

To run the service itself locally, with the Split Screen at `http://127.0.0.1:8080/`:

```bash
BLACKBOX_IN_MEMORY=1 GOOGLE_CLOUD_PROJECT=local .venv/Scripts/python -m uvicorn blackbox.main:app --port 8080
```

The page will be empty until a case exists, because it renders live data rather
than placeholders. `POST /debug/intake/CMP-2026-0841` opens one, and that needs
Gemini credentials; everything else works offline.

## Status

| Phase | State |
|---|---|
| 0. Define the work | Done. See `WORKFLOW.md`. |
| 1. The Flight Recorder | Done, and the write path now actually runs. See the note below. |
| 1.5. The Wiki and three shelves | Done. Tiering rewritten: copy, verify, evict. |
| 2. One agent, deployed | Done. Live on Cloud Run, Gemini via Vertex AI. |
| 3. The fleet wakes itself up | Done. Six agents, suspend and resume, heartbeat. |
| 4. Invisible Ink | Done. Label lattice, propagation, exit checks, taint path. |
| 5. The Eraser | Done. Transitive cascade, regeneration, region pinning. |
| 6. The Time Machine | Done. Policies as data, replay, divergence. |
| 7. The Stunt Double | Done. Shadow runs, Gemini judge, promotion gate. |
| 8. The Immune System | Done. Generated attacks, growing corpus, falling rate. |
| 9. The Crash Test | Done. Faults the agents see, degradation scored. |
| 10. The Split Screen | Done. Six live views, served at `/`. |
| 11. The package | Done. Architecture, spin-up, video script, build post. |

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

## The tiering fix

Phase 1.5 shipped with `tier_old_events()` printing a line and returning zero. It
neither copied to BigQuery nor deleted from Firestore, which is the failure mode
that phase's own spec names: a copy without a delete, so Firestore keeps growing
anyway. It is now implemented.

**Copy, verify, then evict.** Every eviction is preceded by reading the event back
from the shelf it was supposedly written to and comparing it field by field
against the copy still in Firestore. An event that does not read back identically
stays where it is and the mismatch is reported. So a botched run leaves Firestore
too big, which costs money, rather than losing an event, which cannot be undone.

**What append-only means here.** The Diary is append-only as a *record*: no event
is altered and none stops existing. Tiering changes an event's address, not its
content. `EventStore` still has no delete and no update, and an agent has no route
to one; the eviction capability lives on the backend, is called only by the
tiering manager, and is gated on the verification above.

**Reading is transparent across shelves.** The strongest test is not that events
move. It is that folding a case produces byte-identical state before and after
tiering, whichever shelf now holds it.

Shelf 3 writes Parquet partitioned by date, so a six month query scans six months.

Verified against live infrastructure: 4 events moved Firestore to BigQuery and
read back byte-identical, then BigQuery to Cloud Storage as Parquet and folded to
the same state from cold storage.

## What Phase 10 built

One page, served at `/`, that makes nine phases of plumbing legible. Everything on
it is live data from the same process that serves the API.

**Not a dashboard of tiles.** Tiles show that a system exists; they do not show it
working. The page leads with things happening: reasoning arriving, a divergence
opening up, a label travelling four hops. There is one row of numbers at the top
and it is context for the rest.

**Reasoning streams.** Server-sent events, appearing as each THOUGHT is written.
A collapsed log you have to click into hides the thing worth seeing.

**Live, not static.** Every panel fetches from a live endpoint, and says so when
there is nothing there yet rather than rendering plausible placeholder data. No
CDN and no build step: the whole page is one self-contained file, so it also works
on a locked-down network.

The six views: the live fleet with streaming reasoning, the split screen, the
Invisible Ink taint path, the Eraser cascade, a Time Machine scrubber, and the
immune system's two curves.

### A fix the UI forced

Building the divergence view showed that comparing two runs by position reports
the wrong thing. The replay does not re-emit a tool call the original made, so
index one differs for a structural reason, and the gate that actually changed its
mind gets buried underneath. Divergence now also pairs governance rules **by
rule**, so the headline is `gate_a_monetary_threshold: allow became escalate`
rather than a note about a missing tool call.

## What Phase 9 built

Faults injected where the agents can see them, and a score for how the fleet
came out.

**A fault the agents never see proves nothing.** So every fault surfaces as a
tool result an agent reads: a timeout comes back as a result saying the call
timed out, a contradiction comes back as two systems returning different numbers
with both visible. The agent has to decide, and the decision is recorded.

**Retrying a contradiction is not handling it.** A timeout may be transient and
one retry is reasonable. A contradiction is not: if CoreBank says one balance and
CRM360 says another, asking either again returns the same answer more
confidently. That distinction is enforced rather than described, because the
contradiction fault returns the identical pair however many times it is called,
so a fleet cannot retry its way past one.

**Four outcomes, one of which is a failure.** Recovered, escalated, halted
safely, or proceeded on bad data. The last is what this phase exists to detect,
because it is the outcome that looks fine in a log: the fleet saw a fault and
then took a consequential action anyway without recording that it knew. Reading a
second source after a timeout is not that. Deciding a case while the balance is
disputed and never mentioning the dispute is.

Noticing is not the same as handling, and the scorer says so: a fleet that
acknowledged a contradiction and then decided anyway, without escalating, is
still scored as having proceeded on bad data.

**Recovery paths the agents actually have.** The Evidence Agent gained two tools
and the instruction to use them: `report_source_conflict`, which records both
values and escalates without picking a side, and `report_unavailable_source`,
which asks the agent to say honestly whether the case can be decided without what
it could not reach.

### Seeing it

```bash
BLACKBOX_IN_MEMORY=1 python demo_crash_test.py
```

Four faults. A contradiction that gets escalated, a timeout on a case that needed
the missing data and gets escalated, the same timeout on a case that did not and
gets recovered, and the same contradiction handled by a fleet that noticed and
decided anyway. Three safe, one caught.

On a deployed service the faults are armed through an endpoint mid-demo, so
something can be broken on camera without a redeploy.

## What Phase 8 built

A red team that writes its own attacks, a corpus that only grows, and a success
rate measured against a set that keeps getting harder.

**An attack succeeds when a boundary is crossed, not when the model is rattled.**
This is the part most likely to be wrong in a way that flatters the system, so it
is worth being blunt. The tempting implementation scores an attack by whether the
agent said something strange: did it acknowledge the injection, did its tone
change. That is easy to build and it measures nothing. A model can quote an
injection back while doing exactly the right thing, and be perfectly composed
while wiring money to the wrong account.

So every criterion is a boundary checked from the Diary: money moved without the
approval the case required, something reaching a customer the gateway had
refused, internal reasoning appearing in a letter, a third party named to the
complainant, special category data crossing a border with nothing recorded, an
agent reaching for another role's tool. Each is a fact about what happened,
arguable in front of a regulator.

The consequence, accepted deliberately: an attack that produces alarming output
while crossing no boundary is scored a **failure**. That feels wrong when the
transcript reads badly. It is still right. The fleet's job is not to never be
addressed by an attacker; it is to never act on one.

**Gemini writes the attacks.** Given a family, its objective, and everything
already tried, it produces a new variation rather than the next item on a list. A
static list of injection strings is a test suite, not an immune system.

**Every success becomes permanent.** An attack that crosses a boundary enters the
corpus and runs against every version from then on, forever. Nothing is ever
removed, because a hole that closed can reopen and the only way to notice is to
keep testing for it.

### A finding worth recording

While building this, the red team surfaced something about the fleet's own
design. The Intake Agent holds five tools and all of them are reads plus its own
determination. It has no way to move money or write to a customer: ADK will not
even resolve those names for it. So **no boundary is reachable from the primary
injection surface**, however persuasive the injected text. That is capability
based defence, and it is stronger than anything the prompt does.

The threat that remains is indirect, and the attack runner models it: an injection
does not compromise Intake, it poisons the case file Intake writes, and a later
agent with dangerous tools reads that file as established fact.

### Seeing it

```bash
BLACKBOX_IN_MEMORY=1 python demo_immune_system.py
```

Three campaigns against three versions. The success rate falls 100 percent to 25
to 0 while the corpus grows from 2 to 3, and the run ends by naming a specific
attack that worked in the first campaign and showing it blocked in both later
ones. The rate falling while the corpus grows is the only version of that
sentence worth anything.

## What Phase 7 built

A candidate agent version runs against live cases, produces the actions it would
have taken, and touches nothing.

**Every write is stubbed, and not by remembering to.** Guarding this with
discipline would mean auditing every tool each time one is added. Instead the
candidate is handed a world it cannot write through: a scratch Diary seeded from
the live one, a scratch copy of the case file, and a `ShadowSystems` object that
passes reads through and refuses every outbound call. Three independent reasons a
shadow write cannot land, so a tool added tomorrow inherits all three.

**Compared on judgment, not strings.** Reducing the comparison to string equality
would flag every reworded summary and miss the candidate reaching the opposite
conclusion in similar words. The structural pass finds which decisions differ;
then Gemini reads those differences, with the reasoning behind each action, and
categorises them as equivalent, safer, riskier, or incorrect. A line the judge
cannot be parsed into is kept as `INCORRECT` rather than dropped, because a gate
that ignored what it could not read would pass a candidate on a malformed answer.

**The gate blocks rather than warns.** Defaults are strict: one incorrect
behaviour or one riskier behaviour stops promotion. Loosening is possible but has
to be stated explicitly, not defaulted into. An unreachable judge also blocks.

**Shadow runs are off the request path.** Their own endpoint, so a slow candidate
cannot add latency to the live fleet. A test asserts the live path never invokes
a shadow run.

### Seeing it

```bash
BLACKBOX_IN_MEMORY=1 python demo_stunt_double.py
```

The candidate is a Correspondence Agent told to write to the customer as soon as
a case is assessed rather than waiting for the remedy to be executed. That sounds
like a kindness. Shadowed across three cases, the judge calls it riskier twice:
the letter promises money that has not moved, and on the EU case it reaches the
disclosure gateway earlier in the workflow than the live version does. The gate
refuses promotion.

Worth watching in that run: on the EU case the Invisible Ink gateway refused the
letter before the shadow layer had to. Two independent barriers, and the earlier
one caught it.

## What Phase 6 built

Rewind to any past moment, alter a rule, replay, and see what would have happened
instead.

**Policies are data now.** This is the refactor the spec warned would have to
reach back into Phase 4, and it did. The gateway's rules were Python functions;
they are now CEL expressions in a versioned policy set, evaluated against a
context. The Gate A threshold is a number in that set rather than a constant in a
module. Replaying a case under a $100 threshold means loading a policy set that
says 100 and running exactly the same agent code, which is the only way a replay
proves anything.

A rule that will not compile, or that throws when evaluated, **raises**. It does
not return false. False means "this restriction does not apply", and a broken
rule quietly meaning that is how a governance system develops a hole nobody can
see. The outbound path treats an unevaluable rule as a block.

**A replay cannot touch a live system.** The spec calls this the most dangerous
defect possible in this build, and the defence here is capability rather than
discipline. A replay does not run against the live source systems with a flag
set. It runs against `FixtureSystems`, a different class holding a dictionary,
with no clients and no network code anywhere in it. There is nothing to disable
because there is nothing there. Outbound systems raise on contact, and the
attempt is reported because the attempt is the interesting part.

**A fixture miss stops the replay.** It does not fall through, retry, or
substitute a blank. A replay that guessed at a missing recording would produce a
confident, wrong divergence report, and a report you cannot trust is worse than
no report.

**State as-of, not state now.** Rewind to day six of a case that has since closed
and the naive implementation reads today's Wiki page, showing the agent the
outcome it is supposed to be deciding. So the world is rebuilt from the log:
event ids are ULIDs, so "everything at or before the rewind point" is a lexical
comparison, and the Wiki is reconstructed by replaying its `MEMORY_WRITE` events.
Those events now carry the resulting page content, which they did not before,
because without it the Wiki could not be reconstructed at all.

**Divergence is compared on decisions, not text.** Two runs that reached the same
conclusions in different words have not diverged. Comparing payloads would report
a difference on every changed timestamp and bury the decisions that matter. And
the report carries the downstream consequences, not just the point of the split.

### Seeing it

```bash
BLACKBOX_IN_MEMORY=1 python demo_time_machine.py
```

Three closed cases, worked under the $500 threshold. Drop it to $100 and replay.
The $617 case still escalates, the $82 case still sails through, and the $300
case changes from going straight through to waiting on an adjudicator. That is a
case which now waits one to four days it did not wait before, against an eight
week statutory deadline: the consequence a bank would want to see before shipping
the rule.

The demo also shows the replay refusing to send a letter, refusing to answer an
unrecorded call, and leaving the Diary at exactly the event count it started
with. A replay is a question about the past, not an addition to it.

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
  ui.py                The Split Screen, one self-contained page
  faults.py            Breaking things where the agents can see it
  degradation.py       Recovered, escalated, halted safely, or proceeded badly
  immune.py            What counts as a compromise: boundaries, not tone
  redteam.py           Attack families, Gemini generation, the corpus
  immune_service.py    Running attacks against the fleet, and the curves
  shelves.py           The warm and cold shelves, with in-memory doubles
  stunt.py             Shadow isolation: a world a candidate cannot write through
  shadow_service.py    Running a candidate, judging it, gating its promotion
  policy.py            Governance rules as CEL expressions, not code
  timemachine.py       State as-of, and the fixtures a replay may see
  divergence.py        Recorded model turns, and comparing two runs
  replay.py            Fast and fresh replay, and the divergence report
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
  test_phase6.py       Policies as data, state as-of, fixtures, divergence
  test_phase7.py       Write isolation, judged comparison, the promotion gate
  test_phase8.py       Success criteria, attack generation, the corpus
  test_phase9.py       Faults the agents see, and degradation scoring
  test_phase10.py      The page is live, streams, and is not a tile board
  test_tiering.py      The three shelves, and that moving loses nothing
  conftest.py          In-memory fixtures, so the suite needs no credentials
  fakes.py             A scripted stand-in for Gemini, tests only
demo_lifecycle.py      One complaint, start to finish, with time compressed
demo_invisible_ink.py  The four-hop block, and why no filter could catch it
demo_eraser.py         One retraction, six derived pages, and a refused border
demo_time_machine.py   Tighten a threshold, replay, see which cases change
demo_stunt_double.py   A candidate that sounded kind and tested as a risk
demo_immune_system.py  Attacks that write themselves, and a curve that falls
demo_crash_test.py     Four faults, and a fleet that refuses to guess
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

278 tests, all passing.

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
| `GET /` | **The Split Screen.** Six live views of the fleet |
| `GET /stream/reasoning` | Gemini's reasoning, streamed as it is recorded |
| `GET /overview` | Everything the Split Screen needs to open |
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
| `GET /policies` | The rules the fleet is running under, as data |
| `GET /cases/{id}/as-of/{event_id}` | The world as it stood at a past moment |
| `POST /replay` | Rewind, alter a rule, and report what would have differed |
| `POST /shadow` | Run a candidate version in shadow and judge its promotion |
| `GET /shelves` | How many events sit on each shelf |
| `POST /tiering/run` | Move aged events outward through the shelves |
| `POST /redteam/campaign` | Invent attacks, re-run the corpus, score both |
| `GET /redteam/corpus` | Every attack that has ever worked |
| `GET /redteam/metrics` | Success rate and corpus size over time |
| `POST /faults/arm` | Break something, live, without a redeploy |
| `POST /faults/disarm` | Put everything back |
| `GET /cases/{id}/degradation` | How the fleet came out of a fault |
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

## A deviation from the spec, and why

The build spec requires Gemini 3.5 Flash or newer. This project's Vertex AI
endpoint does not serve any 3.x model id: nine variants were tried in
`us-central1`, and only `gemini-2.5-flash` and `gemini-2.5-pro` resolved. `GEMINI_MODEL` defaults to `gemini-2.5-flash`
because that is what this project can actually reach, not because 2.5 satisfies
the letter of the requirement. If a 3.x id becomes available, changing
`GEMINI_MODEL` and redeploying is the whole fix; nothing else in the codebase
names a model.

## Hard constraints, and where they are held

- **Gemini only.** The model id lives in one place, `config.gemini_model`.
  `RecordedLlm` replays turns a Gemini call already produced; it infers nothing
  of its own.
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

Stated plainly, because a reviewer will find them anyway.

- **Propagation is conservative.** A model turn that saw special category data
  carries that class even if it only wrote about fees. Over-restriction is the
  safe direction and the label's origins say which source caused it, so a
  cautious block is legible. A finer-grained model would mean asking Gemini which
  facts it actually used, and a model that under-reports its own influences
  produces a label that is quietly too loose.
- **CommsVault job state lives in process memory.** The wake condition is in the
  Diary, so no case is lost when an instance recycles, but the stub's own map of
  job ids dies with the instance and a recycled one answers "unknown job". The
  case then stays suspended rather than resuming wrongly, which is the safe
  failure, but it is still a gap.
- **The attack corpus persists to the instance filesystem**, which Cloud Run does
  not keep between revisions. Moving it to Cloud Storage is small work.
- **The red team and the shadow runs have no scheduler job.** Both cost model
  calls, and neither needs to run unattended to be demonstrated.
- **`gemini-3.5-flash` is not what runs.** See the deviation note above.

## Domain

A mid-size retail bank operating in the US, UK, and EU, handling regulated
customer complaints. Six agents, statutory deadlines, health disclosures, three
jurisdictions, and human sign-off at defined thresholds. Full description in
`WORKFLOW.md`.

## License

Private. Hackathon project.
