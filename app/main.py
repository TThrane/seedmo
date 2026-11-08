"""seedmo – web-API og server."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
import asyncio
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .engine import Engine
from .security import Throttle, TwoFactor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seedmo")

CONFIG_DIR = Path(os.environ.get("SEEDMO_CONFIG", "/config"))
DOWNLOAD_DIR = Path(os.environ.get("SEEDMO_DOWNLOADS", "/downloads"))
PEER_PORT = int(os.environ.get("SEEDMO_PEER_PORT", "6881"))
PASSWORD = os.environ.get("SEEDMO_PASSWORD", "")
STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "seedmo_session"
SESSION_DAYS = 30
MAX_TORRENT_BYTES = 10 * 1024 * 1024

engine: Engine | None = None
twofa: TwoFactor | None = None
throttle = Throttle()


def _secret() -> bytes:
    f = CONFIG_DIR / "secret.key"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
        os.chmod(f, 0o600)
    return f.read_bytes()


SECRET = b""


def _make_token() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _valid_token(tok: str | None) -> bool:
    if not PASSWORD:
        return True
    if not tok or "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, SECRET, twofa
    SECRET = _secret()
    twofa = TwoFactor(CONFIG_DIR)
    engine = Engine(CONFIG_DIR, DOWNLOAD_DIR, PEER_PORT)
    if not PASSWORD:
        log.warning("SEEDMO_PASSWORD er ikke sat – web-UI'et er åbent for alle på netværket")
    yield
    engine.shutdown()


app = FastAPI(title="seedmo", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    open_paths = ("/api/login", "/api/auth")
    if (path.startswith("/api/") or path == "/add") and path not in open_paths:
        if not _valid_token(request.cookies.get(COOKIE)):
            if path == "/add":
                return RedirectResponse("/?login=1", status_code=303)
            return JSONResponse({"error": "Log ind først"}, status_code=401)
    return await call_next(request)


def _err(e: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


# ---------- login ----------
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _set_session(r: JSONResponse, request: Request):
    r.set_cookie(COOKIE, _make_token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                 secure=request.url.scheme == "https")


@app.get("/api/auth")
def auth_state(request: Request):
    return {"required": bool(PASSWORD), "ok": _valid_token(request.cookies.get(COOKIE)),
            "totp": bool(PASSWORD) and twofa.enabled}


@app.post("/api/login")
async def login(request: Request):
    ip = _client_ip(request)
    wait = throttle.locked_for(ip)
    if wait:
        return JSONResponse({"error": f"For mange forsøg. Prøv igen om {max(1, wait // 60)} min."}, status_code=429)
    data = await request.json()
    pw_ok = not PASSWORD or hmac.compare_digest(str(data.get("password", "")), PASSWORD)
    code_ok = pw_ok and twofa.verify(str(data.get("code", "")))
    if pw_ok and code_ok:
        throttle.ok(ip)
        r = JSONResponse({"ok": True})
        _set_session(r, request)
        return r
    throttle.fail(ip)
    log.warning("Forkert login fra %s", ip)
    await asyncio.sleep(1.5)  # brems gætteri
    msg = "Forkert adgangskode eller kode" if twofa.enabled else "Forkert adgangskode"
    return JSONResponse({"error": msg}, status_code=401)


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


# ---------- to-trins-login ----------
@app.get("/api/2fa")
def twofa_state():
    return {"enabled": twofa.enabled, "available": bool(PASSWORD)}


@app.post("/api/2fa/setup")
def twofa_setup():
    if not PASSWORD:
        _err(Exception("Sæt SEEDMO_PASSWORD først"))
    if twofa.enabled:
        _err(Exception("To-trins-login er allerede slået til"))
    return twofa.start_setup("seedmo")


@app.post("/api/2fa/confirm")
async def twofa_confirm(request: Request):
    data = await request.json()
    if not twofa.confirm_setup(str(data.get("code", ""))):
        _err(Exception("Koden passer ikke. Prøv den næste kode fra appen."))
    return {"ok": True}


@app.post("/api/2fa/disable")
async def twofa_disable(request: Request):
    data = await request.json()
    if not twofa.disable(str(data.get("code", ""))):
        _err(Exception("Koden passer ikke"))
    return {"ok": True}


# ---------- torrents ----------
@app.get("/api/torrents")
def list_torrents():
    s = engine.get_settings()
    return {"torrents": engine.list(), "disk": engine.disk(), "limits": {
        "down": s["download_limit_kb"], "up": s["upload_limit_kb"]}}


@app.get("/api/torrents/{tid}")
def get_torrent(tid: str):
    try:
        return engine.detail(tid)
    except KeyError:
        _err(Exception("Torrent findes ikke"), 404)


def _fetch_torrent_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "seedmo/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(MAX_TORRENT_BYTES + 1)
    if len(data) > MAX_TORRENT_BYTES:
        raise ValueError("Filen er for stor til at være en .torrent")
    return data


@app.post("/api/add")
async def add(request: Request):
    data = await request.json()
    link = str(data.get("link", "")).strip()
    folder = str(data.get("folder", ""))
    try:
        if link.startswith("magnet:"):
            tid = engine.add_magnet(link, folder)
        elif link.startswith(("http://", "https://")):
            raw = await run_in_threadpool(_fetch_torrent_url, link)
            tid = engine.add_torrent_file(raw, folder)
        else:
            raise ValueError("Indsæt et magnet-link eller et link til en .torrent-fil")
    except ValueError as e:
        _err(e)
    except Exception as e:
        _err(Exception(f"Kunne ikke tilføje: {e}"))
    return {"id": tid}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), folder: str = Form("")):
    ids, errors = [], []
    for f in files:
        raw = await f.read(MAX_TORRENT_BYTES + 1)
        try:
            if len(raw) > MAX_TORRENT_BYTES:
                raise ValueError("for stor")
            ids.append(engine.add_torrent_file(raw, folder))
        except Exception as e:
            errors.append(f"{f.filename}: {e}")
    if not ids and errors:
        _err(Exception("; ".join(errors)))
    return {"ids": ids, "errors": errors}


@app.post("/api/torrents/{tid}/files")
async def set_files(tid: str, request: Request):
    data = await request.json()
    try:
        engine.set_files(tid, {int(k): bool(v) for k, v in data.get("wanted", {}).items()})
    except KeyError:
        _err(Exception("Torrent findes ikke"), 404)
    return {"ok": True}


@app.post("/api/torrents/{tid}/pause")
def pause(tid: str):
    try:
        engine.pause(tid)
    except KeyError:
        _err(Exception("Torrent findes ikke"), 404)
    return {"ok": True}


@app.post("/api/torrents/{tid}/resume")
def resume(tid: str):
    try:
        engine.resume(tid)
    except KeyError:
        _err(Exception("Torrent findes ikke"), 404)
    return {"ok": True}


@app.delete("/api/torrents/{tid}")
def remove(tid: str, delete_files: bool = False):
    try:
        engine.remove(tid, delete_files)
    except KeyError:
        _err(Exception("Torrent findes ikke"), 404)
    return {"ok": True}


@app.get("/api/diag")
def diag():
    return engine.diag()


@app.get("/api/settings")
def get_settings():
    return engine.get_settings()


@app.post("/api/settings")
async def set_settings(request: Request):
    return engine.update_settings(await request.json())


# ---------- magnet-links fra browseren ----------
@app.get("/add")
def add_from_browser(magnet: str = ""):
    """Bruges af browserens magnet-håndtering: /add?magnet=%s"""
    try:
        tid = engine.add_magnet(magnet)
        return RedirectResponse(f"/#{tid}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/?fejl={urllib.parse.quote(str(e))}", status_code=303)


# ---------- UI ----------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
