"""Web panel (FastAPI) and phone app (PWA).

Security:
  * binds 127.0.0.1 by default; refuses any other bind address unless PANEL_PASSWORD is set
    (enforced in app.py before the server starts)
  * when PANEL_PASSWORD is set, every page and API call needs a login session cookie
    (HttpOnly, SameSite=Strict, signed with HMAC); login is rate-limited per client IP
  * Host header must be an allowed host (blocks DNS-rebinding attacks on the local panel)
  * every POST needs the X-Memebot header (a custom header cannot be sent cross-site without
    CORS, which is never enabled), and a matching Origin when the browser sends one
  * no endpoint changes mode or settings
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

STATIC = Path(__file__).parent / "static"
COOKIE = "memebot_session"
SESSION_TTL = 7 * 24 * 3600


class LoginLimiter:
    def __init__(self, max_tries: int = 5, window: float = 300.0):
        self.max_tries, self.window = max_tries, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allowed(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits[ip]
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) < self.max_tries

    def fail(self, ip: str, now: float | None = None) -> None:
        self.hits[ip].append(time.monotonic() if now is None else now)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def make_token(secret: bytes, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_TTL)
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    return f"{payload}.{_sign(secret, payload)}"


def check_token(secret: bytes, token: str | None, now: float | None = None) -> bool:
    if not token or token.count(".") != 2:
        return False
    exp, nonce, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(secret, f"{exp}.{nonce}")):
        return False
    try:
        return int(exp) > (now or time.time())
    except ValueError:
        return False


def create_app(engines: list, *, password: str, bind_host: str, port: int, extra_hosts: tuple[str, ...] = (),
               secret: bytes | None = None) -> FastAPI:
    app = FastAPI(title="memebot", docs_url=None, redoc_url=None, openapi_url=None)
    secret = secret or secrets.token_bytes(32)
    limiter = LoginLimiter()
    allowed = {"localhost", "127.0.0.1", "::1", "[::1]", bind_host.lower(), *extra_hosts}
    auth_on = bool(password)

    def host_ok(request: Request) -> bool:
        h = (request.headers.get("host") or "").lower()
        if h.startswith("["):
            name = h.split("]")[0] + "]"
        else:
            name = h.rsplit(":", 1)[0] if ":" in h else h
        return name in allowed or (bind_host in ("0.0.0.0", "::") and not extra_hosts and _is_ip(name))

    def authed(request: Request) -> bool:
        return (not auth_on) or check_token(secret, request.cookies.get(COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not host_ok(request):
            return JSONResponse({"error": "host not allowed"}, status_code=421)
        if request.method == "POST":
            if request.headers.get("x-memebot") != "1":
                return JSONResponse({"error": "missing X-Memebot header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1].lower() != (request.headers.get("host") or "").lower():
                return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        public = request.url.path in ("/login", "/api/login", "/manifest.webmanifest", "/sw.js",
                                      "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png", "/app.css")
        if not public and not authed(request):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "login required"}, status_code=401)
            return Response(status_code=303, headers={"Location": "/login"})
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                   "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def static(name: str, media: str | None = None):
        return FileResponse(STATIC / name, media_type=media)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return static("index.html", "text/html")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        if not auth_on:
            return Response(status_code=303, headers={"Location": "/"})
        return static("login.html", "text/html")

    for fname, media in (("app.js", "text/javascript"), ("app.css", "text/css"), ("sw.js", "text/javascript"),
                         ("manifest.webmanifest", "application/manifest+json"), ("icon-192.png", "image/png"),
                         ("icon-512.png", "image/png"), ("apple-touch-icon.png", "image/png"),
                         ("login.js", "text/javascript")):
        app.add_api_route(f"/{fname}", (lambda f=fname, m=media: static(f, m)), methods=["GET"])

    @app.post("/api/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        if not auth_on:
            return {"ok": True}
        if not limiter.allowed(ip):
            raise HTTPException(429, "Too many attempts. Wait 5 minutes.")
        try:
            body = await request.json()
            pw = str(body.get("password", ""))
        except (json.JSONDecodeError, AttributeError, ValueError):
            pw = ""
        if not hmac.compare_digest(pw.encode(), password.encode()):
            limiter.fail(ip)
            raise HTTPException(401, "Wrong password.")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, make_token(secret), max_age=SESSION_TTL, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    live = next((e for e in engines if e.mode == "LIVE"), None)
    paperish = [e for e in engines if e.mode != "LIVE"]

    def pick(label: str):
        for e in engines:
            if e.label == label:
                return e
        raise HTTPException(404, "unknown engine")

    @app.get("/api/status")
    async def status():
        return {"engines": [e.status() for e in engines], "live_armed": live is not None}

    @app.get("/api/trades")
    async def trades(engine: str = "PAPER", limit: int = 25):
        return pick(engine).recent_trades(max(1, min(limit, 200)))

    @app.get("/api/settings")
    async def settings():
        cfg = engines[0].cfg
        return {"sha256": cfg.sha256, "settings": cfg.raw,
                "effective": {k: v for k, v in cfg.values.items() if not k.startswith("_")}}

    @app.get("/api/alerts")
    async def alerts():
        lines = [ln for e in engines for ln in e.notify.history[-30:]]
        return sorted(lines, reverse=True)[:40]

    @app.post("/api/control")
    async def control(request: Request):
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(400, "bad json") from None
        action = str(body.get("action", ""))
        target = str(body.get("target", "PAPER"))
        if action in ("approve", "reject"):
            if not live:
                raise HTTPException(400, "real trading is not armed")
            return {"message": live.decide_approval(str(body.get("id", "")), action == "approve", "panel")}
        if action == "sell":
            return {"message": pick(target).request_force_sell(str(body.get("address", "")), "panel")}
        if action in ("pause", "resume", "sellall"):
            group = engines
        elif target == "REAL":
            if not live:
                raise HTTPException(400, "real trading is not armed on the computer")
            group = [live]
        else:
            group = paperish
        fn = {"start": lambda e: e.set_entries(True, "panel"), "stop": lambda e: e.set_entries(False, "panel"),
              "pause": lambda e: e.set_paused(True, "panel"), "resume": lambda e: e.set_paused(False, "panel"),
              "sellall": lambda e: e.request_sell_all("panel")}.get(action)
        if not fn:
            raise HTTPException(400, "unknown action")
        return {"message": "\n".join(f"{e.label}: {fn(e)}" for e in group)}

    return app


def _is_ip(name: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        return False
