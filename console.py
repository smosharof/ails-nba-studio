#!/usr/bin/env python3
"""Agent 1 Console: a small web UI to demo the workflow (private; reach it through the Cloud Run proxy).

  * shows data freshness and what is published
  * applies reversible demo changes to the source data (options A, B, C, E) and resets them
  * previews what the next run will do (both gates, dry run, nothing published)
  * runs Agent 1 in 'stepper' mode (pauses at the gates) or 'auto' mode, with a live trace

Standard-library HTTP server; the work is done by workflow.py and demo.py.
Local:  python3 -I console.py     (then open http://localhost:8081; listens on 127.0.0.1 only)
"""
import html
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent2  # noqa: E402  (the chat: fixed, validated, read-only queries)
import common  # noqa: E402
import demo  # noqa: E402
import webauth  # noqa: E402
import workflow  # noqa: E402

RUNS, TASKS = {}, {}
_lock = threading.Lock()


def _busy(allow_when_waiting=False):
    """Why a new action cannot start right now (None if it can)."""
    for t in TASKS.values():
        if t["status"] == "running":
            return f"'{t['kind']}' is still running"
    for r in RUNS.values():
        if r.status == "running":
            return "a workflow run is in progress"
        if r.status == "waiting" and not allow_when_waiting:
            return "a workflow run is paused at a gate (continue or abort it first)"
    return None


def _task(kind, fn):
    tid = uuid.uuid4().hex[:8]
    TASKS[tid] = {"id": tid, "kind": kind, "status": "running", "summary": None, "error": None, "t0": time.time()}

    def go():
        try:
            TASKS[tid]["summary"] = fn()
            TASKS[tid]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            TASKS[tid].update(status="failed", error=str(e)[:400])
        TASKS[tid]["seconds"] = round(time.time() - TASKS[tid]["t0"], 1)
    threading.Thread(target=go, daemon=True).start()
    return tid


# ------------------------------------------------------------------------------------------ handlers
def _active_run():
    """Id of the run that is running or paused right now (None if there is none), so a reloaded page can re-attach."""
    for rid, r in RUNS.items():
        if r.status in ("running", "waiting"):
            return rid
    return None


def api_state(user=None):
    st = workflow.get_state()
    # May this person change things? (The server enforces it on every change-making route; this only lets the page
    # show or hide the Admin tab.)
    operator = (not REQUIRE_LOGIN) or webauth.is_operator(user)
    return {"state": st, "published": workflow.snapshot(), "demo": demo.active_changes(),
            "busy": _busy(), "active_run": _active_run(), "recs": workflow.recs_freshness(), "rules": {"min_new_weeks": workflow.MIN_NEW_WEEKS}, "operator": bool(operator)}


def api_inject(body):
    opt = body.get("option")
    if opt in ("A", "B", "C"):
        why = _busy(allow_when_waiting=True)
        if why:
            return 409, {"error": why}
        if opt == "A":
            return 200, demo.inject_frequency_cap(body.get("hcp_id"), body.get("calls", 3))
        if opt == "B":
            return 200, demo.inject_consent_withdrawal(body.get("hcp_id"))
        return 200, demo.inject_unsafe_claim(body.get("channel"))
    if opt == "E":
        why = _busy()
        if why:
            return 409, {"error": why}
        wb = int(body.get("weeks_back", 4))
        return 202, {"task": _task("rewind the model", lambda: demo.rewind_model(wb))}
    return 400, {"error": "unknown option"}


# When set, every route needs a signed-in, allowlisted Google account (see webauth.py). Without it the console is only
# safe on loopback or behind Cloud Run IAM.
REQUIRE_LOGIN = os.environ.get("NBA_REQUIRE_LOGIN") == "1"
MAX_BODY = 8 * 1024
MUTATE_LIMIT, MUTATE_WINDOW = 60, 600       # change-making requests per user per 10 minutes
ASK_LIMIT, ASK_WINDOW = 20, 600             # chat questions per user per 10 minutes (each can cost several Gemini calls)
MAX_QUESTION = 1000
TABLE_LIMIT, TABLE_WINDOW = 60, 600          # table previews per user per 10 minutes


def audit(user, action, detail):
    """Who did what, in nba.run_log (agent='console'). Never blocks or breaks a request."""
    def go():
        try:
            common.log_run("console", "console", action, "ok", rationale=str(user)[:200],
                           inputs=json.dumps(detail, default=str)[:500])
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # never log query strings (one-time sign-in codes)
        print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)

    def _send(self, code, obj, ctype="application/json", cookies=(), location=None):
        data = obj.encode() if isinstance(obj, str) else json.dumps(obj, default=str).encode()
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

    def _user(self, path, query):
        """The signed-in user ('local' when login is off), or None after the response has been sent."""
        if not REQUIRE_LOGIN:
            return "local"
        return webauth.gate(path, query, self.headers, self._send)

    def _mutating_ok(self, user):
        """Extra checks for requests that change things. Returns False after sending the error."""
        if REQUIRE_LOGIN:
            if not webauth.origin_ok(self.headers):
                self._send(403, {"error": "bad origin or content type"})
                return False
            if not webauth.is_operator(user):
                self._send(403, {"error": "your account can view the console but not change anything"})
                return False
            if not webauth.rate_ok(user, "mutate", MUTATE_LIMIT, MUTATE_WINDOW):
                self._send(429, {"error": "too many actions, please wait a few minutes"})
                return False
        return True

    def _ask_ok(self, user):
        """Chat is open to every signed-in user (it only reads), but still needs same-origin JSON and a rate limit."""
        if REQUIRE_LOGIN:
            if not webauth.origin_ok(self.headers):
                self._send(403, {"error": "bad origin or content type"})
                return False
            if not webauth.rate_ok(user, "ask", ASK_LIMIT, ASK_WINDOW):
                self._send(429, {"error": "too many questions, please wait a few minutes"})
                return False
        return True

    def _body(self):
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("JSON required")
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_BODY:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        import urllib.parse
        p, _, qs = self.path.partition("?")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        user = self._user(p, query)
        if user is None:
            return
        try:
            if p == "/":
                who = "local use" if user == "local" else f"{html.escape(user)} &middot; <a href='/logout'>Sign out</a>"
                return self._send(200, PAGE.replace("__WHO__", who), "text/html; charset=utf-8")
            if p == "/api/state":
                return self._send(200, api_state(user))
            if p == "/api/options":
                return self._send(200, demo.options())
            if p == "/api/datasets":
                return self._send(200, workflow.datasets_status())
            if p == "/api/table":
                if REQUIRE_LOGIN and not webauth.rate_ok(user, "table", TABLE_LIMIT, TABLE_WINDOW):
                    return self._send(429, {"error": "too many previews, please wait a few minutes"})
                try:
                    return self._send(200, workflow.table_preview(query.get("name")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if p.startswith("/api/run/"):
                r = RUNS.get(p.rsplit("/", 1)[1])
                return self._send(200, r.view()) if r else self._send(404, {"error": "unknown run"})
            if p.startswith("/api/task/"):
                t = TASKS.get(p.rsplit("/", 1)[1])
                return self._send(200, t) if t else self._send(404, {"error": "unknown task"})
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)[:300]})

    def do_POST(self):
        p = self.path.split("?")[0]
        user = self._user(p, {})
        if user is None:
            return
        if p == "/api/ask":
            if not self._ask_ok(user):
                return
            try:
                q = str(self._body().get("question") or "").strip()[:MAX_QUESTION]
                if not q:
                    return self._send(400, {"error": "question required"})
                audit(user, "ask", {"question": q[:200]})
                res = agent2.ask(q)                        # not under the workflow lock: asking never blocks a run
                rv = res.get("review") or {}
                if rv.get("blocked"):
                    audit(user, "ask_blocked", {"reason": rv.get("reason"), "ids": rv.get("unknown_ids"), "issues": rv.get("issues")})
                return self._send(200, res)
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e)[:300]})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)[:300]})
        if not self._mutating_ok(user):
            return
        try:
            body = self._body()
            with _lock:
                if p == "/api/inject":
                    audit(user, "inject", body)
                    code, obj = api_inject(body)
                    return self._send(code, obj)
                if p == "/api/reset":
                    why = _busy()
                    if why:
                        return self._send(409, {"error": why})
                    audit(user, "reset", {})
                    return self._send(202, {"task": _task("reset to production", demo.reset)})
                if p == "/api/preview":
                    why = _busy(allow_when_waiting=True)
                    if why:
                        return self._send(409, {"error": why})
                    audit(user, "preview", {})
                    return self._send(200, workflow.preview())
                if p == "/api/run":
                    why = _busy()
                    if why:
                        return self._send(409, {"error": why})
                    mode = body.get("mode", "stepper")
                    if mode not in ("stepper", "auto"):
                        return self._send(400, {"error": "mode must be stepper or auto"})
                    run = workflow.Run(mode, bool(body.get("narrate", True)))
                    RUNS[run.id] = run
                    audit(user, "run", {"mode": mode, "run_id": run.id})
                    threading.Thread(target=workflow.execute, args=(run,), daemon=True).start()
                    return self._send(202, {"run_id": run.id})
                if p.startswith("/api/run/") and p.endswith("/control"):
                    run = RUNS.get(p.split("/")[3])
                    if not run:
                        return self._send(404, {"error": "unknown run"})
                    audit(user, "control", {"run_id": run.id, "action": body.get("action")})
                    ok = run.control(body.get("action"))
                    return self._send(200 if ok else 409, {"ok": ok})
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            self._send(400, {"error": str(e)[:300]})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)[:300]})


# ------------------------------------------------------------------------------------------------ page
PAGE = (Path(__file__).resolve().parent / "console_page.html").read_text()

if __name__ == "__main__":
    demo.ensure_tables()
    port = int(os.environ.get("PORT", "8081"))
    # SECURITY: the console has no login and can change data using the caller's credentials, so it listens on
    # loopback only. The deployed container sets NBA_BIND=0.0.0.0 because Cloud Run IAM protects it there.
    bind = os.environ.get("NBA_BIND", "127.0.0.1")
    print(f"Agent 1 Console on http://{bind}:{port}", flush=True)
    ThreadingHTTPServer((bind, port), H).serve_forever()
