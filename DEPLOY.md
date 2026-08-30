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

**The model id is rejected.** `GEMINI_MODEL` defaults to `gemini-3.5-flash`. If
Vertex AI in your region does not serve that id, set the one it does serve and
redeploy. Nothing else in the codebase names a model.

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

## What is deliberately not here

The tiering job from Phase 1.5 is still a stub. It does not copy to BigQuery and
it does not delete from Firestore, so Firestore will grow. That is fine at these
volumes and needs fixing before the Filing Cabinet claim is true.

The Eraser is Phase 5. Wiki pages record `derived_from`, so the graph a
retraction cascade would walk already exists, but nothing walks it yet and no
region pinning is enforced on where a page is stored.
