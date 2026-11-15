#!/usr/bin/env bash
# One-time (idempotent) setup for the chat's text-to-SQL tool: a curated dataset, a restricted service account and
# impersonation, so generated SQL can only ever read two views.
#
#   nba_chat.recommendations   curated view of the published recommendations
#   nba_chat.hcp_profile       curated view of HCP attributes (pseudonymous IDs, simulated data)
#   nba-sql-reader@...         service account: can run queries and read ONLY dataset nba_chat. Nothing else.
#   nba-pipeline / you         may impersonate it (roles/iam.serviceAccountTokenCreator), which is how the console runs
#                              generated SQL as the restricted identity instead of its own broad one.
#
# The views read the production dataset through AUTHORIZED VIEWS, so the restricted account needs no access to `nba`.
set -euo pipefail

P="fall26-ails-termproj-nba"
DS="nba_chat"
SA="nba-sql-reader"
SA_EMAIL="${SA}@${P}.iam.gserviceaccount.com"
CONSOLE_SA="nba-pipeline@${P}.iam.gserviceaccount.com"
ME="$(gcloud config get-value account 2>/dev/null)"

echo "1/6 enable the IAM Credentials API"
gcloud services enable iamcredentials.googleapis.com --project="$P" --quiet

echo "2/6 dataset ${DS}"
bq --project_id="$P" mk --dataset --location=US --description="Curated read-only views for the chat's SQL tool" "${P}:${DS}" 2>/dev/null || echo "  (already exists)"

echo "3/6 views"
bq --project_id="$P" query --use_legacy_sql=false --quiet "
CREATE OR REPLACE VIEW \`${P}.${DS}.recommendations\` AS
SELECT hcp_id, priority_rank, contact_flag, final_channel, model_channel, substituted_from, blocked_reason,
       final_uplift, uplift_call, uplift_email, uplift_digital, cluster, segment, rationale, message_guidance,
       CAST(evidence_pmid AS STRING) AS evidence_pmid, compliance_ok, model_version, CAST(scoring_week AS STRING) AS scoring_week
FROM \`${P}.nba.recommendations_final\`" >/dev/null
bq --project_id="$P" query --use_legacy_sql=false --quiet "
CREATE OR REPLACE VIEW \`${P}.${DS}.hcp_profile\` AS
SELECT m.hcp_id, m.specialty, m.region, m.state, m.practice_setting, m.years_in_practice, m.pct_medicare,
       m.pct_commercial, m.pct_medicaid, m.mean_patient_age, m.pct_cvd_comorbid,
       s.segment, s.growth_group, s.decile, s.email_consent
FROM \`${P}.nba.raw_hcp_master\` m LEFT JOIN \`${P}.nba.raw_hcp_segments\` s USING (hcp_id)" >/dev/null

echo "4/6 authorize the views to read the production dataset"
python3 - "$P" "$DS" <<'PY'
import json, subprocess, sys, tempfile, os
P, DS = sys.argv[1], sys.argv[2]
cur = json.loads(subprocess.run(["bq", f"--project_id={P}", "show", "--format=prettyjson", f"{P}:nba"], capture_output=True, text=True, check=True).stdout)
have = {(a["view"]["tableId"]) for a in cur.get("access", []) if "view" in a and a["view"].get("datasetId") == DS}
for v in ("recommendations", "hcp_profile"):
    if v not in have:
        cur["access"].append({"view": {"projectId": P, "datasetId": DS, "tableId": v}})
f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(cur, f); f.close()
subprocess.run(["bq", f"--project_id={P}", "update", "--source", f.name, f"{P}:nba"], check=True, capture_output=True)
os.unlink(f.name); print("  authorized:", sorted(a["view"]["tableId"] for a in cur["access"] if "view" in a))
PY

echo "5/6 restricted service account ${SA}"
gcloud iam service-accounts create "$SA" --display-name="Chat SQL reader (reads nba_chat only)" --project="$P" --quiet 2>/dev/null || echo "  (already exists)"
gcloud projects add-iam-policy-binding "$P" --member="serviceAccount:${SA_EMAIL}" --role=roles/bigquery.jobUser --condition=None --quiet >/dev/null
python3 - "$P" "$DS" "$SA_EMAIL" <<'PY'
import json, subprocess, sys, tempfile, os
P, DS, SA = sys.argv[1:4]
cur = json.loads(subprocess.run(["bq", f"--project_id={P}", "show", "--format=prettyjson", f"{P}:{DS}"], capture_output=True, text=True, check=True).stdout)
if not any(a.get("userByEmail", "").lower() == SA.lower() for a in cur.get("access", [])):
    cur["access"].append({"role": "READER", "userByEmail": SA})
f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(cur, f); f.close()
subprocess.run(["bq", f"--project_id={P}", "update", "--source", f.name, f"{P}:{DS}"], check=True, capture_output=True)
os.unlink(f.name); print("  READER on", DS, "granted to the restricted account only")
PY

echo "6/6 who may impersonate it: the console's account and you (for local tests)"
for M in "serviceAccount:${CONSOLE_SA}" "user:${ME}"; do
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --member="$M" --role=roles/iam.serviceAccountTokenCreator --project="$P" --quiet >/dev/null
done
echo "done. Restricted account: ${SA_EMAIL}"
