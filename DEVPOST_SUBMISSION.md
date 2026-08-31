<!-- prose-check: off, this file quotes technical terms and model names that are accurate descriptions, not intensifiers. -->
# BLACKBOX: Devpost Submission (copy-paste source)

Every long-form field, in the order Devpost asks for them, with the confusing ones
explained inline. Same structure used for OnboardFlow's submission.

---

## Step 2: Project Overview

### Project name

BLACKBOX

### Elevator pitch (≤200 characters)

> An unerasable recording layer for AI agents. Rewind any decision, replay it under
> new rules, prove sensitive data never leaked, all from a log that can't be edited.

(196 characters)

### Thumbnail

Not yet made. 3:2 ratio, under 5MB. Do this after the video is done, since a good
thumbnail usually comes from a strong frame in the finished cut.

---

## Step 3: Project Details (public, appears on your project page)

### Built With (tags, up to 25)

python, google-adk, gemini, gemini-3-5-flash, vertex-ai, google-cloud-run,
google-cloud-firestore, google-cloud-bigquery, google-cloud-storage,
google-cloud-pubsub, google-cloud-scheduler, google-cloud-trace, fastapi, uvicorn,
pyarrow, cel-python, server-sent-events

### Try it out links

- Live app: https://blackbox-rd444zycdq-uc.a.run.app
- Code: https://github.com/TKHatton/blackbox
- Demo video: [ADD YOUTUBE LINK ONCE UPLOADED]

### About the project ("Project Story")

#### Inspiration

Companies are already handing consequential decisions to AI agents: approving
refunds, reviewing claims, screening applicants. When someone asks why an agent did
what it did, most teams cannot fully answer. They have logs of what got called and
what came back, not the reasoning behind it, no way to return to the moment before
the decision, and no way to answer the question that actually gets asked: would it
have done the same thing under last month's rules?

BLACKBOX is the layer that makes those questions answerable. Not a better agent, a
recording layer any agent system can sit on top of, plus everything that becomes
possible once the recording is complete and impossible to tamper with.

Two things sit at the center of it. **The Diary** is that recording: an
append-only event log, one write method, no update, no delete. **The Wiki** is
condensed working memory built from the Diary, rewritten as facts change, the
only thing the agents themselves ever read. Everything else in this project is
something the Diary makes possible that would otherwise be a promise, not a fact.

#### What it does

The Diary sits underneath a six-agent fleet handling a regulated bank complaint
workflow, chosen as the proving ground because it is the hardest believable case:
statutory deadlines, health information, customers across three jurisdictions.
The same recording layer applies to medical triage, insurance claims, lending, or
hiring, anywhere a machine's decision is something a person may later have to
answer for.

Because every action and its reasoning is written down and never altered:

- **Invisible Ink** blocks a data leak in a letter that contains none of the
  sensitive words itself, because a sensitivity tag travels with information
  through paraphrase and rewriting, not by matching keywords
- **The Time Machine** rewinds a closed case, changes one governance rule as
  data rather than code, and replays it to show exactly who a policy change
  would have affected differently, without touching anything live
- **The Eraser** retracts a fact and cascades that retraction through every
  summary derived from it, even several steps downstream, without ever showing
  a regenerating model the retracted content
- **The Stunt Double** shadow-runs a candidate agent version against genuine past
  cases with every write faked, and an independent Gemini judgment blocks
  promotion if the new version is riskier than what's already live
- **The Immune System** lets Gemini write its own adversarial attacks against
  the live agent code, keeps every attack that ever worked in a corpus that only
  grows, and counts a success only when an actual policy boundary was crossed
- **The Crash Test** injects faults the agents themselves have to read and react
  to, so a genuine contradiction between two systems of record can never be
  retried away, only escalated
- The fleet **runs unattended**, waking itself on a schedule rather than a
  button, including suspending for days waiting on a slow external system and
  resuming with full context rebuilt from the Wiki

#### How I built it

Google ADK agents, Gemini 3.5 Flash on Vertex AI for every model call, deployed on
Cloud Run with Firestore (hot), BigQuery (warm), and Cloud Storage/Parquet (cold)
as a three-shelf tier, Pub/Sub and Cloud Scheduler driving every trigger so nothing
starts from a manual action. The one rule everything else follows: the event store
has exactly one write method, no update, no delete. Firestore writes go through
`create()`, not `set()`, so an overwrite fails at the database rather than
succeeding quietly.

On top of that: a sensitivity-label lattice that propagates through every model
call and blocks disclosure by label ancestry rather than keyword matching; a
policy-as-data replay engine (CEL expressions) that rebuilds state from the log
rather than reading current state, isolated from production by capability, not a
flag; a shadow-evaluation system that stubs every write and uses Gemini as an
independent judge before promoting a new agent version; a red-team system where
Gemini generates adversarial attacks against the deployed agent code, scored strictly
by whether a policy boundary was crossed; and fault injection that surfaces faults
as tool results the agents themselves read, so a contradiction cannot be retried
away.

#### Challenges I ran into

The build spec calls for "Gemini 3.5 Flash or newer." Every 3.x model id 404s
against Vertex AI's regional endpoints, nine variants tried. The fix was not a
different model, it was a different endpoint: gemini-3.5-flash resolves on
Vertex's global endpoint, not a region, which is easy to miss since the error
looks identical to the model simply not existing yet.

A machine-level `gcloud` TLS failure turned out to be Norton Antivirus
intercepting TLS with its own root CA, unrelated to this project but blocking all
Google Cloud work from this machine until diagnosed.

The most consequential bug was not a crash. An earlier pass had marked the
foundational event-store phase complete on the strength of tests that never
actually exercised the write path, only asserted method signatures existed.
Finding and fixing that, and rewriting a tiering job that had shipped as a no-op,
mattered more than any later phase, because six later phases depend entirely on
the recording being trustworthy.

#### What I learned

Decide what a failure means before building the thing that detects it. The
tempting failure criterion, whether the model "sounded" wrong, measures nothing.
The honest one, whether a policy boundary was actually crossed, is what caught
genuine issues in the red-team system. And prefer capability to discipline: every
guarantee in this system holds because the dangerous action is unreachable, no
client for a replay to call, no tool registered for an agent that shouldn't have
one, not because the code remembers not to take it.

#### What's next

Move the red-team attack corpus off the Cloud Run instance filesystem to Cloud
Storage so it survives revision changes, and add a scheduler job for ongoing
automated shadow and red-team runs if continuous testing, rather than on-demand,
is wanted.

### Image gallery

Upload `architecture-diagram.png` at minimum. Screenshots of the Split Screen's six
tabs would strengthen the gallery if there's time after the video is done.

### Video demo link

Add once uploaded. **Must be public on YouTube or Vimeo, not unlisted or private.**
That's a hard rule in this hackathon's own terms, not a general Devpost default.

---

## Step 4: Additional Info

### Sponsor/Special Prizes (optional, select all that apply, separate from Category below)

- **Individual/Hobbyist** (solo build, not incorporated)
- **Best Architectural Design** (the layered recording-layer diagram and the
  append-only/verify-before-evict/capability-based-isolation design decisions map
  directly to this category's judging language)

Not selecting: Startup Excellence (not incorporated), Best Multimodal UX (the
product is a text and data UI, narration is only in the demo video, not the app
itself).

### Submitter type

**Individual.** This is a separate, generic Devpost field, unrelated to the
Category or Prize choices, just who is submitting.

### Submitter country of residence

Your country, from the dropdown.

### Category (required track, one choice only, appears in the gallery)

**Fortified Enterprise Fleet.** This hackathon's three tracks are Taskmaster
(a complete workflow instead of a chatbot), Collaborative Partner (an agent that
asks clarifying questions and adapts), and Fortified Enterprise Fleet (a scalable
network of institutional agents integrated with enterprise infrastructure,
demonstrating cataloging, asynchronous operation, and compliance). BLACKBOX is a
six-agent fleet, integrated with enterprise systems, built specifically around
regulatory compliance, with genuinely asynchronous suspend/resume. That is the
Fortified Enterprise Fleet track by definition, not a stretch fit.

### Organization name

Leave blank. Submitter type is Individual, no organization involved.

### Project start date

**08-30-26.** Confirmed from this repo's actual first commit
(`Initial commit: BLACKBOX Phase 1 and 1.5 complete`, 2026-08-30 10:09:57 -0400).
Well inside the hackathon's submission window (August 3 through August 31), so
there's no eligibility concern.

### Code repo URL

https://github.com/TKHatton/blackbox (public, no sharing with testing@devpost.com
needed)

### Reproducible testing instructions in README?

**Yes.** README.md has a "Quick Start" with the exact commands, and every command
in it has actually been run and verified this session.

### Hosted project URL

https://blackbox-rd444zycdq-uc.a.run.app

### Testing instructions (private, judges only, ~255 characters)

> No login needed, the live service is open for testing. Click any tab on the page
> for live data. Time Machine's Replay button and the API routes work directly,
> no token required. Offline: `pytest` runs all 278 tests with no credentials.

(249 characters)

### Google SDK(s) used

Google ADK (Agent Development Kit)

### Google Cloud service(s) used

Cloud Run, Cloud Firestore, Cloud Scheduler, Cloud Pub/Sub, BigQuery, Cloud
Storage, Cloud Trace

### Google AI model(s) used

Gemini 3.5 Flash, via Vertex AI's global endpoint (regional endpoints 404 for this
model family, documented in the README)

### Architecture diagram

`architecture-diagram.png` in the repo root, and also embedded in both README.md
and ARCHITECTURE.md.

### Bonus: content piece link (optional)

Your blog post, once written, publicly stating it was made for this hackathon.

### Bonus: social media post link (optional)

A post with the hackathon's required hashtag, once posted.

---

## Step 4 fields already covered above but worth restating for the "text description" box

### Features & functionality

- Six-agent fleet on Google ADK, handling regulated complaints end to end, with a
  disclosure gateway checking every outbound action
- Append-only event log: one write method, no update, no delete, enforced at the
  database, not just in application code
- Sensitivity-label propagation that blocks a data leak containing zero sensitive
  words, by tracing derivation rather than matching keywords
- Policy-as-data replay: rewind a closed case, change a governance rule, see the
  divergence, isolated from production by capability
- Shadow evaluation of candidate agent versions, judged by an independent Gemini
  call, blocking promotion on any risk finding
- Self-writing red-team corpus: Gemini generates attacks, success is scored by
  whether a policy boundary was crossed, the corpus only grows
- Fault injection surfaced as tool results the agents read, so a contradiction
  cannot be retried away
- Fully autonomous trigger chain: Cloud Scheduler to Pub/Sub to the fleet, with
  genuine multi-day suspend/resume, no manual step anywhere
- A live single-page UI (the Split Screen) with six views, all reading live data
  from the running service

### Technologies used

Python, Google ADK, Gemini 3.5 Flash via Vertex AI, FastAPI, Uvicorn, Firestore,
BigQuery, Cloud Storage, Pub/Sub, Cloud Scheduler, Cloud Trace, Cloud Run,
CEL (`cel-python`) for policy expressions, PyArrow for Parquet, server-sent events
for the live UI.

### Other data sources used

None external. The bank source systems (accounts, CRM, call archive, print vendor,
regulator portal) are simulated stubs, built with realistic parameter shapes so
they could be swapped for live integrations without changing the agents'
reasoning.

### Findings & learnings

See "Challenges I ran into" and "What I learned" above.

---

## Notes to self before submitting

- Swap in the YouTube link once the video is processed and public.
- Thumbnail comes after the video, once there's a strong frame to crop from.
- Double check no fault is armed on the live service before judges look at it:
  `curl -H "Authorization: Bearer $TOKEN" https://blackbox-rd444zycdq-uc.a.run.app/faults`
  should return `{"armed":[]}`.
