"""Auth primitives: scrypt password hashes, signed session tokens, login rate limiting.

Standard library only. No `$` characters appear in hashes so they are safe in .env/compose.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from pathlib import Path

log = logging.getLogger("auth")

N, R, P = 2 ** 14, 8, 1
COOKIE = "manifest_session"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "manifest"
MIN_PASSWORD = 10
_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- usernames and password policy -------------------------------------------------

def normalize_username(name: str) -> str:
    return (name or "").strip().lower()


def validate_username(name: str) -> str:
    n = normalize_username(name)
    if not _USERNAME_RE.match(n):
        raise ValueError("Username must be 3-32 characters: letters, numbers, dot, dash or underscore")
    return n


def validate_new_password(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    if password.lower() == DEFAULT_PASSWORD or password.lower() == normalize_username(username):
        raise ValueError("Choose a password that is not the default or the username")


def is_local_ip(ip: str) -> bool:
    """Private, loopback, link-local or CGNAT (e.g. Tailscale) address."""
    try:
        return not ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


# ---- password hashing --------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt:{N}:{R}:{P}:{_b64(salt)}:{_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split(":")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        expected = _unb64(dk)
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=n, r=r, p=p,
                             dklen=len(expected), maxmem=256 * n * r)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def fingerprint(stored_hash: str) -> str:
    """Ties a session to the user's current password: changing it signs the user out everywhere."""
    return _b64(hashlib.sha256(stored_hash.encode()).digest()[:12])


# ---- sessions ----------------------------------------------------------------------

class Sessions:
    def __init__(self, secret: bytes, session_seconds: int = 7 * 86400):
        self.secret = secret
        self.session_seconds = session_seconds

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self.secret, payload, hashlib.sha256).digest())

    def issue(self, username: str, fp: str, now: float | None = None,
              purpose: str = "session", seconds: int | None = None) -> str:
        exp = int((now if now is not None else time.time()) + (seconds or self.session_seconds))
        payload = f"{exp}.{_b64(username.encode())}.{fp}.{purpose}".encode()
        return f"{_b64(payload)}.{self._sign(payload)}"

    def parse(self, token: str | None, now: float | None = None,
              purpose: str = "session") -> tuple[str, str] | None:
        """Return (username, fingerprint) for a genuine, unexpired token of this purpose, else None.
        The purpose stops a short-lived 2FA challenge from being used as a login session."""
        if not token or token.count(".") != 1:
            return None
        try:
            body, sig = token.split(".")
            payload = _unb64(body)
            if not hmac.compare_digest(sig, self._sign(payload)):
                return None
            exp, user, fp, kind = payload.decode().split(".", 3)
            if kind != purpose or int(exp) <= (now if now is not None else time.time()):
                return None
            return _unb64(user).decode(), fp
        except (ValueError, UnicodeDecodeError):
            return None


def load_secret() -> bytes:
    env = os.getenv("MANIFEST_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    path = Path(os.getenv("SECRET_FILE", "/data/secret.key"))
    try:
        return _unb64(path.read_text().strip())
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_b64(key))
        os.chmod(path, 0o600)
    except OSError:
        log.warning("Could not persist the session key; logins will reset when the app restarts")
    return key


class LoginLimiter:
    """Blocks a key (address) after `max_fail` failures within `window` seconds."""

    def __init__(self, max_fail: int = 5, window: int = 900):
        self.max_fail, self.window = max_fail, window
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(key, []) if now - t < self.window]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def blocked_for(self, key: str, now: float | None = None) -> int:
        """Seconds until another attempt is allowed (0 = allowed)."""
        now = now if now is not None else time.time()
        recent = self._recent(key, now)
        if len(recent) < self.max_fail:
            return 0
        return max(1, int(recent[0] + self.window - now))

    def fail(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if len(self._fails) > 10_000:  # bound memory under a spray from many addresses
            self._fails.clear()
        self._fails.setdefault(key, []).append(now)

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
