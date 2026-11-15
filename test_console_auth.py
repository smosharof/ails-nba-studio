#!/usr/bin/env python3
"""Attack tests for the console's sign-in gate (console.py + webauth.py). Runs locally; Google, BigQuery and the
workflow are stubbed, so nothing real is read or changed.

Run:  python3 -I test_console_auth.py        (exits non-zero if any check fails)
"""
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KEY = "k" * 48
ALLOWED = "member2@example.edu,member1@example.edu,viewer@example.com"
FAILS, PASSES = [], 0


def check(name, cond, detail=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name} {detail}")
        print("  FAIL:", name, detail)


def env(port, configured=True, operators=None):
    os.environ.update(NBA_REQUIRE_LOGIN="1", BASE_URL=f"http://127.0.0.1:{port}", SESSION_AUD="console")
    for k in ("OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "SESSION_KEY", "ALLOWED_EMAILS", "OPERATOR_EMAILS"):
        os.environ.pop(k, None)
    if configured:
        os.environ.update(OAUTH_CLIENT_ID="cid", OAUTH_CLIENT_SECRET="shh", SESSION_KEY=KEY, ALLOWED_EMAILS=ALLOWED)
    if operators:
        os.environ["OPERATOR_EMAILS"] = operators


def req(base, path, method="GET", body=None, headers=None):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        r = urllib.request.build_opener(NoRedirect).open(urllib.request.Request(base + path, data=data, method=method, headers=h), timeout=20)
    except urllib.error.HTTPError as e:
        r = e
    return r.status if hasattr(r, "status") else r.code, dict(r.headers), r.read().decode(errors="replace")


def main():
    import console
    import webauth
    calls = []
    # ---- stub everything that touches BigQuery, Gemini or the workflow --------------------------------
    console.workflow.get_state = lambda: {"layer1_model_version": "v", "model_cutoff": "2026-06-29", "latest_outcome_week": "2026-06-29", "new_outcome_weeks": 0}
    console.workflow.snapshot = lambda: {"contacts": 1, "call": 1, "email": 0, "digital": 0, "compliance_failures": 0, "withheld": 0}
    console.workflow.datasets_status = lambda: [{"table_id": "t", "row_count": 1, "last_modified": "x"}]
    console.workflow.recs_freshness = lambda: {"status": "pending", "published_at": "2026-11-14 23:46:16", "source_changed_at": "2026-11-15 21:26:52"}
    console.workflow.preview = lambda: calls.append("preview") or {"gate1": {}, "gate2": {}}
    console.workflow.execute = lambda run: calls.append("execute")
    console.demo.active_changes = lambda: {"changes": [], "overrides": []}
    console.demo.options = lambda: {"A": {}}
    console.demo.reset = lambda: calls.append("reset") or {"summary": "ok"}
    console.demo.inject_frequency_cap = lambda h, c=3: calls.append(f"inject {h}") or {"summary": "ok"}
    def _preview(name):
        if name != "features":
            raise ValueError("unknown table")
        calls.append("table features")
        return {"table": name, "columns": ["a"], "rows": [["1"]], "restricted": False, "note": None}
    console.workflow.table_preview = _preview
    audits = []
    console.audit = lambda user, action, detail: audits.append((user, action, detail))
    console.agent2.ask = lambda q: calls.append("ask") or {"answer": "stub:" + q, "tools_used": []}
    console.REQUIRE_LOGIN = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.H)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    key = KEY.encode()
    ck = lambda email, aud="console", ttl=600, k=key: {"Cookie": "nba_session=" + webauth.sign({"e": email, "a": aud}, ttl, k)}  # noqa: E731

    # ---- 1. closed when sign-in is not configured -----------------------------------------------------
    env(port, configured=False)
    for m, pth in [("GET", "/"), ("GET", "/api/state"), ("POST", "/api/reset"), ("POST", "/api/run")]:
        c, _, _ = req(base, pth, m, {} if m == "POST" else None)
        check(f"unconfigured: {m} {pth} is closed", c == 503, c)

    # ---- 2. every route needs a session ---------------------------------------------------------------------
    env(port)
    for pth in ("/api/state", "/api/options", "/api/datasets", "/api/run/abc", "/api/task/abc"):
        check(f"no session: GET {pth} is 401", req(base, pth)[0] == 401)
    check("no session: GET /api/table is 401", req(base, "/api/table?name=features")[0] == 401)
    check("no session: POST /api/ask is 401", req(base, "/api/ask", "POST", {"question": "x"})[0] == 401)
    for pth in ("/api/inject", "/api/reset", "/api/preview", "/api/run", "/api/run/abc/control"):
        check(f"no session: POST {pth} is 401", req(base, pth, "POST", {"option": "A", "hcp_id": "H100001"})[0] == 401)
    c, h, _ = req(base, "/"); check("no session: page redirects to /login", c == 302 and h.get("Location") == "/login", c)
    c, _, _ = req(base, "/anything"); check("no session: unknown page redirects", c == 302, c)
    check("nothing ran without a session", calls == [] and audits == [], (calls, audits))

    # ---- 3. cookie attacks (checked on a state-changing route) --------------------------------------------
    good = ck("member2@example.edu")
    raw, sig = good["Cookie"].split("=", 1)[1].split(".")
    attacks = {
        "signed with another key": ck("member2@example.edu", k=b"x" * 48),
        "tampered signature": {"Cookie": f"nba_session={raw}.{sig[:-2]}AA"},
        "payload swapped, old signature": {"Cookie": "nba_session=" + webauth.sign({"e": "evil@example.com", "a": "console"}, 600, key).split(".")[0] + "." + sig},
        "validly signed but not allowlisted": ck("evil@example.com"),
        "expired": ck("member2@example.edu", ttl=-5),
        "garbage": {"Cookie": "nba_session=garbage"},
        "issued for ANOTHER app (Agent 2 cookie)": ck("member2@example.edu", aud="default"),
    }
    for name, hdr in attacks.items():
        check(f"cookie attack rejected on /api/reset: {name}", req(base, "/api/reset", "POST", {}, hdr)[0] == 401)
        check(f"cookie attack rejected on /api/state: {name}", req(base, "/api/state", headers=hdr)[0] == 401)
        check(f"cookie attack rejected on /api/table: {name}", req(base, "/api/table?name=features", headers=hdr)[0] == 401)
        check(f"cookie attack rejected on /api/ask: {name}", req(base, "/api/ask", "POST", {"question": "x"}, hdr)[0] == 401)
    check("no cookie attack changed anything", calls == [] and audits == [], (calls, audits))

    # ---- 4. a valid operator -------------------------------------------------------------------------------------------
    c, _, b = req(base, "/", headers=good); check("operator: page loads and shows who is signed in", c == 200 and "member2@example.edu" in b, c)
    check("operator: GET /api/state works", req(base, "/api/state", headers=good)[0] == 200)
    c, _, _ = req(base, "/api/preview", "POST", {}, good); check("operator: preview works", c == 200 and "preview" in calls, c)
    c, _, _ = req(base, "/api/inject", "POST", {"option": "A", "hcp_id": "H100001"}, good); check("operator: inject works", c == 200 and "inject H100001" in calls, c)
    check("every change is audited with the user's email", ("member2@example.edu", "inject", {"option": "A", "hcp_id": "H100001"}) in audits, audits)
    c, _, _ = req(base, "/api/run", "POST", {"mode": "evil"}, good); check("run mode is validated (400)", c == 400, c)
    c, _, _ = req(base, "/api/run", "POST", {"mode": "stepper"}, good); check("valid run starts", c == 202, c)
    console.RUNS.clear(); console.TASKS.clear()          # the stubbed run never finishes; clear it so later checks are not blocked

    # ---- 5. CSRF / content type / size ------------------------------------------------------------------------------------
    n0 = len(calls)
    c, _, _ = req(base, "/api/reset", "POST", b"x=1", {**good, "Content-Type": "application/x-www-form-urlencoded"}); check("form post (CSRF style) rejected", c == 403, c)
    c, _, _ = req(base, "/api/reset", "POST", {}, {**good, "Origin": "https://evil.example"}); check("foreign Origin rejected", c == 403, c)
    c, _, _ = req(base, "/api/reset", "POST", b"{" + b" " * 9000 + b"}", good); check("oversized body rejected", c in (400, 413), c)
    check("rejected requests did not run anything", len(calls) == n0, calls[n0:])
    c, _, _ = req(base, "/api/reset", "POST", {}, {**good, "Origin": base}); check("our own Origin is accepted", c == 202, c)

    import time; time.sleep(0.5); console.RUNS.clear(); console.TASKS.clear()
    # ---- 6. viewer vs operator ----------------------------------------------------------------------------------------------------
    env(port, operators="member2@example.edu")
    viewer = ck("viewer@example.com")
    check("viewer can read state", req(base, "/api/state", headers=viewer)[0] == 200)
    check("state tells a viewer they are NOT an operator", '"operator": false' in req(base, "/api/state", headers=viewer)[2])
    check("state tells an operator they are", '"operator": true' in req(base, "/api/state", headers=good)[2])
    check("state carries the recommendations status", '"recs": {"status": "pending"' in req(base, "/api/state", headers=good)[2])
    check("state reports no active run when nothing is running", '"active_run": null' in req(base, "/api/state", headers=good)[2])
    class _R:  # a stand-in run that is paused at a gate
        status = "waiting"
    console.RUNS["rtest"] = _R()
    check("state reports the id of a paused run so a reloaded page can re-attach", '"active_run": "rtest"' in req(base, "/api/state", headers=good)[2])
    del console.RUNS["rtest"]
    check("viewer can load the page", req(base, "/", headers=viewer)[0] == 200)
    n0 = len(calls)
    for pth in ("/api/reset", "/api/preview", "/api/inject", "/api/run"):
        c, _, b = req(base, pth, "POST", {"option": "A", "hcp_id": "H100001"}, viewer)
        check(f"viewer cannot change anything: POST {pth}", c == 403 and "view" in b, (c, b[:60]))
    check("viewer's attempts ran nothing", len(calls) == n0, calls[n0:])
    check("operator still works", req(base, "/api/preview", "POST", {}, good)[0] == 200)

    # ---- 6b. the chat (viewer-level, read-only) -----------------------------------------------------------------
    c, _, b = req(base, "/", headers=viewer); check("page shows the NBA Studio tabs", c == 200 and "NBA Studio" in b and 'data-tab="recommender"' in b, c)
    c, _, b = req(base, "/api/ask", "POST", {"question": "hi"}, viewer); check("viewer CAN ask", c == 200 and "stub:hi" in b, (c, b[:60]))
    c, _, _ = req(base, "/api/ask", "POST", {"question": "hi"}, good); check("operator can ask", c == 200, c)
    check("asking is audited with the user's email", any(a[0] == "viewer@example.com" and a[1] == "ask" for a in audits), audits[-2:])
    c, _, _ = req(base, "/api/ask", "POST", b"question=hi", {**viewer, "Content-Type": "application/x-www-form-urlencoded"}); check("chat: form post rejected", c == 403, c)
    c, _, _ = req(base, "/api/ask", "POST", {"question": "hi"}, {**viewer, "Origin": "https://evil.example"}); check("chat: foreign Origin rejected", c == 403, c)
    c, _, _ = req(base, "/api/ask", "POST", {"question": "   "}, viewer); check("chat: empty question is 400", c == 400, c)
    c, _, b = req(base, "/api/ask", "POST", {"question": "x" * 5000}, viewer); check("chat: long question is truncated to 1000", c == 200 and b.count("x") <= 1005, (c, len(b)))
    c, _, _ = req(base, "/api/ask", "POST", b"{" + b" " * 9000 + b"}", viewer); check("chat: oversized body rejected", c in (400, 413), c)
    console.RUNS["r1"] = type("R", (), {"status": "running"})()
    c, _, _ = req(base, "/api/ask", "POST", {"question": "hi"}, viewer); check("asking is NOT blocked by a running workflow", c == 200, c)
    console.RUNS.clear()
    ask_user = ck("member1@example.edu")
    codes = [req(base, "/api/ask", "POST", {"question": "r"}, ask_user)[0] for _ in range(24)]
    check("chat rate limit kicks in (429) after 20", 429 in codes, codes[-5:])
    check("chat rate limit is per user", req(base, "/api/ask", "POST", {"question": "r"}, viewer)[0] == 200)

    # ---- 6c. table previews (read-only, any signed-in user) -------------------------------------------------------
    c, _, b = req(base, "/api/table?name=features", headers=viewer); check("viewer CAN preview a table", c == 200 and '"columns"' in b, c)
    c, _, _ = req(base, "/api/table?name=nope", headers=viewer); check("unknown table is a 400", c == 400, c)
    c, _, _ = req(base, "/api/table?name=features%3B%20DROP%20TABLE%20x", headers=viewer); check("hostile table name is a 400", c == 400, c)
    c, _, _ = req(base, "/api/table", headers=viewer); check("missing table name is a 400", c == 400, c)
    codes = [req(base, "/api/table?name=features", headers=ck("member1@example.edu"))[0] for _ in range(65)]
    check("table preview rate limit kicks in (429)", 429 in codes, codes[-4:])
    check("table preview rate limit is per user", req(base, "/api/table?name=features", headers=viewer)[0] == 200)

    # ---- 7. rate limit on change-making requests ----------------------------------------------------------------------------------
    env(port)
    other = ck("member1@example.edu")
    codes = [req(base, "/api/preview", "POST", {}, other)[0] for _ in range(65)]
    check("rate limit kicks in on actions (429)", 429 in codes, codes[-5:])
    check("reading is not rate limited", all(req(base, "/api/state", headers=other)[0] == 200 for _ in range(5)))
    check("rate limit is per user", req(base, "/api/preview", "POST", {}, good)[0] == 200)

    # ---- 8. headers, logs, local mode -------------------------------------------------------------------------------------------------------
    c, h, _ = req(base, "/", headers=good)
    check("security headers present", h.get("X-Frame-Options") == "DENY" and h.get("X-Content-Type-Options") == "nosniff" and "Content-Security-Policy" in h and h.get("Cache-Control") == "no-store")
    buf = io.StringIO()
    with redirect_stdout(buf):
        console.H.log_message(type("X", (), {"command": "GET", "path": "/oauth2callback?code=SECRET&state=s"})(), "x", "GET", "302")
    check("request log omits the query string", "SECRET" not in buf.getvalue())
    console.REQUIRE_LOGIN = False                       # local loopback use: no login, same as before
    check("local mode (login off) still works without a cookie", req(base, "/api/state")[0] == 200)

    srv.shutdown()
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
