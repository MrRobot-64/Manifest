"""TOTP (RFC 6238, SHA-1, 6 digits, 30 s) using only the standard library."""
from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from hashlib import sha1
from urllib.parse import quote

PERIOD = 30
DIGITS = 6


def new_secret() -> str:
    """160-bit secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", counter), sha1).digest()
    o = h[-1] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def current_step(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // PERIOD)


def verify(secret: str, code: str, last_step: int = 0, now: float | None = None, window: int = 1) -> int | None:
    """Return the matched time step, or None. Accepts +-`window` steps for clock drift, and never a
    step at or before `last_step`, so a code can't be used twice."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    cur = current_step(now)
    for step in range(cur - window, cur + window + 1):
        if step > last_step and hmac.compare_digest(hotp(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, username: str, issuer: str = "Manifest") -> str:
    label = quote(f"{issuer}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")
