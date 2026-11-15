#!/usr/bin/env bash
# Load the GLUCOREN course CSVs into Cloud Storage and BigQuery (nba.raw_*).
# Read-only on the course repo. Re-runnable: tables are replaced each run.
set -euo pipefail

PROJECT="fall26-ails-termproj-nba"
BUCKET="gs://${PROJECT}-raw"
DATASET="nba"
ACCOUNT="${NBA_ACCOUNT:-$(gcloud config get-value account 2>/dev/null)}"   # your active gcloud login unless NBA_ACCOUNT is set
[ -n "$ACCOUNT" ] || { echo "no gcloud account: run 'gcloud auth login' (or export NBA_ACCOUNT)"; exit 1; }
REPO="${NBA_COURSE_REPO:?set NBA_COURSE_REPO to the course repo folder that holds the Week 04 and Week 05 data}"
W4="${REPO}/Week 04 - HCP and Patient Segmentation/data"
W5="${REPO}/Week 05 - Omnichannel Optimization and Next Best Action/data"

# table_name | source file
FILES=(
  "raw_hcp_monthly|${W4}/glucoren_hcp_monthly.csv"
  "raw_hcp_master|${W4}/glucoren_hcp_master.csv"
  "raw_touch_log|${W5}/glucoren_touch_log.csv"
  "raw_channel_affinity|${W5}/glucoren_hcp_channel_affinity.csv"
  "raw_hcp_segments|${W5}/glucoren_hcp_segments.csv"
)

for entry in "${FILES[@]}"; do
  table="${entry%%|*}"
  src="${entry#*|}"
  echo "==> ${table}"
  gcloud storage cp "${src}" "${BUCKET}/glucoren/${table}.csv" \
    --project="${PROJECT}" --account="${ACCOUNT}" --quiet
  bq --project_id="${PROJECT}" load --replace --source_format=CSV \
    --skip_leading_rows=1 --autodetect \
    "${DATASET}.${table}" "${BUCKET}/glucoren/${table}.csv"
done

echo "==> row counts"
bq --project_id="${PROJECT}" query --use_legacy_sql=false --format=pretty '
SELECT "raw_hcp_monthly" AS tbl, COUNT(*) AS n FROM nba.raw_hcp_monthly UNION ALL
SELECT "raw_hcp_master", COUNT(*) FROM nba.raw_hcp_master UNION ALL
SELECT "raw_touch_log", COUNT(*) FROM nba.raw_touch_log UNION ALL
SELECT "raw_channel_affinity", COUNT(*) FROM nba.raw_channel_affinity UNION ALL
SELECT "raw_hcp_segments", COUNT(*) FROM nba.raw_hcp_segments'
