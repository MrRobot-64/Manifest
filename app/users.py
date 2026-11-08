"""User accounts persisted to a JSON file (/data/users.json). Passwords are scrypt hashes."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import totp
from .auth import (DEFAULT_PASSWORD, DEFAULT_USERNAME, hash_password, normalize_username,
                   validate_new_password, validate_username, verify_password)

log = logging.getLogger("users")
ROLES = ("admin", "viewer")
MAX_API_KEYS = 5


def _key_hash(key: str) -> str:
    # API keys are long random strings, so a plain SHA-256 is enough (no need for a slow hash)
    return hashlib.sha256(key.encode()).hexdigest()


def _recovery_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").replace(" ", "").encode()).hexdigest()


class UserStore:
    def __init__(self, path: Path):
        self.path = path
        self.users: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._dummy = hash_password(secrets.token_hex(8))  # so unknown usernames cost the same time

    # ---- persistence ----
    def _read(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
            users = data["users"]
            if isinstance(users, dict) and users and all("hash" in u and "role" in u for u in users.values()):
                return users
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"users": self.users}))
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        except OSError:
            log.warning("Could not save users to %s; changes will be lost on restart", self.path)

    @staticmethod
    def _new(stored_hash: str, role: str, must_change: bool, default: bool = False) -> dict:
        return {"hash": stored_hash, "role": role, "must_change": must_change, "default": default,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def load_or_seed(self) -> None:
        """Load users, or create the first admin.

        If MANIFEST_PASSWORD(_HASH) is set (older installs) that password is used as-is.
        Otherwise the default login admin/manifest is created and must be changed at first login.
        """
        loaded = self._read()
        if loaded:
            self.users = loaded
            if not any(u["role"] == "admin" for u in loaded.values()):
                log.warning("users.json has no administrator account")
            return
        try:
            name = validate_username(os.getenv("MANIFEST_USERNAME", DEFAULT_USERNAME))
        except ValueError:
            name = DEFAULT_USERNAME
        stored = os.getenv("MANIFEST_PASSWORD_HASH", "").strip()
        if not stored.startswith("scrypt:"):
            stored = ""
        if not stored and os.getenv("MANIFEST_PASSWORD"):
            stored = hash_password(os.environ["MANIFEST_PASSWORD"])
        if stored:
            self.users = {name: self._new(stored, "admin", must_change=False)}
            log.info("Created admin '%s' from environment variables", name)
        else:
            self.users = {DEFAULT_USERNAME: self._new(hash_password(DEFAULT_PASSWORD), "admin",
                                                      must_change=True, default=True)}
            log.warning("Created default login %s / %s. It must be changed at first sign-in.",
                        DEFAULT_USERNAME, DEFAULT_PASSWORD)
        self._save()

    # ---- queries ----
    def get(self, name: str) -> dict | None:
        n = normalize_username(name)
        u = self.users.get(n)
        return {"username": n, **u} if u else None

    def authenticate(self, name: str, password: str) -> dict | None:
        """Blocking (scrypt); call from a thread. Always does one hash verification."""
        n = normalize_username(name)
        u = self.users.get(n)
        ok = verify_password(password, u["hash"] if u else self._dummy)
        return {"username": n, **u} if (u and ok) else None

    def public_list(self) -> list[dict]:
        return [{"username": n, "role": u["role"], "must_change": u["must_change"], "created": u["created"],
                 "totp": bool(u.get("totp_secret")), "api_keys": len(u.get("api_keys", []))}
                for n, u in sorted(self.users.items())]

    # ---- two-factor (TOTP) ----
    def has_totp(self, username: str) -> bool:
        u = self.users.get(normalize_username(username))
        return bool(u and u.get("totp_secret"))

    def totp_status(self, username: str) -> dict:
        u = self.users.get(normalize_username(username)) or {}
        return {"enabled": bool(u.get("totp_secret")), "recovery_remaining": len(u.get("recovery", []))}

    def begin_totp(self, username: str) -> str:
        """Create a pending secret. 2FA only switches on once confirm_totp() sees a valid code."""
        n = normalize_username(username)
        with self._lock:
            if n not in self.users:
                raise ValueError("No such user")
            if self.users[n].get("totp_secret"):
                raise ValueError("Two-factor authentication is already on")
            self.users[n]["totp_pending"] = secret = totp.new_secret()
            self._save()
        return secret

    def confirm_totp(self, username: str, code: str) -> list[str]:
        """Verify the first code, switch 2FA on, and return one-time recovery codes (shown only once)."""
        n = normalize_username(username)
        with self._lock:
            u = self.users.get(n)
            pending = u.get("totp_pending") if u else None
            if not pending:
                raise ValueError("Start the setup again")
            step = totp.verify(pending, code)
            if step is None:
                raise ValueError("That code is not correct. Check the code and your phone's clock, then try again.")
            codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(8)]
            u.update(totp_secret=pending, totp_pending=None, totp_last=step,
                     recovery=[_recovery_hash(c) for c in codes])
            self._save()
        return codes

    def disable_totp(self, username: str) -> None:
        n = normalize_username(username)
        with self._lock:
            u = self.users.get(n)
            if not u:
                raise ValueError("No such user")
            for k in ("totp_secret", "totp_pending", "totp_last", "recovery"):
                u.pop(k, None)
            self._save()

    def check_second_factor(self, username: str, code: str) -> bool:
        """A current authenticator code (each usable once) or an unused recovery code (consumed)."""
        n = normalize_username(username)
        code = (code or "").strip()
        with self._lock:
            u = self.users.get(n)
            if not u or not u.get("totp_secret"):
                return False
            if len(code.replace(" ", "")) == totp.DIGITS and code.replace(" ", "").isdigit():
                step = totp.verify(u["totp_secret"], code, u.get("totp_last", 0))
                if step is None:
                    return False
                u["totp_last"] = step
                self._save()
                return True
            h = _recovery_hash(code)
            for stored in u.get("recovery", []):
                if hmac.compare_digest(h, stored):
                    u["recovery"] = [r for r in u["recovery"] if r != stored]
                    self._save()
                    return True
            return False

    # ---- changes ----
    def create(self, username: str, password: str, role: str) -> None:
        n = validate_username(username)
        if role not in ROLES:
            raise ValueError("Role must be admin or viewer")
        validate_new_password(password, n)
        stored = hash_password(password)
        with self._lock:
            if n in self.users:
                raise ValueError("That username already exists")
            self.users[n] = self._new(stored, role, must_change=True)  # new users must pick their own password
            self._save()

    def set_password(self, username: str, password: str, must_change: bool) -> None:
        n = normalize_username(username)
        validate_new_password(password, n)
        stored = hash_password(password)
        with self._lock:
            if n not in self.users:
                raise ValueError("No such user")
            self.users[n].update(hash=stored, must_change=must_change, default=False)
            self._save()

    # ---- API keys (per user; read-only stats access for dashboard widgets) ----
    @staticmethod
    def _public_key(k: dict) -> dict:
        return {f: k.get(f) for f in ("id", "name", "prefix", "created", "last_used")}

    def create_api_key(self, username: str, name: str) -> dict:
        """Returns the key itself exactly once. Only a hash is stored."""
        n = normalize_username(username)
        name = (name or "").strip()
        if not 1 <= len(name) <= 40:
            raise ValueError("Give the key a name (up to 40 characters)")
        key = "mnf_" + secrets.token_urlsafe(24)
        entry = {"id": secrets.token_hex(4), "name": name, "hash": _key_hash(key), "prefix": key[:8],
                 "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "last_used": None}
        with self._lock:
            u = self.users.get(n)
            if not u:
                raise ValueError("No such user")
            keys = u.setdefault("api_keys", [])
            if len(keys) >= MAX_API_KEYS:
                raise ValueError(f"Limit of {MAX_API_KEYS} keys reached; revoke one first")
            keys.append(entry)
            self._save()
        return {**self._public_key(entry), "key": key}

    def list_api_keys(self, username: str) -> list[dict]:
        u = self.users.get(normalize_username(username)) or {}
        return [self._public_key(k) for k in u.get("api_keys", [])]

    def revoke_api_key(self, username: str, key_id: str) -> None:
        with self._lock:
            u = self.users.get(normalize_username(username))
            keys = (u or {}).get("api_keys", [])
            remaining = [k for k in keys if k["id"] != key_id]
            if not u or len(remaining) == len(keys):
                raise ValueError("No such key")
            u["api_keys"] = remaining
            self._save()

    def revoke_all_keys(self, username: str) -> None:
        with self._lock:
            u = self.users.get(normalize_username(username))
            if not u:
                raise ValueError("No such user")
            u.pop("api_keys", None)
            self._save()

    def any_api_keys(self) -> bool:
        return any(u.get("api_keys") for u in list(self.users.values()))

    def find_key(self, key: str) -> str | None:
        """Username owning this API key, or None. Records last use (persisted at most hourly)."""
        h = _key_hash(key.strip())
        now = datetime.now(timezone.utc)
        for name, u in list(self.users.items()):
            for k in u.get("api_keys", []):
                if hmac.compare_digest(h, k["hash"]):
                    prev = k.get("last_used")
                    k["last_used"] = now.isoformat(timespec="seconds")
                    if not prev or (now - datetime.fromisoformat(prev)).total_seconds() > 3600:
                        with self._lock:
                            self._save()
                    return name
        return None

    def set_role(self, username: str, role: str) -> None:
        n = normalize_username(username)
        if role not in ROLES:
            raise ValueError("Role must be admin or viewer")
        with self._lock:
            if n not in self.users:
                raise ValueError("No such user")
            if (self.users[n]["role"] == "admin" and role != "admin"
                    and sum(u["role"] == "admin" for u in self.users.values()) <= 1):
                raise ValueError("Cannot demote the last administrator")
            self.users[n]["role"] = role
            self._save()

    def delete(self, username: str) -> None:
        n = normalize_username(username)
        with self._lock:
            if n not in self.users:
                raise ValueError("No such user")
            if self.users[n]["role"] == "admin" and sum(u["role"] == "admin" for u in self.users.values()) <= 1:
                raise ValueError("Cannot delete the last administrator")
            del self.users[n]
            self._save()
