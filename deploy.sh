#!/usr/bin/env bash
#
# BLACKBOX deploy, Phases 2 and 3.
#
# Creates nothing destructive and is safe to re-run: every step either creates a
# resource or reports that it already exists.
#
# Reads settings from .env so there is one place that says which project this is.
# Run it from the repo root:
#
#   bash deploy.sh
#
set -euo pipefail

if [[ ! -f .env ]]; then
  echo "No .env found. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in .env}"
: "${GOOGLE_CLOUD_LOCATION:=us-central1}"
: "${FIRESTORE_DATABASE:=blackbox-database}"
: "${COMPLAINTS_TOPIC:=blackbox-complaints}"
: "${APPROVALS_TOPIC:=blackbox-approvals}"
: "${REPLIES_TOPIC:=blackbox-customer-replies}"
: "${GEMINI_MODEL:=gemini-2.5-flash}"
# Region pinning is checked against this on every Wiki read, so it has to match
# the region the service actually runs in.
: "${WORKER_REGION:=EU}"
# Tiering. The Desk keeps recent events; older ones move outward.
: "${HOT_TTL_DAYS:=7}"
: "${COLD_TTL_DAYS:=365}"
: "${WAREHOUSE_BUCKET:=}"

PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="$GOOGLE_CLOUD_LOCATION"
SERVICE="blackbox"
RUNTIME_SA="blackbox-runtime"
INVOKER_SA="blackbox-invoker"
RUNTIME_EMAIL="${RUNTIME_SA}@${PROJECT}.iam.gserviceaccount.com"
INVOKER_EMAIL="${INVOKER_SA}@${PROJECT}.iam.gserviceaccount.com"

echo "==> Project: $PROJECT   Region: $REGION   Database: $FIRESTORE_DATABASE"
gcloud config set project "$PROJECT" >/dev/null

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudtrace.googleapis.com \
  artifactregistry.googleapis.com

echo "==> Service accounts"
# The runtime identity the service runs as. Least privilege: it writes the Diary,
# calls Gemini, and publishes to the topics. Nothing else.
gcloud iam service-accounts create "$RUNTIME_SA" \
  --display-name "BLACKBOX Cloud Run runtime" 2>/dev/null || echo "    runtime SA exists"
# A separate identity for Pub/Sub push and Cloud Scheduler, so the thing that
# triggers work is not the thing that does it.
gcloud iam service-accounts create "$INVOKER_SA" \
  --display-name "BLACKBOX Pub/Sub and Scheduler invoker" 2>/dev/null || echo "    invoker SA exists"

for ROLE in \
  roles/datastore.user \
  roles/aiplatform.user \
  roles/bigquery.dataEditor \
  roles/bigquery.jobUser \
  roles/storage.objectAdmin \
  roles/pubsub.publisher \
  roles/cloudtrace.agent \
  roles/logging.logWriter
do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${RUNTIME_EMAIL}" \
    --role "$ROLE" --condition=None >/dev/null
done
echo "    runtime roles bound"

echo "==> Pub/Sub topics"
# Three inbound paths, each waking a different part of the fleet.
for TOPIC in "$COMPLAINTS_TOPIC" "$APPROVALS_TOPIC" "$REPLIES_TOPIC"; do
  gcloud pubsub topics create "$TOPIC" 2>/dev/null || echo "    topic $TOPIC exists"
done

echo "==> Deploying to Cloud Run from source (Cloud Build does the container build)"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$RUNTIME_EMAIL" \
  --no-allow-unauthenticated \
  --timeout 600 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},FIRESTORE_DATABASE=${FIRESTORE_DATABASE},COMPLAINTS_TOPIC=${COMPLAINTS_TOPIC},APPROVALS_TOPIC=${APPROVALS_TOPIC},REPLIES_TOPIC=${REPLIES_TOPIC},GEMINI_MODEL=${GEMINI_MODEL},WORKER_REGION=${WORKER_REGION},HOT_TTL_DAYS=${HOT_TTL_DAYS},COLD_TTL_DAYS=${COLD_TTL_DAYS},WAREHOUSE_BUCKET=${WAREHOUSE_BUCKET},GOOGLE_GENAI_USE_VERTEXAI=TRUE,TRACE_EXPORTER=cloud_trace"

SERVICE_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
echo "==> Service URL: $SERVICE_URL"

echo "==> Allowing the invoker identity to call the service"
gcloud run services add-iam-policy-binding "$SERVICE" \
  --region "$REGION" \
  --member "serviceAccount:${INVOKER_EMAIL}" \
  --role roles/run.invoker >/dev/null

echo "==> Pub/Sub push subscriptions"
# Each of these is a way the fleet gets woken by something outside itself.
#   complaint -> the Intake Agent opens a case
#   approval  -> a suspended Assessment Agent carries on
#   reply     -> a sleeping case cuts its appeal window short
make_push_sub () {
  local NAME="$1"
  local TOPIC="$2"
  local ENDPOINT="$3"
  if ! gcloud pubsub subscriptions create "$NAME" \
      --topic "$TOPIC" \
      --push-endpoint "${SERVICE_URL}${ENDPOINT}" \
      --push-auth-service-account "$INVOKER_EMAIL" \
      --ack-deadline 600 2>/dev/null; then
    gcloud pubsub subscriptions update "$NAME" \
      --push-endpoint "${SERVICE_URL}${ENDPOINT}" \
      --push-auth-service-account "$INVOKER_EMAIL"
  fi
}

make_push_sub "${COMPLAINTS_TOPIC}-push" "$COMPLAINTS_TOPIC" "/pubsub/complaint"
make_push_sub "${APPROVALS_TOPIC}-push"  "$APPROVALS_TOPIC"  "/pubsub/approval"
make_push_sub "${REPLIES_TOPIC}-push"    "$REPLIES_TOPIC"    "/pubsub/customer-reply"

make_scheduler_job () {
  local NAME="$1"
  local SCHEDULE="$2"
  local ENDPOINT="$3"
  if ! gcloud scheduler jobs create http "$NAME" \
      --location "$REGION" \
      --schedule "$SCHEDULE" \
      --uri "${SERVICE_URL}${ENDPOINT}" \
      --http-method POST \
      --attempt-deadline 540s \
      --oidc-service-account-email "$INVOKER_EMAIL" \
      --oidc-token-audience "$SERVICE_URL" 2>/dev/null; then
    gcloud scheduler jobs update http "$NAME" \
      --location "$REGION" \
      --schedule "$SCHEDULE" \
      --uri "${SERVICE_URL}${ENDPOINT}" \
      --attempt-deadline 540s \
      --oidc-service-account-email "$INVOKER_EMAIL" \
      --oidc-token-audience "$SERVICE_URL"
  fi
}

echo "==> Cloud Scheduler: the inbound poller"
# Wakes on a timer and publishes anything new. Nobody presses a button.
make_scheduler_job blackbox-intake-poller "*/10 * * * *" "/ingest/poll"

echo "==> Warehouse bucket"
if [[ -n "$WAREHOUSE_BUCKET" ]]; then
  gcloud storage buckets create "gs://${WAREHOUSE_BUCKET}"     --location "$REGION" 2>/dev/null || echo "    bucket exists"
else
  echo "    WAREHOUSE_BUCKET unset, cold storage disabled"
fi

echo "==> Cloud Scheduler: the fleet heartbeat"
# The beat that gives suspended agents a chance to evaluate their own wake
# conditions. It starts no work of its own.
make_scheduler_job blackbox-heartbeat "*/5 * * * *" "/heartbeat"

echo "==> Cloud Scheduler: the tiering job"
# Moves aged events off the Desk so Firestore stays flat. Daily is plenty:
# the hot window is measured in days, not minutes.
make_scheduler_job blackbox-tiering "17 3 * * *" "/tiering/run"

cat <<EOM

Deployed.

  Service URL   $SERVICE_URL
  Topics        $COMPLAINTS_TOPIC
                $APPROVALS_TOPIC
                $REPLIES_TOPIC
  Subscriptions complaint -> /pubsub/complaint
                approval  -> /pubsub/approval
                reply     -> /pubsub/customer-reply
  Scheduler     blackbox-intake-poller, every 10 minutes
                blackbox-heartbeat,     every 5 minutes
                blackbox-tiering,       daily at 03:17

The fleet is now running on its own. The poller publishes the seeded complaints,
the Intake Agent opens a case for each, and the heartbeat gives suspended agents
a chance to wake. Nothing else needs to happen.

To watch it, authenticate with your own account:

  TOKEN=\$(gcloud auth print-identity-token)
  curl -H "Authorization: Bearer \$TOKEN" ${SERVICE_URL}/cases
  curl -H "Authorization: Bearer \$TOKEN" ${SERVICE_URL}/suspensions
  curl -H "Authorization: Bearer \$TOKEN" ${SERVICE_URL}/cases/CASE-CMP-2026-0841/reasoning

To make things happen now rather than waiting for the timers:

  gcloud scheduler jobs run blackbox-intake-poller --location $REGION
  gcloud scheduler jobs run blackbox-heartbeat --location $REGION

To grant an approval, publish one to the approvals topic:

  gcloud pubsub topics publish $APPROVALS_TOPIC --message \\
    '{"case_id":"CASE-CMP-2026-0841","gate":"A","approved":true,"approver":"you"}'

EOM
