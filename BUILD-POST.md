# I built a flight recorder for AI agents, and the recording turned out to be the product

*A draft for publishing. Written to be read by someone who has never seen the
repo.*

---

Everyone shipping AI agents has the same quiet problem. The agent did something,
and you cannot fully explain why. You have logs of what it called and what came
back. You do not have the reasoning, you cannot rewind to the moment before the
decision, and you certainly cannot answer "would it have done that under last
month's rules".

I spent a week building the thing that fixes it. Not a better agent: a recording
layer underneath one, and then everything that becomes possible once the
recording exists.

## The setup

A mid-size bank handling regulated customer complaints. Six agents: intake,
evidence, assessment, remediation, correspondence, and a compliance officer.
Statutory deadlines, three jurisdictions, health disclosures, human sign-off at
defined thresholds.

I chose complaint handling because the governance constraints are the point. If
you removed the compliance layer and still had a working product, the domain
would be wrong. Here, remove it and you have an unlicensed bank.

Everything runs on Google Cloud. Every model call goes to Gemini on Vertex AI.
The agents are Google ADK.

## The one rule everything else follows

The recording is append-only. There is exactly one write method, no update, and
no delete. Firestore writes go through `create()` rather than `set()`, so an
attempt to overwrite an event fails at the database rather than succeeding
quietly.

That sounds like a small design choice. It is the whole build. Six of the eleven
phases are things you can only do if the record is complete and trustworthy.

Two things follow immediately, and they are constantly confused:

**The Diary** is the raw append-only log. Nothing is ever changed or removed.

**The Wiki** is condensed current state, rewritten as facts change.

Agents read the Wiki. Agents never read the Diary. A fleet that rebuilt its
context from raw events would feel fine in a demo and be unusable in month nine,
because the log grows forever and a summary page does not.

## What the recording buys you

### Nobody presses a button

A Cloud Scheduler job wakes a poller. The poller publishes to Pub/Sub. A message
landing on that topic is what makes an agent run. There is no code path where a
human starts the work.

The interesting part is what happens when an agent has to wait. The archive
system in this workflow answers in days, not seconds. So the agent writes a
`SUSPEND` event containing the condition that would wake it, and stops. No
process, no thread, nothing resident.

Days later a heartbeat reads that condition out of the log, decides it is met,
and the agent resumes with its context rebuilt. **The wake condition is an event,
never a variable.** If it lived in memory, a container recycling would lose every
pending case in the fleet, silently, and nobody would find out until a statutory
deadline passed.

While I was building the next phase, the deployed fleet sat there running that
loop unattended for ninety minutes: eighteen evaluations, each correctly deciding
not to wake, each recording why.

### A leak no keyword filter can catch

This is the piece I would show you if you only looked at one.

A customer in Ireland complains about bank fees. In passing she mentions a cancer
diagnosis that cut her income. Four hops later, a different agent that never saw
the complaint writes her a letter. The letter contains no medical word at all. I
checked twelve of them: cancer, diagnosis, treatment, illness. None present.

The gateway blocked it anyway.

Special category data, EU origin, going to a US-based print vendor, with no
transfer basis recorded. The system knew because a label travelled with the
content through two Gemini calls and a paraphrase.

The design decision that makes this work: **the label is attached to the
derivation, not to the words.** A model output carries the join of the labels of
everything that turn could see. Not "everything relevant", everything, because a
language model conditions on its whole context. Rewording cannot remove a label
that was never attached to the wording.

The one I nearly got wrong: sensitivity is a **set**, not a number. The obvious
design is a single value from least to most restrictive, combined by taking the
maximum. That silently under-restricts. "Never leaves the bank" and "never
reaches a customer" and "not for this recipient" are different kinds of
restriction. Ask which is higher and the question has no answer; force them onto
one axis and one of them quietly disappears.

### Rewind, change a rule, replay

Governance rules are CEL expressions in a versioned policy set, not code. So you
can rewind a finished case, change one number, and replay it under the same agent
code.

Drop the approval threshold from five hundred dollars to one hundred, and a case
that sailed through now waits one to four days for a human, against an eight week
statutory deadline. That is a policy consequence a bank would want to see before
shipping the rule, and there is currently no way to get it.

Two things a replay must never do, and both took careful design:

**It must not read current state.** Rewind to day six of a case that has since
closed, and a naive implementation reads today's summary and shows the agent the
answer it is supposed to be deciding. So the world is rebuilt from the log. This
one bit me: the Wiki turned out to be unreconstructable, because its write events
recorded version numbers and not content. Every replay would have silently fallen
back to current state.

**It must not touch anything live.** The defence is capability, not discipline. A
replay does not run against the live systems with a flag set. It runs against a
different class holding a dictionary, with no clients and no network code in it.
There is nothing to disable because there is nothing there. A missing recording
stops the replay rather than falling through to a live call.

### Erasure that actually erases

A customer invokes their right to erasure. You delete the source record, report
success, and six summaries elsewhere still carry their name because a model wrote
them months ago and nobody knows what went into them.

Every page here records what it was derived from, so the cascade is transitive. In
the demo it reaches an operating-context page three edges away that never
mentioned the customer at all.

The part worth stealing: **the regenerator is never shown the old page.** Not
shown it and asked to remove things. Not shown it. It gets the sources that
survive, and sources carrying the retracted fact are excluded. Then the result is
scanned for the retracted values anyway, and a page that still contains one is
held back rather than published. That scan is a keyword check and it is not the
control. The control is that the model never saw the content. The scan is the
verification that the control held.

Meanwhile the log still records that a retraction happened, which is exactly why
the retracted values are never written into it. An append-only log cannot forget.

### Attacks that write themselves

Gemini generates adversarial inputs against the fleet: injection in the complaint
text, poisoned call transcripts, pressure applied across a thirty day appeal
window. Anything that works is added to a corpus that runs against every version
from then on, forever.

The success criterion is the part most likely to be wrong in a way that flatters
you. The tempting version scores an attack by whether the agent said something
strange. That measures nothing. A model can quote an injection back while doing
exactly the right thing, and be perfectly composed while wiring money to the
wrong account.

So an attack succeeds only when a **policy boundary was crossed**, checked from
the log: money moved without approval, something reaching a customer the gateway
refused, a third party named to the complainant. The consequence I accepted
deliberately is that an attack producing alarming output while crossing no
boundary is scored a failure. That reads wrong when the transcript reads badly.
It is still right. The fleet's job is not to never be addressed by an attacker. It
is to never act on one.

Building this surfaced something about my own design I had not noticed: the
intake agent holds five tools and all of them are reads. It cannot move money or
write to a customer, because those tools are not registered for it. No boundary
is reachable from the primary injection surface at all, however persuasive the
text. That is capability-based defence and it is stronger than anything a prompt
does.

### Break it on camera

Faults are injected where the agents can see them, as tool results they read
rather than exceptions the infrastructure swallows. A fault caught by a retry in
an HTTP client only demonstrates that the HTTP client retries.

The distinction I care about: a timeout may be transient and one retry is
reasonable. A contradiction is not. If two systems of record disagree about a
balance, asking either again returns the same answer more confidently. So the
contradiction fault returns the identical pair however many times it is called,
and a fleet cannot retry its way past one. The correct move is to stop and
escalate, because acting on either figure means acting on data the bank already
knows is disputed.

Four outcomes: recovered, escalated, halted safely, or proceeded on bad data. Only
the last is a failure, and it is the one that looks fine in a log.

## What I would tell you if you are building something similar

**Decide what a failure is before you build the thing that detects it.** Twice
the honest criterion was less flattering than the obvious one, and both times the
obvious one would have produced a metric that moved without meaning anything.

**Prefer capability to discipline.** Every guarantee that holds in this system
holds because the dangerous thing is unreachable, not because the code remembers
not to do it. The replay cannot call production because it has no client. The
intake agent cannot move money because it has no tool. Those survive a change
made by someone who has not read the comments.

**Write the failure mode into the test name.** The tests here are named things
like "an unsettling reply that crosses nothing is a failure". A year from now
that says why the test exists, which is the only thing that stops someone
deleting it when it becomes inconvenient.

**Fix the thing you skipped.** I shipped a tiering job that printed a line and
returned zero, and marked the phase complete. It sat there for six phases. When I
went back, implementing it took an afternoon, and it turned up two deployment gaps
nobody would have found until the log outgrew Firestore.

## Where it is

The code is public and the service is live. Two hundred and seventy-eight tests,
which run with no credentials and no network, because the model is scripted and
the storage is in memory. The demos run the same way.

If you want the short version of why any of this matters: agents are going to
make consequential decisions in regulated settings, and the industry currently
cannot answer basic questions about decisions its own systems already made. Not
because the answers are hard. Because nobody kept the recording.
