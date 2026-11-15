#!/usr/bin/env python3
"""Attack tests for the Agent 2 sign-in gate (webauth.py + agent2.py). Runs locally; Google is stubbed.

Run:  python3 -I test_webauth.py        (exits non-zero if any check fails)
"""
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KEY = "k" * 48
ALLOWED = "Member2@example.edu, member1@example.edu, Member4@example.com"
FAILS, PASSES = [], 0


def check(name, cond, detail=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name} {detail}")
        print("  FAIL:", name, detail)


def setup(port, configured=True):
    os.environ.update(NBA_REQUIRE_LOGIN="1", BASE_URL=f"http://127.0.0.1:{port}")
    for k in ("OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "SESSION_KEY", "ALLOWED_EMAILS"):
        os.environ.pop(k, None)
    if configured:
        os.environ.update(OAUTH_CLIENT_ID="cid.apps.googleusercontent.com", OAUTH_CLIENT_SECRET="shh",
                          SESSION_KEY=KEY, ALLOWED_EMAILS=ALLOWED)


def req(base, path, method="GET", body=None, headers=None):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)
    try:
        r = op.open(urllib.request.Request(base + path, data=data, method=method, headers=h), timeout=20)
    except urllib.error.HTTPError as e:
        r = e
    return r.status if hasattr(r, "status") else r.code, dict(r.headers), r.read().decode(errors="replace")


def main():
    import agent2
    import webauth
    agent2.REQUIRE_LOGIN = True
    agent2.ask = lambda q: {"answer": "stub:" + q, "tools_used": []}      # no LLM calls in tests
    srv = ThreadingHTTPServer(("127.0.0.1", 0), agent2.Handler)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---- 1. fail closed when not configured --------------------------------------------------
    setup(port, configured=False)
    c, _, _ = req(base, "/"); check("unconfigured: / is closed", c == 503, c)
    c, _, _ = req(base, "/ask", "POST", {"question": "x"}); check("unconfigured: /ask is closed", c == 503, c)
    c, _, _ = req(base, "/login"); check("unconfigured: /login is closed", c == 503, c)

    # ---- 2. no session -------------------------------------------------------------------------
    setup(port)
    c, h, _ = req(base, "/"); check("no session: / redirects to /login", c == 302 and h.get("Location") == "/login", (c, h.get("Location")))
    c, _, _ = req(base, "/ask", "POST", {"question": "x"}); check("no session: /ask is 401", c == 401, c)
    c, h, _ = req(base, "/random"); check("no session: unknown path redirects, leaks nothing", c == 302, c)
    c, h, b = req(base, "/login")
    loc = h.get("Location", "")
    check("login goes to Google", c == 302 and loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?"), loc[:60])
    check("login sends state, nonce, our client_id, openid email",
          all(x in loc for x in ("state=", "nonce=", "client_id=cid.apps.googleusercontent.com", "scope=openid+email")))
    ck = h.get("Set-Cookie", "")
    check("state cookie is HttpOnly + Secure + SameSite", all(x in ck for x in ("HttpOnly", "Secure", "SameSite=Lax")), ck[:80])

    # ---- 3. session cookie attacks ----------------------------------------------------------------
    key = KEY.encode()
    good = webauth.sign({"e": "member2@example.edu"}, 600, key)
    sess = lambda t: {"Cookie": f"nba_session={t}"}  # noqa: E731
    c, _, b = req(base, "/", headers=sess(good)); check("valid allowlisted session: 200", c == 200 and "member2@example.edu" in b, c)
    forged = webauth.sign({"e": "member2@example.edu"}, 600, b"x" * 48)
    c, _, _ = req(base, "/", headers=sess(forged)); check("cookie signed with another key is rejected", c == 302, c)
    raw, sig = good.split(".")
    c, _, _ = req(base, "/", headers=sess(raw + "." + sig[:-2] + "AA")); check("tampered signature is rejected", c == 302, c)
    other = webauth.sign({"e": "attacker@example.com"}, 600, key).split(".")[0]
    c, _, _ = req(base, "/", headers=sess(other + "." + sig)); check("swapping the payload keeps the old signature: rejected", c == 302, c)
    c, _, _ = req(base, "/", headers=sess(webauth.sign({"e": "attacker@example.com"}, 600, key))); check("validly signed but NOT allowlisted: rejected", c == 302, c)
    c, _, _ = req(base, "/", headers=sess(webauth.sign({"e": "member2@example.edu"}, -5, key))); check("expired session is rejected", c == 302, c)
    c, _, _ = req(base, "/", headers=sess("garbage")); check("garbage cookie is rejected", c == 302, c)
    c, _, _ = req(base, "/", headers=sess("")); check("empty cookie is rejected", c == 302, c)
    c, _, _ = req(base, "/", headers=sess("a.b.c")); check("malformed cookie is rejected", c == 302, c)

    # ---- 4. /ask protections (CSRF, size, rate) -------------------------------------------------------
    ok = {"Cookie": f"nba_session={good}"}
    c, _, b = req(base, "/ask", "POST", {"question": "hi"}, ok); check("valid session can ask", c == 200 and "stub:hi" in b, (c, b[:60]))
    c, _, _ = req(base, "/ask", "POST", b"question=hi", {**ok, "Content-Type": "application/x-www-form-urlencoded"}); check("form post (CSRF style) rejected", c == 403, c)
    c, _, _ = req(base, "/ask", "POST", {"question": "hi"}, {**ok, "Origin": "https://evil.example"}); check("foreign Origin rejected", c == 403, c)
    c, _, _ = req(base, "/ask", "POST", {"question": "hi"}, {**ok, "Origin": base}); check("our own Origin accepted", c == 200, c)
    c, _, _ = req(base, "/ask", "POST", b"{" + b" " * 9000 + b"}", ok); check("oversized body rejected", c == 413, c)
    c, _, b = req(base, "/ask", "POST", {"question": "x" * 5000}, ok); check("long question is truncated, not rejected", c == 200 and len(b) < 2000, c)
    c, _, _ = req(base, "/ask", "POST", {"question": " "}, ok); check("empty question is a 400", c == 400, c)
    codes = [req(base, "/ask", "POST", {"question": "r"}, ok)[0] for _ in range(25)]
    check("rate limit kicks in (429) after 20 requests", 429 in codes, codes[-6:])
    other_user = webauth.sign({"e": "member1@example.edu"}, 600, key)
    c, _, _ = req(base, "/ask", "POST", {"question": "r"}, {"Cookie": f"nba_session={other_user}"}); check("rate limit is per user", c == 200, c)
    c, h, _ = req(base, "/", headers=ok)
    check("security headers present", h.get("X-Frame-Options") == "DENY" and h.get("X-Content-Type-Options") == "nosniff" and "Content-Security-Policy" in h)
    check("responses are not cacheable", h.get("Cache-Control") == "no-store")

    # ---- 5. the Google callback (stubbed) --------------------------------------------------------------
    st = webauth.sign({"s": "STATE", "n": "NONCE"}, 600, key)
    cb = lambda state="STATE", cookie=st: webauth.finish_login(  # noqa: E731
        {"state": state, "code": "c"}, {"Cookie": f"nba_oauth={cookie}"} if cookie else {})
    good_info = {"aud": "cid.apps.googleusercontent.com", "iss": "https://accounts.google.com", "email_verified": "true",
                 "nonce": "NONCE", "email": "Member2@Example.edu"}
    webauth._post_form = lambda url, data: {"id_token": "t"}
    for name, info, expect in [
        ("good identity (case-insensitive email)", good_info, True),
        ("wrong audience", {**good_info, "aud": "someone-elses-app"}, False),
        ("wrong issuer", {**good_info, "iss": "https://evil.example"}, False),
        ("unverified email", {**good_info, "email_verified": "false"}, False),
        ("wrong nonce", {**good_info, "nonce": "OTHER"}, False),
        ("email not on allowlist", {**good_info, "email": "attacker@example.com"}, False),
        ("allowlisted name, different domain", {**good_info, "email": "member2@example.edu.evil.com"}, False),
        ("missing email", {k: v for k, v in good_info.items() if k != "email"}, False)]:
        webauth._tokeninfo = lambda t, i=info: i
        ok_, msg, cookies = cb()
        check("callback: " + name, ok_ is expect, (ok_, msg))
        if expect:
            check("callback issues a session cookie", any(c.startswith("nba_session=") and "HttpOnly" in c for c in cookies))
    webauth._tokeninfo = lambda t: good_info
    check("callback: state mismatch rejected", cb(state="WRONG")[0] is False)
    check("callback: missing state cookie rejected", cb(cookie=None)[0] is False)
    check("callback: expired state cookie rejected", cb(cookie=webauth.sign({"s": "STATE", "n": "NONCE"}, -5, key))[0] is False)
    check("callback: state cookie signed with another key rejected", cb(cookie=webauth.sign({"s": "STATE", "n": "NONCE"}, 600, b"x" * 48))[0] is False)
    check("callback: Google error rejected", webauth.finish_login({"state": "STATE", "error": "access_denied"}, {"Cookie": f"nba_oauth={st}"})[0] is False)
    def boom(*a, **k):
        raise RuntimeError("network")
    webauth._post_form = boom
    check("callback: token exchange failure rejected", cb()[0] is False)

    # ---- 6. logs never contain query strings (one-time codes) ------------------------------------------
    buf = io.StringIO()
    with redirect_stdout(buf):
        agent2.Handler.log_message(type("H", (), {"command": "GET", "path": "/oauth2callback?code=SECRETCODE&state=abc"})(), "x", "GET", "302")
    check("request log omits the query string", "SECRETCODE" not in buf.getvalue() and "oauth2callback" in buf.getvalue(), buf.getvalue())

    srv.shutdown()
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
