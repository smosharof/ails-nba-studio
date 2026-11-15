#!/usr/bin/env bash
# Deploy Agent 2 (Cloud Run service, PRIVATE) and Agent 1 (Cloud Run Job + PAUSED weekly schedule).
# Builds from a temp staging dir so only the needed files are shipped.
#   ./07_deploy.sh agent2|agent2-public|agent1|console|console-public|all   ('all' keeps everything private)
set -euo pipefail

PROJECT="fall26-ails-termproj-nba"
REGION="us-central1"
ACCOUNT="${NBA_ACCOUNT:-$(gcloud config get-value account 2>/dev/null)}"   # your active gcloud login unless NBA_ACCOUNT is set
[ -n "$ACCOUNT" ] || { echo "no gcloud account: run 'gcloud auth login' (or export NBA_ACCOUNT)"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE_ROOT="${TMPDIR:-/tmp}/nba_deploy"
G=(--project="$PROJECT" --account="$ACCOUNT" --quiet)

deploy_agent2() {
  local d="$STAGE_ROOT/agent2"; rm -rf "$d"; mkdir -p "$d/outputs"
  cp "$HERE/common.py" "$HERE/agent2.py" "$HERE/sqltool.py" "$HERE/webauth.py" "$d/"
  [ -f "$HERE/outputs/evaluation.json" ] && cp "$HERE/outputs/evaluation.json" "$d/outputs/"
  cat > "$d/Dockerfile" <<'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "agent2.py", "--serve"]
EOF
  gcloud run deploy nba-agent2 --source "$d" --region "$REGION" \
    --service-account "nba-agent2-reader@${PROJECT}.iam.gserviceaccount.com" \
    --no-allow-unauthenticated --min-instances 0 --max-instances 2 \
    --memory 512Mi --cpu 1 --timeout 120 \
    --set-env-vars "NBA_PROJECT=${PROJECT}" "${G[@]}"
  # SAFETY: this build has no login, so it must never be publicly invokable. Remove a leftover public grant
  # from an earlier `agent2-public` deploy (ignore the error if there is none).
  gcloud run services remove-iam-policy-binding nba-agent2 --region "$REGION" \
    --member=allUsers --role=roles/run.invoker "${G[@]}" >/dev/null 2>&1 || true
}

deploy_agent2_public() {
  # Agent 2 reachable from the internet, gated by "Sign in with Google" (webauth.py) and an allowlist
  # (allowed_emails.txt). Needs: secret nba-oauth-client-secret (the OAuth client secret) and nba-session-key,
  # and NBA_OAUTH_CLIENT_ID set to the OAuth client ID. The console is NOT touched and stays private.
  : "${NBA_OAUTH_CLIENT_ID:?set NBA_OAUTH_CLIENT_ID to the OAuth client ID}"
  for s in nba-oauth-client-secret nba-session-key; do
    gcloud secrets describe "$s" "${G[@]}" >/dev/null 2>&1 || { echo "missing secret: $s"; exit 1; }
  done
  [ -s "$HERE/allowed_emails.txt" ] || { echo "allowed_emails.txt is missing or empty: copy allowed_emails.example.txt to allowed_emails.txt and list the people who may sign in"; exit 1; }
  local d="$STAGE_ROOT/agent2"; rm -rf "$d"; mkdir -p "$d/outputs"
  cp "$HERE/common.py" "$HERE/agent2.py" "$HERE/sqltool.py" "$HERE/webauth.py" "$d/"
  [ -f "$HERE/outputs/evaluation.json" ] && cp "$HERE/outputs/evaluation.json" "$d/outputs/"
  cat > "$d/Dockerfile" <<'DOCKER'
FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "agent2.py", "--serve"]
DOCKER
  local emails; emails="$(grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' "$HERE/allowed_emails.txt" | paste -sd, -)"
  local num; num="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)' --account="$ACCOUNT")"
  local url="https://nba-agent2-${num}.${REGION}.run.app"
  gcloud run deploy nba-agent2 --source "$d" --region "$REGION" \
    --service-account "nba-agent2-reader@${PROJECT}.iam.gserviceaccount.com" \
    --allow-unauthenticated --min-instances 0 --max-instances 2 \
    --memory 512Mi --cpu 1 --timeout 120 \
    --set-env-vars "^|^NBA_PROJECT=${PROJECT}|NBA_REQUIRE_LOGIN=1|OAUTH_CLIENT_ID=${NBA_OAUTH_CLIENT_ID}|BASE_URL=${url}|ALLOWED_EMAILS=${emails}" \
    --set-secrets "OAUTH_CLIENT_SECRET=nba-oauth-client-secret:latest,SESSION_KEY=nba-session-key:latest" "${G[@]}"
  echo "Agent 2 is public but gated by Sign in with Google: ${url}"
}

deploy_agent1() {
  local d="$STAGE_ROOT/agent1"; rm -rf "$d"; mkdir -p "$d/outputs"
  cp "$HERE"/{common.py,compliance.py,layer2.py,workflow.py,03_baseline_uplift.py,02_build_features.sql,agent1.py} "$d/"
  [ -f "$HERE/outputs/public_reference.json" ] && cp "$HERE/outputs/public_reference.json" "$d/outputs/"
  cat > "$d/Dockerfile" <<'EOF'
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pandas numpy scikit-learn
COPY . /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "agent1.py"]
EOF
  gcloud run jobs deploy nba-agent1 --source "$d" --region "$REGION" \
    --service-account "nba-pipeline@${PROJECT}.iam.gserviceaccount.com" \
    --memory 2Gi --cpu 2 --task-timeout 900 --max-retries 0 \
    --set-env-vars "NBA_PROJECT=${PROJECT}" "${G[@]}"

  # Weekly trigger (Mon 06:00 New York). Created PAUSED so nothing runs/bills until you resume it.
  gcloud iam service-accounts add-iam-policy-binding "nba-pipeline@${PROJECT}.iam.gserviceaccount.com" \
    --member="serviceAccount:nba-pipeline@${PROJECT}.iam.gserviceaccount.com" \
    --role=roles/iam.serviceAccountUser "${G[@]}" >/dev/null
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:nba-pipeline@${PROJECT}.iam.gserviceaccount.com" \
    --role=roles/run.invoker --condition=None "${G[@]}" >/dev/null
  local uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/nba-agent1:run"
  gcloud scheduler jobs create http nba-agent1-weekly --location "$REGION" \
    --schedule "0 6 * * 1" --time-zone "America/New_York" --uri "$uri" --http-method POST \
    --oauth-service-account-email "nba-pipeline@${PROJECT}.iam.gserviceaccount.com" "${G[@]}" 2>/dev/null \
    || echo "(scheduler job already exists)"
  gcloud scheduler jobs pause nba-agent1-weekly --location "$REGION" "${G[@]}"
}

smoke_gate() {
  # Never ship a console page that fails the real-browser smoke test (macOS: uses the system WebKit).
  if [ "$(uname)" = "Darwin" ] && [ -x "$HERE/test_page_smoke.sh" ]; then
    echo "running the page smoke test ..."
    "$HERE/test_page_smoke.sh" || { echo "page smoke test FAILED: not deploying"; exit 1; }
  fi
}

deploy_console() {
  # The console has no login of its own: it is PRIVATE on Cloud Run (IAM) and reached through the proxy.
  smoke_gate
  local d="$STAGE_ROOT/console"; rm -rf "$d"; mkdir -p "$d/outputs"
  cp "$HERE"/{common.py,compliance.py,layer2.py,workflow.py,demo.py,console.py,console_page.html,agent2.py,sqltool.py,webauth.py,03_baseline_uplift.py,02_build_features.sql} "$d/"
  [ -f "$HERE/outputs/public_reference.json" ] && cp "$HERE/outputs/public_reference.json" "$d/outputs/"
  [ -f "$HERE/outputs/evaluation.json" ] && cp "$HERE/outputs/evaluation.json" "$d/outputs/"
  cat > "$d/Dockerfile" <<'DOCKER'
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pandas numpy scikit-learn
COPY . /app
ENV PYTHONUNBUFFERED=1 NBA_BIND=0.0.0.0
CMD ["python", "console.py"]
DOCKER
  gcloud run deploy nba-console --source "$d" --region "$REGION" \
    --service-account "nba-pipeline@${PROJECT}.iam.gserviceaccount.com" \
    --no-allow-unauthenticated --min-instances 0 --max-instances 1 --concurrency 20 \
    --memory 2Gi --cpu 2 --no-cpu-throttling --timeout 3600 \
    --set-env-vars "NBA_PROJECT=${PROJECT},NBA_SQL_SA=nba-sql-reader@${PROJECT}.iam.gserviceaccount.com" "${G[@]}"
  # SAFETY: this build runs without login, so it must never be publicly invokable.
  gcloud run services remove-iam-policy-binding nba-console --region "$REGION" \
    --member=allUsers --role=roles/run.invoker "${G[@]}" >/dev/null 2>&1 || true
}

deploy_console_public() {
  # The console reachable from a browser, gated by Sign in with Google (webauth.py) + allowlist, with every request
  # needing a same-origin JSON call and every change audited. It can CHANGE DATA and run retrains: only add people you trust.
  # Optional: put emails in operator_emails.txt to let only those people change things (others can view).
  : "${NBA_OAUTH_CLIENT_ID:?set NBA_OAUTH_CLIENT_ID to the OAuth client ID}"
  for s in nba-oauth-client-secret nba-session-key; do
    gcloud secrets describe "$s" "${G[@]}" >/dev/null 2>&1 || { echo "missing secret: $s"; exit 1; }
  done
  [ -s "$HERE/allowed_emails.txt" ] || { echo "allowed_emails.txt is missing or empty: copy allowed_emails.example.txt to allowed_emails.txt and list the people who may sign in"; exit 1; }
  smoke_gate
  local d="$STAGE_ROOT/console"; rm -rf "$d"; mkdir -p "$d/outputs"
  cp "$HERE"/{common.py,compliance.py,layer2.py,workflow.py,demo.py,console.py,console_page.html,agent2.py,sqltool.py,webauth.py,03_baseline_uplift.py,02_build_features.sql} "$d/"
  [ -f "$HERE/outputs/public_reference.json" ] && cp "$HERE/outputs/public_reference.json" "$d/outputs/"
  [ -f "$HERE/outputs/evaluation.json" ] && cp "$HERE/outputs/evaluation.json" "$d/outputs/"
  cat > "$d/Dockerfile" <<'DOCKER'
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pandas numpy scikit-learn
COPY . /app
ENV PYTHONUNBUFFERED=1 NBA_BIND=0.0.0.0
CMD ["python", "console.py"]
DOCKER
  local emails; emails="$(grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' "$HERE/allowed_emails.txt" | paste -sd, -)"
  local ops=""
  if [ -s "$HERE/operator_emails.txt" ]; then
    ops="|OPERATOR_EMAILS=$(grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' "$HERE/operator_emails.txt" | paste -sd, -)"
  fi
  local num; num="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)' --account="$ACCOUNT")"
  local url="https://nba-console-${num}.${REGION}.run.app"
  gcloud run deploy nba-console --source "$d" --region "$REGION" \
    --service-account "nba-pipeline@${PROJECT}.iam.gserviceaccount.com" \
    --allow-unauthenticated --min-instances 0 --max-instances 1 --concurrency 20 \
    --memory 2Gi --cpu 2 --no-cpu-throttling --timeout 3600 \
    --set-env-vars "^|^NBA_PROJECT=${PROJECT}|NBA_REQUIRE_LOGIN=1|OAUTH_CLIENT_ID=${NBA_OAUTH_CLIENT_ID}|BASE_URL=${url}|ALLOWED_EMAILS=${emails}|SESSION_AUD=console|SESSION_TTL_S=3600|NBA_SQL_SA=nba-sql-reader@${PROJECT}.iam.gserviceaccount.com${ops}" \
    --set-secrets "OAUTH_CLIENT_SECRET=nba-oauth-client-secret:latest,SESSION_KEY=nba-session-key:latest" "${G[@]}"
  echo "Console is public but gated by Sign in with Google: ${url}"
}

case "${1:-all}" in
  agent2) deploy_agent2 ;;
  agent2-public) deploy_agent2_public ;;
  agent1) deploy_agent1 ;;
  console) deploy_console ;;
  console-public) deploy_console_public ;;
  all) deploy_agent2; deploy_agent1; deploy_console ;;
  *) echo "usage: $0 agent2|agent2-public|agent1|console|console-public|all"; exit 1 ;;
esac
