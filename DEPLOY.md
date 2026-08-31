# Deploying BLACKBOX

What you end up with: six ADK agents running on Cloud Run, reasoning through
Gemini on Vertex AI, writing every thought and tool call to an append-only
Firestore log. Complaints arrive on a Pub/Sub topic, agents suspend themselves
when they have to wait, and a Cloud Scheduler heartbeat lets them decide when to
wake. Nothing runs because a person pressed a button. Every outbound path passes
a disclosure gateway that can refuse a letter containing no sensitive word,
because of where its content is derived from.

## Before you start

You need the `gcloud` CLI, authenticated, with billing available. You do not need
Docker: the build runs in Cloud Build from source.

## 1. Create the project

Pick an id that is yours alone. The one below is an example.

```bash
gcloud projects create blackbox-flightrecorder --name "BLACKBOX"
```

Link billing to it in the console, or with `gcloud billing projects link`.

## 2. Create the Firestore database

BLACKBOX uses a **named** database, not `(default)`. This matters more than it
looks: a client that omits the database id talks to `(default)` instead, finds
nothing, and reports an empty log rather than an error.

```bash
gcloud firestore databases create \
  --database=blackbox-database \
  --location=nam5 \
  --type=firestore-native
```

Use an EU location instead if you want the Phase 5 region pinning work to have an
enforceable border to refuse to cross.

## 3. Fill in .env

```bash
cp .env.example .env
```

Set at minimum:

```
GOOGLE_CLOUD_PROJECT=blackbox-flightrecorder
GOOGLE_CLOUD_LOCATION=us-central1
FIRESTORE_DATABASE=blackbox-database
```

`.env` is gitignored. Keep it that way.

## 4. Firestore indexes

Four composite indexes are needed, because the event queries filter and sort at
the same time. They take a few minutes each to build, so start them all and let
them run. Create them once:

```bash
gcloud firestore indexes composite create \
  --database=blackbox-database \
  --collection-group=events \
  --field-config=field-path=case_id,order=ascending \
  --field-config=field-path=event_id,order=ascending
```

```bash
gcloud firestore indexes composite create \
  --database=blackbox-database \
  --collection-group=events \
  --field-config=field-path=case_id,order=ascending \
  --field-config=field-path=event_type,order=ascending \
  --field-config=field-path=event_id,order=ascending
```

```bash
gcloud firestore indexes composite create \
  --database=blackbox-database \
  --collection-group=events \
  --field-config=field-path=caused_by,order=ascending \
  --field-config=field-path=event_id,order=ascending
```

The fourth is what the Phase 3 heartbeat needs. It is the only cross-case query
in the system: to find suspended work, the heartbeat has to look at every
`SUSPEND` event in the fleet, not just one case's.

```bash
gcloud firestore indexes composite create \
  --database=blackbox-database \
  --collection-group=events \
  --field-config=field-path=event_type,order=ascending \
  --field-config=field-path=event_id,order=ascending
```

Firestore will tell you in the error message if you skip one, with a link that
creates it.

## 5. Deploy

```bash
bash deploy.sh
```

The script enables the APIs, creates two service accounts, deploys to Cloud Run,
creates three Pub/Sub topics with their push subscriptions, and registers two
Cloud Scheduler jobs: the inbound poller and the fleet heartbeat. It is safe to
run more than once.

## 6. Confirm it is working

The service is not public, so calls need an identity token.

```bash
TOKEN=$(gcloud auth print-identity-token)
```

Check how the instance is configured:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/healthz
```

Fire the poller rather than waiting for the timer:

```bash
gcloud scheduler jobs run blackbox-intake-poller --location us-central1
```

Within a minute or so, three cases exist:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/cases
```

Read the reasoning the agent recorded:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/cases/CASE-CMP-2026-0841/reasoning
```

And the causal tree:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/cases/CASE-CMP-2026-0841/trace
```

## 7. Watch the fleet wait, and then wake

See what the fleet is currently waiting on. This list is read out of the Diary,
so it is identical whichever instance answers:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/suspensions
```

Run a heartbeat now rather than waiting for the timer. Cases whose conditions are
met resume; the rest record why they did not:

```bash
gcloud scheduler jobs run blackbox-heartbeat --location us-central1
```

Grant an approval, which is what wakes a case suspended on a gate. Nothing polls
for this: the message arriving is the wake condition being met.

```bash
gcloud pubsub topics publish blackbox-approvals --message '{"case_id":"CASE-CMP-2026-0841","gate":"A","approved":true,"approver":"you"}'
```

See what the gateway has refused, and why:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/cases/CASE-CMP-2026-0841/blocked
```

Then trace one of those blocks back to the data that caused it. Pass the
`event_id` from the call above:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/taint/EVENT_ID
```

The response has a `rendered` field holding the trail as readable lines, one per
hop, marking where each restriction attached.

The demonstration worth watching is the one you cannot rush: suspend a case,
close the laptop, and come back the next day. The CommsVault waits are two to
three days, so a case that stopped on Monday resumes on Wednesday or Thursday,
on whatever instance happens to be up, with its context rebuilt from the Wiki.

In the console, Cloud Trace shows the same run as a tree of spans, one per
recorded event, with the agent's thoughts and tool calls nested under the
complaint that caused them.

## If something fails

**The model id is rejected.** `GEMINI_MODEL` defaults to `gemini-2.5-flash`,
confirmed working against this project's Vertex AI endpoint in `us-central1`.
`gemini-3.5-flash` and every 3.x variant tried returned 404 NOT_FOUND on this
project, so the default was set to what actually resolves rather than what the
build spec names. If a newer id becomes available in your project or region,
set `GEMINI_MODEL` to it and redeploy. Nothing else in the codebase names a
model.

**Permission denied on Firestore.** The runtime service account needs
`roles/datastore.user`, which `deploy.sh` binds. IAM changes can take a minute to
take effect.

**The push subscription reports 403.** The invoker service account needs
`roles/run.invoker` on the service. The script binds this after the deploy, so a
first run that failed partway through may have skipped it. Re-running the script
fixes it.

**A case looks stuck.** The Diary is append-only, so a case cannot be reopened or
rewritten. Read what happened at `/cases/{case_id}/trace`. If the agent finished
without opening the case, there is an `ESCALATE` event saying so.

**A case is suspended and never wakes.** Check `/suspensions` for what it is
waiting on. A case waiting on a gate needs an approval published to the approvals
topic; nothing will wake it otherwise, by design. If the case does not appear in
`/suspensions` at all but is not progressing either, look at
`unparseable_suspensions` in the heartbeat's response: that lists suspensions
whose wake condition could not be read, which is the one way a case can be
genuinely lost rather than merely waiting.

## Region pinning and where you deploy

`WORKER_REGION` says which region an instance runs in, and it is checked on
every Wiki read. Set it to match the Cloud Run region you deploy to. An instance
that claims to be in the EU while running in `us-central1` would pass the check
and defeat the control, so this value has to be true rather than convenient.

To see the refusal, deploy a second service in a US region with
`WORKER_REGION=US` and ask it for an EU case:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-US-SERVICE-URL/wiki/case:CASE-CMP-2026-0841
```

It answers 451 with the reasoning, and records the refusal as an event.

To retract a customer record and watch the cascade:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{"subject":"CUST-4471","fields":["name","address"],"reason":"right to erasure","requested_by":"customer"}'   https://YOUR-SERVICE-URL/retractions
```

## Replaying a case under a different rule

The rules the fleet runs under are data, and readable:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/policies
```

Pick a case and a point to rewind to. Any event id from the case's trace works;
one just before the assessment is the interesting one:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/cases/CASE-CMP-2026-0841/trace
```

Then replay it under a tighter threshold:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{"case_id":"CASE-CMP-2026-0841","rewind_to":"EVENT_ID","constants":{"gate_a_threshold":100}}'   https://YOUR-SERVICE-URL/replay
```

The response names the policy version it ran under, where the runs first differ,
and every downstream decision that changed. A replay reads recorded tool
responses only: it cannot reach CoreBank, and a missing recording stops it rather
than falling through to a live call.

## Testing a candidate agent version

Run a candidate in shadow across cases already in the log. It reads live data, produces the
actions it would have taken, and cannot change anything:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{"version_id":"correspondence-v2","agent_name":"correspondence_agent",
       "instruction":"Write to the customer as soon as the case is assessed.",
       "case_ids":["CASE-CMP-2026-0841","CASE-CMP-2026-0842"]}'   https://YOUR-SERVICE-URL/shadow
```

The response carries Gemini's categorised comparison and the promotion decision.
A candidate that behaved incorrectly or more riskily than the live version is
refused, and the reasons say which cases and why.

## Watching the shelves

The number that matters is the Desk's. It should stay roughly flat as the system
runs rather than climbing forever:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/shelves
```

The tiering job runs daily at 03:17 and moves events older than `HOT_TTL_DAYS`
from Firestore into BigQuery, then events older than `COLD_TTL_DAYS` from
BigQuery into Cloud Storage as Parquet. To run it now:

```bash
gcloud scheduler jobs run blackbox-tiering --location us-central1
```

It copies, reads each event back to confirm it arrived intact, and only then
removes it from Firestore. The response reports anything that failed
verification; those events stay on the Desk rather than being lost.

Set `WAREHOUSE_BUCKET` in `.env` before deploying, or cold storage stays
disabled and events accumulate in BigQuery instead.

## Running the red team

One campaign invents new attacks in each family, then re-runs everything that has
ever worked:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{"version":"fleet-2026-08-31","per_family":2}'   https://YOUR-SERVICE-URL/redteam/campaign
```

An attack counts as a success only when a policy boundary was crossed, checked
from recorded events. An agent that sounded rattled while holding every boundary
scores as a failure, which is the honest measure and the one that makes the curve
mean something.

The corpus and the two curves:

```bash
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/redteam/corpus
curl -H "Authorization: Bearer $TOKEN" https://YOUR-SERVICE-URL/redteam/metrics
```

Attacks run in scratch stores, so a successful one leaves no fraudulent refund in
the live Diary. The agent code and the boundaries are the live ones.

Note that the corpus persists to the instance filesystem, which Cloud Run does
not keep between revisions. Moving it to Cloud Storage is a small change and is
not done.

## What is deliberately not here

The Crash Test is Phase 9. Faults cannot yet be injected live: tool timeouts,
contradictory answers from two systems, a model refusal, or a workflow
interrupted mid-flight.
