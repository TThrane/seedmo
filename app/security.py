"""seedmo – to-trins-login (TOTP) og bremse mod gætteri."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import struct
import threading
import time
from pathlib import Path
from urllib.parse import quote

import segno

ISSUER = "seedmo"
STEP = 30
DIGITS = 6


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(code).zfill(DIGITS)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at or time.time()) // STEP))


def totp_verify(secret_b32: str, code: str, window: int = 1) -> int | None:
    """Returnerer tidstrinnet for en gyldig kode (så den ikke kan genbruges), ellers None."""
    code = "".join(c for c in str(code) if c.isdigit())
    if len(code) != DIGITS:
        return None
    now = int(time.time() // STEP)
    for d in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, now + d), code):
            return now + d
    return None


class TwoFactor:
    """Gemmer TOTP-hemmeligheden i /config/totp.json.

    Slettes filen (fx i File Station), er to-trins-login slået fra igen.
    """

    def __init__(self, config_dir: Path):
        self.file = Path(config_dir) / "totp.json"
        self.lock = threading.Lock()
        self.pending: str | None = None
        self.last_step = -1
        self.data = {}
        if self.file.exists():
            try:
                self.data = json.loads(self.file.read_text())
            except Exception:
                self.data = {}

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled") and self.data.get("secret"))

    def _save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def start_setup(self, label: str = "seedmo") -> dict:
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        self.pending = secret
        uri = f"otpauth://totp/{quote(ISSUER)}:{quote(label)}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={STEP}"
        buf = io.BytesIO()
        segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2, dark="#000", light="#fff", xmldecl=False, svgns=True)
        return {"secret": secret, "uri": uri, "svg": buf.getvalue().decode()}

    def confirm_setup(self, code: str) -> bool:
        with self.lock:
            step = totp_verify(self.pending, code) if self.pending else None
            if step is None:
                return False
            self.last_step = step
            self.data = {"enabled": True, "secret": self.pending, "since": int(time.time())}
            self.pending = None
            self._save()
            return True

    def verify(self, code: str) -> bool:
        if not self.enabled:
            return True
        with self.lock:
            step = totp_verify(self.data["secret"], code)
            if step is None or step <= self.last_step:
                return False
            self.last_step = step  # samme kode kan ikke bruges to gange
            return True

    def disable(self, code: str) -> bool:
        if not self.verify(code):
            return False
        with self.lock:
            self.data = {}
            self.file.unlink(missing_ok=True)
        return True


class Throttle:
    """Låser en IP-adresse i 15 minutter efter 5 forkerte login-forsøg."""

    def __init__(self, max_fails: int = 5, lock_seconds: int = 900):
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def locked_for(self, ip: str) -> int:
        with self.lock:
            now = time.time()
            recent = [t for t in self.fails.get(ip, []) if now - t < self.lock_seconds]
            self.fails[ip] = recent
            if len(recent) >= self.max_fails:
                return int(self.lock_seconds - (now - recent[-self.max_fails])) + 1
            return 0

    def fail(self, ip: str):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def ok(self, ip: str):
        with self.lock:
            self.fails.pop(ip, None)
