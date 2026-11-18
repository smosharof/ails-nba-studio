"""Shared helpers: auth, BigQuery over REST, Gemini function-calling loop.

Standard library only (pandas is imported lazily when a DataFrame is requested),
so the same module runs on a laptop and inside Cloud Run.
Auth: on Cloud Run the metadata server supplies the token; locally we call gcloud.
"""
import csv
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROJECT = os.environ.get("NBA_PROJECT", "fall26-ails-termproj-nba")
DATASET = os.environ.get("NBA_DATASET", "nba")
ACCOUNT = os.environ.get("NBA_ACCOUNT")   # optional: which gcloud login to use locally (default: the active one)
GEMINI_MODEL = os.environ.get("NBA_GEMINI_MODEL", "gemini-3-flash-preview")
BQ_LOCATION = "US"

_tok = {"v": None, "exp": 0.0}


def token() -> str:
    if _tok["v"] and time.time() < _tok["exp"]:
        return _tok["v"]
    try:  # Cloud Run / GCE metadata server
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"})
        d = json.load(urllib.request.urlopen(req, timeout=3))
        _tok.update(v=d["access_token"], exp=time.time() + d["expires_in"] - 60)
    except Exception:  # local dev
        out = subprocess.run(["gcloud", "auth", "print-access-token"] + ([f"--account={ACCOUNT}"] if ACCOUNT else []),
                             capture_output=True, text=True, check=True).stdout.strip()
        _tok.update(v=out, exp=time.time() + 1800)
    return _tok["v"]


def _call(url, body=None, method="POST", headers=None, raw=False, timeout=120):
    h = {"Authorization": f"Bearer {token()}", "x-goog-user-project": PROJECT}
    if headers:
        h.update(headers)
    data = body if raw else (json.dumps(body).encode() if body is not None else None)
    if body is not None and not raw:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {e.read()[:600].decode(errors='replace')}")


# ----------------------------------------------------------------------- BigQuery
_CAST = {"INTEGER": int, "INT64": int, "FLOAT": float, "FLOAT64": float, "NUMERIC": float,
         "BOOLEAN": lambda v: v == "true", "BOOL": lambda v: v == "true"}


def bq_query(sql: str, max_rows: int = 2_000_000):
    """Run a query; return a list of dicts (typed). Also used for DDL/DML (returns [])."""
    base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}"
    r = _call(f"{base}/queries", {"query": sql, "useLegacySql": False, "location": BQ_LOCATION,
                                  "defaultDataset": {"projectId": PROJECT, "datasetId": DATASET},
                                  "timeoutMs": 120000, "maxResults": 100000})
    job = r["jobReference"]["jobId"]
    while not r.get("jobComplete", False):
        time.sleep(1)
        r = _call(f"{base}/queries/{job}?location={BQ_LOCATION}&maxResults=100000", method="GET")
    fields = r.get("schema", {}).get("fields", [])
    rows = []
    while True:
        for row in r.get("rows", []):
            rows.append({f["name"]: (None if c["v"] is None else _CAST.get(f["type"], str)(c["v"]))
                         for f, c in zip(fields, row["f"])})
        if "pageToken" in r and len(rows) < max_rows:
            r = _call(f"{base}/queries/{job}?location={BQ_LOCATION}&maxResults=100000"
                      f"&pageToken={urllib.parse.quote(r['pageToken'])}", method="GET")
        else:
            break
    return rows


def bq_df(sql: str):
    import pandas as pd
    rows = bq_query(sql)
    return pd.DataFrame(rows)


def bq_load_df(df, table: str):
    """Replace nba.<table> with the DataFrame (CSV multipart load job, schema autodetected)."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, quoting=csv.QUOTE_MINIMAL)
    boundary = uuid.uuid4().hex
    meta = {"configuration": {"load": {
        "destinationTable": {"projectId": PROJECT, "datasetId": DATASET, "tableId": table},
        "sourceFormat": "CSV", "skipLeadingRows": 1, "autodetect": True,
        "writeDisposition": "WRITE_TRUNCATE", "allowQuotedNewlines": True}},
        "jobReference": {"projectId": PROJECT, "location": BQ_LOCATION}}
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: text/csv\r\n\r\n{buf.getvalue()}\r\n--{boundary}--").encode()
    r = _call(f"https://bigquery.googleapis.com/upload/bigquery/v2/projects/{PROJECT}/jobs?uploadType=multipart",
              body, raw=True, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    jid = r["jobReference"]["jobId"]
    while r["status"]["state"] != "DONE":
        time.sleep(1)
        r = _call(f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/jobs/{jid}"
                  f"?location={BQ_LOCATION}", method="GET")
    if r["status"].get("errorResult"):
        raise RuntimeError(f"load into {table} failed: {r['status']['errorResult']}")


def log_run(run_id, agent, step, decision, rationale="", inputs="", outputs="", status="ok"):
    """Append one audit row to nba.run_log (streaming insert via DML)."""
    def esc(s):
        t = str(s)[:4000].replace("\\", "\\\\").replace("'", "\\'")
        t = t.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
        return "'" + t + "'"
    bq_query(
        "INSERT INTO nba.run_log (run_ts, run_id, agent, step, decision, rationale, inputs, outputs, status) "
        f"VALUES (CURRENT_TIMESTAMP(), {esc(run_id)}, {esc(agent)}, {esc(step)}, {esc(decision)}, "
        f"{esc(rationale)}, {esc(inputs)}, {esc(outputs)}, {esc(status)})")


# ------------------------------------------------------------------------- Gemini
def gemini(contents, system=None, tools=None, temperature=0.2, max_tokens=2048, json_mode=False):
    url = (f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/"
           f"publishers/google/models/{GEMINI_MODEL}:generateContent")
    body = {"contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
    for attempt in (1, 2):                      # one retry for a temporary rate-limit or service blip
        try:
            return _call(url, body)
        except RuntimeError as e:
            if attempt == 2 or not any(f"HTTP {code}" in str(e) for code in (429, 500, 502, 503, 504)):
                raise
            time.sleep(2.0)


def run_agent(system, user, tool_specs, tool_impls, max_steps=10, on_event=None):
    """Function-calling loop. Model content is echoed back verbatim (keeps thought signatures)."""
    contents = [{"role": "user", "parts": [{"text": user}]}]
    for step in range(max_steps):
        r = gemini(contents, system=system, tools=tool_specs)
        cand = r["candidates"][0]
        content = cand.get("content", {"role": "model", "parts": []})
        contents.append(content)
        calls = [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]
        if not calls:
            return "".join(p.get("text", "") for p in content.get("parts", [])).strip()
        said = " ".join(p.get("text", "") for p in content.get("parts", []) if p.get("text")).strip()
        responses = []
        for c in calls:
            name, args = c["name"], c.get("args", {})
            try:
                result = tool_impls[name](**args)
                status = "ok"
            except Exception as e:  # surface tool errors to the model
                result, status = {"error": str(e)[:500]}, "error"
            if on_event:
                on_event(step, name, args, result, status, said)
            responses.append({"functionResponse": {"name": name, "response": {"result": result}}})
        contents.append({"role": "user", "parts": responses})
    return "Stopped: reached the maximum number of steps."
