"""Sign in with Google for the Agent 2 web service (standard library only).

Why this exists: Cloud Run's built-in IAP could not be completed for this project (no organization), and
Agent 2 must be reachable from a browser by a few named people. So the service is made reachable from the
internet and THIS module is the gate. It is deliberately small and strict:

  * Authorization-code flow with Google ("openid email"), with a signed one-time `state` cookie and a `nonce`.
  * The ID token is validated by Google's tokeninfo endpoint (signature/expiry), and we also check the audience
    (our client ID), the issuer, `email_verified`, the `nonce`, and that the lower-cased email is on the ALLOWLIST.
  * The session is an HMAC-SHA256-signed cookie (HttpOnly, Secure, SameSite=Lax) with a short expiry.
  * Every route except /login, /oauth2callback and /logout needs a valid session. No session: pages redirect
    to /login, API calls get 401.
  * State-changing requests must be JSON, and the Origin header (when sent) must be our own origin (CSRF).
  * A per-user rate limit protects the Gemini bill.

Also, while the Google OAuth app is in 'Testing' mode, Google only issues tokens to its listed test users:
a second, independent gate.

Config (environment): OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, SESSION_KEY, ALLOWED_EMAILS (comma list),
BASE_URL (e.g. https://nba-agent2-....run.app).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

SESSION_TTL_S = 2 * 3600
STATE_TTL_S = 10 * 60
RATE_LIMIT = 20          # requests ...
RATE_WINDOW_S = 600      # ... per 10 minutes per user
_hits = {}


def _cfg():
    return {
        "client_id": os.environ.get("OAUTH_CLIENT_ID", ""),
        "client_secret": os.environ.get("OAUTH_CLIENT_SECRET", ""),
        "key": os.environ.get("SESSION_KEY", "").encode(),
        "allowed": {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()},
        "base": os.environ.get("BASE_URL", "").rstrip("/"),
        # People who may CHANGE things (console actions). Default: everyone on the allowlist.
        "operators": ({e.strip().lower() for e in os.environ.get("OPERATOR_EMAILS", "").split(",") if e.strip()}
                      or {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}),
        # A session cookie is only valid for the app that issued it (so an Agent 2 cookie never works on the console).
        "aud": os.environ.get("SESSION_AUD", "default"),
        "ttl": int(os.environ.get("SESSION_TTL_S", SESSION_TTL_S)),
    }


def configured():
    c = _cfg()
    return bool(c["client_id"] and c["client_secret"] and len(c["key"]) >= 32 and c["allowed"] and c["base"])


# --------------------------------------------------------------------------------- signed cookies
def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict, ttl: int, key: bytes) -> str:
    body = dict(payload, exp=int(time.time()) + ttl)
    raw = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, raw.encode(), hashlib.sha256).digest())
    return f"{raw}.{sig}"


def verify(token: str, key: bytes):
    """Return the payload if the signature is valid and it has not expired, else None."""
    try:
        raw, sig = token.split(".", 1)
        good = _b64(hmac.new(key, raw.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        body = json.loads(_unb64(raw))
        return body if int(body.get("exp", 0)) > time.time() else None
    except Exception:
        return None


def cookie_header(name, value, max_age):
    return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"


def get_cookie(headers, name):
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


# ------------------------------------------------------------------------------------ the login flow
def login_redirect():
    """Return (location_url, set_cookie_header). Stores a signed one-time state+nonce in a short-lived cookie."""
    c = _cfg()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    q = urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": c["base"] + "/oauth2callback", "response_type": "code",
        "scope": "openid email", "state": state, "nonce": nonce, "prompt": "select_account"})
    cookie = cookie_header("nba_oauth", sign({"s": state, "n": nonce}, STATE_TTL_S, c["key"]), STATE_TTL_S)
    return "https://accounts.google.com/o/oauth2/v2/auth?" + q, cookie


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req, timeout=15))


def _tokeninfo(id_token):
    return json.load(urllib.request.urlopen(
        "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(id_token), timeout=15))


def finish_login(query: dict, headers):
    """Handle /oauth2callback. Returns (ok, message, set_cookie_headers)."""
    c = _cfg()
    st = verify(get_cookie(headers, "nba_oauth") or "", c["key"])
    clear = [cookie_header("nba_oauth", "", 0)]
    if not st or not hmac.compare_digest(str(query.get("state", "")), st["s"]):
        return False, "Login expired or invalid. Please try again.", clear
    if query.get("error") or not query.get("code"):
        return False, "Sign-in was cancelled or refused.", clear
    try:
        tok = _post_form("https://oauth2.googleapis.com/token", {
            "code": query["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
            "redirect_uri": c["base"] + "/oauth2callback", "grant_type": "authorization_code"})
        info = _tokeninfo(tok["id_token"])          # Google verifies signature and expiry
    except Exception:
        return False, "Could not complete sign-in.", clear
    email = str(info.get("email", "")).strip().lower()
    ok = (info.get("aud") == c["client_id"]
          and info.get("iss") in ("accounts.google.com", "https://accounts.google.com")
          and str(info.get("email_verified")).lower() == "true"
          and hmac.compare_digest(str(info.get("nonce", "")), st["n"])
          and email in c["allowed"])
    if not ok:
        return False, "This account is not allowed to use this app.", clear
    return True, email, clear + [cookie_header("nba_session", sign({"e": email, "a": c["aud"]}, c["ttl"], c["key"]), c["ttl"])]


def current_user(headers):
    """The signed-in, allowlisted email, or None."""
    c = _cfg()
    s = verify(get_cookie(headers, "nba_session") or "", c["key"])
    email = (s or {}).get("e", "")
    if (s or {}).get("a", "default") != c["aud"]:
        return None
    return email if email and email in c["allowed"] else None


def is_operator(email):
    """May this signed-in user change things (inject data, reset, run)?"""
    return bool(email) and email in _cfg()["operators"]


def origin_ok(headers):
    """CSRF guard for state-changing requests: JSON only, and Origin (if present) must be our own."""
    if "application/json" not in (headers.get("Content-Type") or ""):
        return False
    origin = headers.get("Origin")
    return origin is None or origin.rstrip("/") == _cfg()["base"]


def rate_ok(email, bucket="default", limit=RATE_LIMIT, window=RATE_WINDOW_S):
    now = time.time()
    k = (bucket, email)
    q = [t for t in _hits.get(k, []) if now - t < window]
    if len(q) >= limit:
        _hits[k] = q
        return False
    q.append(now)
    _hits[k] = q
    return True


# ------------------------------------------------------------------------------------ a reusable gate
def gate(path, query, headers, send):
    """Run the sign-in flow and the login requirement for any handler.

    `send(code, body, ctype, cookies, location)` writes a response. Returns the signed-in, allowlisted email,
    or None after having ALREADY sent the response (503 closed, login redirect, 401, access denied page...).
    """
    import html
    if not configured():
        send(503, '{"error":"sign-in is not configured, so this service is closed"}', "application/json", [], None)
        return None
    if path == "/login":
        loc, cookie = login_redirect()
        send(302, "", "text/plain", [cookie], loc)
        return None
    if path == "/oauth2callback":
        ok, msg, cookies = finish_login(query, headers)
        if ok:
            send(302, "", "text/plain", cookies, "/")
        else:
            send(403, "<!doctype html><meta charset=utf-8><body style='font:16px system-ui;max-width:560px;margin:3rem auto'>"
                      f"<h3>Access denied</h3><p>{html.escape(msg)}</p><p><a href='/login'>Try again</a></p>",
                 "text/html; charset=utf-8", cookies, None)
        return None
    if path == "/logout":
        send(200, "<!doctype html><meta charset=utf-8><body style='font:16px system-ui;margin:3rem'>Signed out. "
                  "<a href='/login'>Sign in again</a>", "text/html; charset=utf-8", [cookie_header("nba_session", "", 0)], None)
        return None
    user = current_user(headers)
    if not user:
        if path.startswith("/api/"):
            send(401, '{"error":"sign in required"}', "application/json", [], None)
        else:
            send(302, "", "text/plain", [], "/login")
        return None
    return user
