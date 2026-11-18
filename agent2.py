#!/usr/bin/env python3
"""Agent 2 -- plain-language access to the recommendation table.

Gemini function-calling over a small set of FIXED, validated query tools that read
only the view nba.v_recommendations. No free-form SQL, no raw/feature tables, and
HCP-facing wording comes only from pre-approved, compliance-checked message_guidance.

Local test:   python3 -I agent2.py "Who are the top 5 HCPs to call this week?"
Web service:  python3 -I agent2.py --serve      (PORT env var, default 8080)
Standard library only, so it runs unchanged on Cloud Run.
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import answer_check  # noqa: E402
import common  # noqa: E402
import sqltool  # noqa: E402
import webauth  # noqa: E402

VIEW = "nba.v_recommendations"
CHANNELS = {"call", "email", "digital"}
SEGMENTS = {"Movers", "Core", "Long tail", "Access-constrained", "Entrenched"}
COLS = ("hcp_id, priority_rank, contact_flag, final_channel, model_channel, substituted_from, "
        "blocked_reason, ROUND(final_uplift, 3) AS uplift_score, cluster, segment")

SYSTEM = """You are Agent 2, a plain-language assistant for sales operations and marketing
at a pharma company (GLUCOREN, Type 2 Diabetes). You answer questions about the Next-Best-Action
recommendation table using ONLY your tools. Rules:
- Never invent numbers, HCP IDs or reasons; if a tool does not return it, say you don't have it.
- When you list HCPs, show each one's priority_rank exactly as the tool returned it (an overall rank), never your own list position.
- Give a reason (for example why a channel was switched or an HCP was recommended) ONLY if a tool returned it, in fields such as
  blocked_reason or rationale; otherwise report the count or fact without explaining why.
- If the user names a channel (call, email, digital) or segment, you MUST pass it as a filter to top_hcps,
  and always state each HCP's recommended channel in your answer.
- Be concise and plain; use short lists or small tables. Lead with the answer.
- 'uplift_score' is a RANKING score, not a promised outcome: validation showed the absolute size
  is overstated by roughly 1.6x to 2.5x (depending on the validation split), and the ranking is only moderately
  stable over time. The clearest gains were for the 'Movers' segment; other segments were not reliably positive. Say so when
  someone asks how much lift to expect, and call model_info for caveats.
- Whenever you cite a PubMed reference, you MUST say it is comparator-class context (studies of metformin or
  sulfonylureas), NOT evidence about GLUCOREN itself, and never imply it supports a GLUCOREN claim.
- If the fixed tools cannot answer (counts, averages, comparisons, or filters on HCP attributes such as specialty, region, segment or consent), and ask_data is available, call ask_data ONCE with the user's question in plain words. Use the fixed tools first whenever one fits. Report the numbers ask_data returns faithfully; the SQL it used is shown to the user separately, so do not paste it. If ask_data returns an error, say plainly you cannot answer that from the recommendation data.
- You may share a pre-approved message only by calling talking_point. Never write your own
  product claims, efficacy statements, or comparisons; if asked to, decline and offer talking_point.
- You do not execute outreach; humans in sales ops and marketing do."""

TOOLS = [
    {"name": "top_hcps", "description": "Highest-priority HCPs to contact now, optionally filtered by recommended channel and/or segment.",
     "parameters": {"type": "object", "properties": {
         "n": {"type": "integer", "description": "How many (1-50), default 10"},
         "channel": {"type": "string", "description": "call, email or digital"},
         "segment": {"type": "string", "description": "Movers, Core, Long tail, Access-constrained or Entrenched"}}}},
    {"name": "explain_hcp", "description": "Why one HCP got their recommendation (channel, rank, substitution, caps, rationale, evidence).",
     "parameters": {"type": "object", "properties": {"hcp_id": {"type": "string", "description": "e.g. H100123"}}, "required": ["hcp_id"]}},
    {"name": "talking_point", "description": "The pre-approved, compliance-checked message for an HCP's recommended channel.",
     "parameters": {"type": "object", "properties": {"hcp_id": {"type": "string"}}, "required": ["hcp_id"]}},
    {"name": "channel_mix", "description": "How many contacts per channel, plus how many were switched to another channel because of frequency caps or consent.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "segment_summary", "description": "Contacts and average ranking score per segment.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "model_info", "description": "Current model version, scoring week, contact budget, and validation caveats.",
     "parameters": {"type": "object", "properties": {}}},
]


def _q(sql):
    return common.bq_query(sql)


def top_hcps(n=10, channel=None, segment=None):
    n = max(1, min(int(n or 10), 50))
    where = ["contact_flag"]
    if channel:
        if channel not in CHANNELS:
            return {"error": f"channel must be one of {sorted(CHANNELS)}"}
        where.append(f"final_channel = '{channel}'")
    if segment:
        if segment not in SEGMENTS:
            return {"error": f"segment must be one of {sorted(SEGMENTS)}"}
        where.append(f"segment = '{segment}'")
    return _q(f"SELECT {COLS} FROM {VIEW} WHERE {' AND '.join(where)} ORDER BY priority_rank LIMIT {n}")


def _check_id(hcp_id):
    if not re.fullmatch(r"H\d{4,8}", str(hcp_id or "")):
        raise ValueError("hcp_id must look like H100123")
    return hcp_id


def explain_hcp(hcp_id):
    rows = _q(f"SELECT {COLS}, rationale, evidence_pmid, model_version FROM {VIEW} WHERE hcp_id = '{_check_id(hcp_id)}'")
    return rows[0] if rows else {"error": "HCP not found"}


def talking_point(hcp_id):
    rows = _q(f"SELECT contact_flag, final_channel, message_guidance, compliance_ok FROM {VIEW} "
              f"WHERE hcp_id = '{_check_id(hcp_id)}'")
    if not rows:
        return {"error": "HCP not found"}
    r = rows[0]
    if not r["contact_flag"] or not r["compliance_ok"] or not r["message_guidance"]:
        return {"message": None, "note": "No approved message: HCP is not on this period's contact list."}
    return {"channel": r["final_channel"], "approved_message": r["message_guidance"]}


def channel_mix():
    return _q(f"SELECT final_channel, COUNT(*) AS hcps, COUNTIF(substituted_from IS NOT NULL) AS switched_from_other_channel "
              f"FROM {VIEW} WHERE contact_flag GROUP BY 1 ORDER BY hcps DESC")


def segment_summary():
    return _q(f"SELECT segment, COUNT(*) AS contacts, ROUND(AVG(final_uplift), 3) AS avg_score "
              f"FROM {VIEW} WHERE contact_flag GROUP BY 1 ORDER BY contacts DESC")


def model_info():
    meta = _q(f"SELECT ANY_VALUE(model_version) AS model_version, MAX(scoring_week) AS scoring_week, "
              f"COUNT(*) AS hcps, COUNTIF(contact_flag) AS contacts FROM {VIEW}")[0]
    meta["contact_budget"] = "top 30% of HCPs by ranking score"
    ev = Path(__file__).resolve().parent / "outputs" / "evaluation.json"
    summary = ("Out-of-fold validation: actual lift per contact ~0.24-0.37 vs ~0.61 predicted (about 1.6x-2.5x overstated, "
               "depending on the validation split); clearest gains for 'Movers', other segments not reliably positive; "
               "ranking only moderately stable over time. Data are simulated; real Part D comparison shows the "
               "simulation compresses differences between specialties.")
    meta["validation_summary"] = summary         # always returned, so what the assistant says about caveats is also in its evidence
    if ev.exists():
        meta["validation_caveats"] = json.loads(ev.read_text())
    return meta


IMPLS = {"top_hcps": top_hcps, "explain_hcp": explain_hcp, "talking_point": talking_point,
         "channel_mix": channel_mix, "segment_summary": segment_summary, "model_info": model_info}


ASK_DATA_SPEC = {"name": "ask_data",
                 "description": ("Answer a question the fixed tools cannot (counts, averages, comparisons, filters on HCP attributes like "
                                 "specialty, region, segment or email consent) by running a read-only query on the recommendation data. "
                                 "Use only when no other tool fits."),
                 "parameters": {"type": "object", "properties": {"question": {"type": "string", "description": "The user's question in plain words"}},
                                "required": ["question"]}}


def capabilities_text():
    """What to tell the user when a question is out of scope. Built per service, so it never promises SQL where there is none."""
    sql = (" You can also count or compare HCPs by specialty, region, segment or email consent." if sqltool.enabled() else "")
    return ("\n- When you cannot answer, or the question is off topic, do not just say you do not have it. In one or two friendly sentences, say what "
            "you CAN do: show the top HCPs to contact, explain why one HCP was recommended, share an HCP's pre-approved message, show contacts "
            "by channel or segment, and explain the model's caveats." + sql + " Never offer anything beyond that.")


def ask(question: str):
    used, sql_log, evidence = [], [], []
    calls = {"ask_data": 0}
    tools, impls = list(TOOLS), dict(IMPLS)
    if sqltool.enabled():
        def ask_data(question=""):
            calls["ask_data"] += 1
            if calls["ask_data"] > sqltool.MAX_CALLS_PER_QUESTION:
                return {"error": "limit reached: one data query per question"}
            r = sqltool.answer(str(question)[:1000])
            if r.get("sql"):
                sql_log.append({"sql": r["sql"], "rows": r.get("row_count"), "error": r.get("error"), "truncated": r.get("truncated", False)})
            if "rows" in r:   # keep what the model sees small; the full (capped) result is not needed to write the answer
                return {"columns": r["columns"], "rows": r["rows"][:60], "row_count": r["row_count"], "truncated": r["truncated"] or r["row_count"] > 60}
            return {"error": r.get("error", "failed")}
        tools.append(ASK_DATA_SPEC)
        impls["ask_data"] = ask_data

    def on_event(step, name, args, result, status, said):
        used.append({"tool": name, "args": args, "status": status})
        if status == "ok":
            evidence.append(result)           # what the tools actually returned: the evidence the answer is checked against
    answer = common.run_agent(SYSTEM + capabilities_text(), question[:2000], tools, impls, max_steps=6, on_event=on_event)
    rev = answer_check.review(question, answer, evidence)     # grounding check + second-model judge, before anything is shown
    out = {"answer": rev["answer"], "tools_used": used, "review": rev["review"]}
    if sql_log:
        out["sql"] = sql_log[-1]["sql"]
        out["sql_rows"] = sql_log[-1]["rows"]
        out["sql_error"] = sql_log[-1]["error"]
    return out


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>NBA Assistant</title><style>body{font:16px system-ui;max-width:760px;margin:2rem auto;padding:0 16px}
textarea{width:100%;height:4rem;font:inherit}button{margin:.5rem 0;padding:.5rem 1rem}
pre{white-space:pre-wrap;background:#f4f4f4;padding:12px;border-radius:6px}small{color:#666}.who{float:right}</style>
<div class=who><small>__WHO__</small></div>
<h2>GLUCOREN Next-Best-Action assistant</h2>
<textarea id=q maxlength=1000 placeholder="e.g. Who are the top 5 HCPs to call this week, and why?"></textarea><br>
<button onclick=go()>Ask</button> <small>Answers come only from the recommendation table. Simulated data.</small>
<pre id=a></pre><script>
async function go(){const a=document.getElementById('a');a.textContent='Thinking...';
const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({question:document.getElementById('q').value})});
if(r.status===401){location='/login';return}
const j=await r.json();a.textContent=j.answer||j.error||JSON.stringify(j)}</script>"""

# When set, every route except the sign-in flow needs a signed-in, allowlisted Google account (see webauth.py).
REQUIRE_LOGIN = os.environ.get("NBA_REQUIRE_LOGIN") == "1"
MAX_BODY = 8 * 1024
MAX_QUESTION = 1000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # never log query strings: they carry one-time sign-in codes
        print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)

    def _send(self, code, body, ctype="application/json", cookies=(), location=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        if location:
            self.send_header("Location", location)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(data)

    def _page(self, code, title, text, link=None):
        import html
        body = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                f"<body style='font:16px system-ui;max-width:560px;margin:3rem auto;padding:0 16px'><h3>{html.escape(title)}</h3>"
                f"<p>{html.escape(text)}</p>" + (f"<p><a href='{link[0]}'>{html.escape(link[1])}</a></p>" if link else ""))
        self._send(code, body, "text/html; charset=utf-8")

    def do_GET(self):
        import html
        import urllib.parse
        path, _, qs = self.path.partition("?")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        who = "local use"
        if REQUIRE_LOGIN:
            if not webauth.configured():
                return self._page(503, "Not available", "Sign-in is not configured, so this service is closed.")
            if path == "/login":
                loc, cookie = webauth.login_redirect()
                return self._send(302, "", "text/plain", [cookie], loc)
            if path == "/oauth2callback":
                ok, msg, cookies = webauth.finish_login(query, self.headers)
                if ok:
                    return self._send(302, "", "text/plain", cookies, "/")
                return self._send(403, f"<!doctype html><meta charset=utf-8><body style='font:16px system-ui;max-width:560px;margin:3rem auto'>"
                                       f"<h3>Access denied</h3><p>{html.escape(msg)}</p><p><a href='/login'>Try again</a></p>",
                                  "text/html; charset=utf-8", cookies)
            if path == "/logout":
                return self._send(200, "<!doctype html><meta charset=utf-8><body style='font:16px system-ui;margin:3rem'>Signed out. <a href='/login'>Sign in again</a>",
                                  "text/html; charset=utf-8", [webauth.cookie_header("nba_session", "", 0)])
            user = webauth.current_user(self.headers)
            if not user:
                return self._send(302, "", "text/plain", [], "/login")
            who = f"{html.escape(user)} · <a href='/logout'>Sign out</a>"
        if path != "/":
            return self._send(404, '{"error":"not found"}')
        self._send(200, PAGE.replace("__WHO__", who), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.split("?")[0] != "/ask":
            return self._send(404, '{"error":"not found"}')
        try:
            user = "local"
            if REQUIRE_LOGIN:
                if not webauth.configured():
                    return self._send(503, '{"error":"closed"}')
                user = webauth.current_user(self.headers)
                if not user:
                    return self._send(401, '{"error":"sign in required"}')
                if not webauth.origin_ok(self.headers):
                    return self._send(403, '{"error":"bad origin or content type"}')
                if not webauth.rate_ok(user):
                    return self._send(429, '{"error":"too many requests, please wait a few minutes"}')
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                return self._send(413, '{"error":"request too large"}')
            body = json.loads(self.rfile.read(n) or b"{}")
            q = (body.get("question") or "").strip()[:MAX_QUESTION]
            if not q:
                return self._send(400, '{"error":"question required"}')
            self._send(200, json.dumps(ask(q)))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)[:300]}))


if __name__ == "__main__":
    if "--serve" in sys.argv:
        port = int(os.environ.get("PORT", "8080"))
        print(f"Agent 2 listening on :{port}", flush=True)
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    else:
        q = " ".join(a for a in sys.argv[1:]) or "Give me a quick overview of this week's recommendations."
        r = ask(q)
        print("Tools:", [t["tool"] for t in r["tools_used"]])
        print(r["answer"])
