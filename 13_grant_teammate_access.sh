#!/usr/bin/env bash
# Give a teammate the SCOPED roles needed to deploy and run this project (and take them away again).
#   ./13_grant_teammate_access.sh name@example.edu [more@example.edu ...]      grant
#   ./13_grant_teammate_access.sh --revoke name@example.edu [...]              remove every grant below
# Run it as someone who can manage IAM on the project (the project owner). Idempotent: safe to run twice.
#
# What this does NOT give: Owner/Editor, anything on billing or IAM, permission to read secret VALUES
# (Secret Manager Viewer shows that secrets exist; it cannot read them), or permission to impersonate the restricted
# SQL account (nba-sql-reader). Note: anyone who can deploy can ship code that runs as the service accounts below,
# so they effectively inherit what those accounts can do (edit BigQuery data, call Vertex AI).
set -euo pipefail

P="fall26-ails-termproj-nba"
REGION="us-central1"
BUCKET="gs://run-sources-${P}-${REGION}"       # where `gcloud run deploy --source` uploads the code
VERB="add"
if [ "${1:-}" = "--revoke" ]; then VERB="remove"; shift; fi
[ "$#" -ge 1 ] || { echo "usage: $0 [--revoke] email [email ...]"; exit 1; }

PROJECT_ROLES=(
  roles/run.admin                         # deploy and update the console / Agent 2, make a service public or private
  roles/cloudbuild.builds.editor          # build the container from source
  roles/cloudscheduler.admin              # Agent 1's (paused) weekly schedule
  roles/serviceusage.serviceUsageConsumer # let command-line tools use the project's APIs
  roles/bigquery.dataEditor               # run the pipeline scripts and tests against the dataset
  roles/bigquery.jobUser
  roles/aiplatform.user                   # Gemini calls from local scripts
  roles/secretmanager.viewer              # see that secrets exist (cannot read their values)
  roles/logging.viewer                    # read Cloud Run logs
)
NUM="$(gcloud projects describe "$P" --format='value(projectNumber)')"
ACT_AS=("nba-pipeline@${P}.iam.gserviceaccount.com" "nba-agent2-reader@${P}.iam.gserviceaccount.com" "${NUM}-compute@developer.gserviceaccount.com")

for U in "$@"; do
  [[ "$U" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { echo "not an email address: $U"; exit 1; }
  M="user:$(printf '%s' "$U" | tr '[:upper:]' '[:lower:]')"
  echo "== ${VERB} scoped roles for ${U}"
  for R in "${PROJECT_ROLES[@]}"; do
    if [ "$VERB" = add ]; then
      gcloud projects add-iam-policy-binding "$P" --member="$M" --role="$R" --condition=None --quiet >/dev/null
    else
      gcloud projects remove-iam-policy-binding "$P" --member="$M" --role="$R" --all --quiet >/dev/null 2>&1 || true
    fi
    echo "   project  $R"
  done
  for SA in "${ACT_AS[@]}"; do
    if [ "$VERB" = add ]; then
      gcloud iam service-accounts add-iam-policy-binding "$SA" --member="$M" --role=roles/iam.serviceAccountUser --project="$P" --quiet >/dev/null
    else
      gcloud iam service-accounts remove-iam-policy-binding "$SA" --member="$M" --role=roles/iam.serviceAccountUser --project="$P" --quiet >/dev/null 2>&1 || true
    fi
    echo "   act-as   $SA"
  done
  if [ "$VERB" = add ]; then
    gcloud storage buckets add-iam-policy-binding "$BUCKET" --member="$M" --role=roles/storage.objectAdmin --quiet >/dev/null
  else
    gcloud storage buckets remove-iam-policy-binding "$BUCKET" --member="$M" --role=roles/storage.objectAdmin --quiet >/dev/null 2>&1 || true
  fi
  echo "   bucket   ${BUCKET} (storage.objectAdmin)"
done
echo "done."
