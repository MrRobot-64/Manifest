"""Login, sessions and user-management routes."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .auth import (COOKIE, LoginLimiter, Sessions, fingerprint, is_local_ip, load_secret,
                   validate_new_password)
from .users import UserStore

log = logging.getLogger("accounts")
STATIC = Path(__file__).parent / "static"


def env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


class Accounts:
    enabled = False
    store: UserStore
    sessions: Sessions

    def setup(self) -> None:
        if env_bool("AUTH_DISABLED"):
            log.warning("AUTH_DISABLED is set: the app has NO login. Do not expose it to the internet.")
            self.enabled = False
            return
        self.store = UserStore(Path(os.getenv("USERS_FILE", "/data/users.json")))
        self.store.load_or_seed()
        self.sessions = Sessions(load_secret(), int(float(os.getenv("SESSION_HOURS", "168")) * 3600))
        self.enabled = True


accounts = Accounts()
limiter = LoginLimiter()
router = APIRouter()
NO_LOGIN_USER = {"username": "(no login)", "role": "admin", "must_change": False, "totp": False}
CHALLENGE_SECONDS = 300


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def client_ip(request: Request) -> str:
    """Behind a reverse proxy set TRUST_PROXY=true so each visitor is judged by their own address."""
    if env_bool("TRUST_PROXY"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()  # the entry added by our own proxy
    return request.client.host if request.client else "unknown"


def user_for(request: Request) -> dict | None:
    """The signed-in user (without secrets), or None."""
    if not accounts.enabled:
        return NO_LOGIN_USER
    parsed = accounts.sessions.parse(request.cookies.get(COOKIE))
    if not parsed:
        return None
    name, fp = parsed
    u = accounts.store.get(name)
    if not u or not hmac.compare_digest(fingerprint(u["hash"]), fp):
        return None
    return {"username": u["username"], "role": u["role"], "must_change": u["must_change"],
            "totp": bool(u.get("totp_secret"))}


def admin(request: Request) -> dict:
    u = getattr(request.state, "user", None)
    if not u or u["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return u


def _set_session(resp: JSONResponse, request: Request, user: dict) -> None:
    resp.set_cookie(COOKIE, accounts.sessions.issue(user["username"], fingerprint(user["hash"])),
                    max_age=accounts.sessions.session_seconds, httponly=True, samesite="strict",
                    secure=is_https(request), path="/")


def _throttled(key: str) -> JSONResponse | None:
    wait = limiter.blocked_for(key)
    if wait:
        return JSONResponse({"detail": f"Too many failed attempts. Try again in {wait // 60 + 1} min."},
                            status_code=429, headers={"Retry-After": str(wait)})
    return None


# ---- pages -------------------------------------------------------------------------

@router.get("/login")
async def login_page(request: Request):
    if user_for(request) is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html")


@router.get("/change-password")
async def change_password_page(request: Request):
    return FileResponse(STATIC / "change-password.html")


# ---- login / logout / me -----------------------------------------------------------

class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/api/login")
async def login(body: LoginIn, request: Request):
    if not accounts.enabled:
        return {"ok": True}
    ip = client_ip(request)
    if (blocked := _throttled(ip)):
        return blocked
    user = await asyncio.to_thread(accounts.store.authenticate, body.username, body.password)  # CPU-heavy
    if not user:
        limiter.fail(ip)
        log.warning("Failed login from %s", ip)
        await asyncio.sleep(1)
        raise HTTPException(401, "Invalid username or password")
    if user["default"] and not (env_bool("BOOTSTRAP_ANYWHERE") or is_local_ip(ip)):
        limiter.fail(ip)
        log.warning("Default-credential login refused from non-local address %s", ip)
        raise HTTPException(403, "The default login only works from your local network. "
                                 "Sign in from the LAN and choose a new password first.")
    if user.get("totp_secret"):
        # password was right, but no session yet: the browser must now present a 2FA code
        challenge = accounts.sessions.issue(user["username"], fingerprint(user["hash"]),
                                            purpose="2fa", seconds=CHALLENGE_SECONDS)
        return {"ok": True, "needs_2fa": True, "challenge": challenge}
    limiter.success(ip)
    resp = JSONResponse({"ok": True, "must_change": user["must_change"]})
    _set_session(resp, request, user)
    return resp


class SecondFactorIn(BaseModel):
    challenge: str
    code: str


@router.post("/api/login/2fa")
async def login_second_factor(body: SecondFactorIn, request: Request):
    if not accounts.enabled:
        return {"ok": True}
    parsed = accounts.sessions.parse(body.challenge, purpose="2fa")
    user = accounts.store.get(parsed[0]) if parsed else None
    if not user or not hmac.compare_digest(fingerprint(user["hash"]), parsed[1]) or not user.get("totp_secret"):
        raise HTTPException(401, "Your sign-in expired. Enter your password again.")
    ip, ukey = client_ip(request), f"2fa:{user['username']}"
    for key in (ip, ukey):
        if (blocked := _throttled(key)):
            return blocked
    if not await asyncio.to_thread(accounts.store.check_second_factor, user["username"], body.code):
        limiter.fail(ip)
        limiter.fail(ukey)
        log.warning("Failed 2FA code for %s from %s", user["username"], ip)
        await asyncio.sleep(1)
        raise HTTPException(401, "Invalid code")
    limiter.success(ip)
    limiter.success(ukey)
    resp = JSONResponse({"ok": True, "must_change": user["must_change"]})
    _set_session(resp, request, user)
    return resp


@router.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/api/me")
async def me(request: Request):
    return {**request.state.user, "auth_enabled": accounts.enabled}


# ---- own password ------------------------------------------------------------------

class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/api/change-password")
async def change_password(body: ChangePasswordIn, request: Request):
    if not accounts.enabled:
        raise HTTPException(400, "Login is disabled")
    name = request.state.user["username"]
    key = f"pw:{name}"
    if (blocked := _throttled(key)):
        return blocked
    ok = await asyncio.to_thread(accounts.store.authenticate, name, body.current_password)
    if not ok:
        limiter.fail(key)
        await asyncio.sleep(1)
        raise HTTPException(400, "Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(400, "The new password must be different from the current one")
    try:
        validate_new_password(body.new_password, name)
        await asyncio.to_thread(accounts.store.set_password, name, body.new_password, False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    limiter.success(key)
    resp = JSONResponse({"ok": True})
    _set_session(resp, request, accounts.store.get(name))  # old sessions are now invalid; keep this one
    return resp


# ---- own two-factor authentication ---------------------------------------------------

class PasswordIn(BaseModel):
    password: str


class CodeIn(BaseModel):
    code: str


class DisableIn(BaseModel):
    password: str
    code: str


async def _require_password(name: str, password: str) -> None:
    key = f"pw:{name}"
    if (wait := limiter.blocked_for(key)):
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait // 60 + 1} min.")
    if not await asyncio.to_thread(accounts.store.authenticate, name, password):
        limiter.fail(key)
        await asyncio.sleep(1)
        raise HTTPException(400, "Password is incorrect")
    limiter.success(key)


@router.get("/api/2fa")
async def twofa_status(request: Request):
    _need_auth()
    return accounts.store.totp_status(request.state.user["username"])


@router.post("/api/2fa/setup")
async def twofa_setup(body: PasswordIn, request: Request):
    """Step 1: confirm the password, create a pending secret and return it as a QR code."""
    _need_auth()
    from . import totp
    name = request.state.user["username"]
    await _require_password(name, body.password)
    try:
        secret = accounts.store.begin_totp(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    uri = totp.otpauth_uri(secret, name)
    import segno  # lazy: only needed here
    qr = segno.make(uri, error="m").svg_data_uri(scale=5, border=2)
    return {"secret": secret, "uri": uri, "qr": qr}


@router.post("/api/2fa/enable")
async def twofa_enable(body: CodeIn, request: Request):
    """Step 2: prove the app works by entering a code. Returns the one-time recovery codes."""
    _need_auth()
    name = request.state.user["username"]
    key = f"2fa:{name}"
    if (wait := limiter.blocked_for(key)):
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait // 60 + 1} min.")
    try:
        codes = accounts.store.confirm_totp(name, body.code)
    except ValueError as e:
        limiter.fail(key)
        raise HTTPException(400, str(e))
    limiter.success(key)
    return {"recovery_codes": codes}


@router.post("/api/2fa/disable")
async def twofa_disable(body: DisableIn, request: Request):
    _need_auth()
    name = request.state.user["username"]
    if not accounts.store.has_totp(name):
        raise HTTPException(400, "Two-factor authentication is not on")
    await _require_password(name, body.password)
    key = f"2fa:{name}"
    if (wait := limiter.blocked_for(key)):
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait // 60 + 1} min.")
    if not await asyncio.to_thread(accounts.store.check_second_factor, name, body.code):
        limiter.fail(key)
        await asyncio.sleep(1)
        raise HTTPException(400, "That code is not correct")
    limiter.success(key)
    accounts.store.disable_totp(name)
    return {"ok": True}


# ---- own API keys --------------------------------------------------------------------

class KeyIn(BaseModel):
    name: str


@router.get("/api/keys")
async def list_keys(request: Request):
    _need_auth()
    return accounts.store.list_api_keys(request.state.user["username"])


@router.post("/api/keys")
async def create_key(body: KeyIn, request: Request):
    """The only time the key is shown; afterwards just its first characters."""
    _need_auth()
    try:
        return accounts.store.create_api_key(request.state.user["username"], body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/keys/{key_id}")
async def revoke_key(key_id: str, request: Request):
    _need_auth()
    try:
        accounts.store.revoke_api_key(request.state.user["username"], key_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return accounts.store.list_api_keys(request.state.user["username"])


# ---- user management (administrators) ------------------------------------------------

class NewUserIn(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class ResetIn(BaseModel):
    password: str


def _need_auth() -> None:
    if not accounts.enabled:
        raise HTTPException(400, "Login is disabled (AUTH_DISABLED=true), so there are no users")


@router.get("/api/users")
async def list_users(_: dict = Depends(admin)):
    _need_auth()
    return accounts.store.public_list()


@router.post("/api/users")
async def create_user(body: NewUserIn, _: dict = Depends(admin)):
    _need_auth()
    try:
        await asyncio.to_thread(accounts.store.create, body.username, body.password, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return accounts.store.public_list()


@router.post("/api/users/{username}/reset")
async def reset_user(username: str, body: ResetIn, me: dict = Depends(admin)):
    _need_auth()
    if username.strip().lower() == me["username"]:
        raise HTTPException(400, "Use Change password for your own account")
    try:  # a temporary password: the user must replace it at next sign-in
        await asyncio.to_thread(accounts.store.set_password, username, body.password, True)
        accounts.store.revoke_all_keys(username)  # a reset usually means "assume it's compromised"
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/api/users/{username}/revoke-keys")
async def revoke_user_keys(username: str, _: dict = Depends(admin)):
    _need_auth()
    try:
        accounts.store.revoke_all_keys(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class RoleIn(BaseModel):
    role: str


@router.post("/api/users/{username}/role")
async def set_user_role(username: str, body: RoleIn, me: dict = Depends(admin)):
    """Promote to administrator or demote to a standard (read-only) user. Takes effect immediately."""
    _need_auth()
    if username.strip().lower() == me["username"]:
        raise HTTPException(400, "You cannot change your own role")
    try:
        accounts.store.set_role(username, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return accounts.store.public_list()


@router.post("/api/users/{username}/reset-2fa")
async def reset_user_2fa(username: str, _: dict = Depends(admin)):
    """For a user who lost their authenticator and recovery codes."""
    _need_auth()
    try:
        accounts.store.disable_totp(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/api/users/{username}")
async def delete_user(username: str, me: dict = Depends(admin)):
    _need_auth()
    if username.strip().lower() == me["username"]:
        raise HTTPException(400, "You cannot delete your own account")
    try:
        accounts.store.delete(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return accounts.store.public_list()
