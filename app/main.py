from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .accounts import accounts, admin, client_ip, is_https, limiter, router as accounts_router, user_for
from .digest import (UNKNOWN, UPDATE_AVAILABLE, UP_TO_DATE, compare_digests,
                     map_portainer_status, parse_image_ref, pick_image_reference)
from .notify import NotifyError, Notifier
from .version import __version__
from .portainer import Portainer, PortainerError
from .registry import RateLimited, RegistryClient, RegistryError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("monitor")

STATIC = Path(__file__).parent / "static"
DOCKER_ENDPOINT_TYPES = {1, 2, 4}  # Docker, Agent, Edge Agent (skip Kubernetes etc.)


def env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


class State:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.environments: list[dict] = []
        self.last_refresh: str | None = None
        self.refreshing = False
        self.error: str | None = None


state = State()
portainer: Portainer | None = None
registry = RegistryClient()
notifier = Notifier()
_lock = asyncio.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def resolve_container(eid: int, machine: str, c: dict, sem: asyncio.Semaphore) -> dict:
    name = (c.get("Names") or ["/" + c["Id"][:12]])[0].lstrip("/")
    row = {"machine": machine, "env_id": eid, "id": c["Id"], "container": name, "image": c.get("Image", ""),
           "state": c.get("State", ""), "status": UNKNOWN, "reason": "", "checked": now()}
    async with sem:
        try:
            status, msg = await portainer.image_status(eid, c["Id"])
            mapped = map_portainer_status(status)
            if mapped:
                row["status"] = mapped
                row["source"] = "portainer"
                return row
            p_reason = msg or (f"Portainer reported '{status}'" if status else "")
            row["status"], row["reason"] = await digest_fallback(eid, c)
            row["source"] = "registry"
            if row["status"] == UNKNOWN and p_reason:
                row["reason"] = f"{row['reason']}; {p_reason}"
        except PortainerError as e:
            row["reason"] = f"Portainer error: {e}"
        except Exception as e:  # never let one container break the page
            log.exception("container check failed")
            row["reason"] = f"unexpected error: {type(e).__name__}"
    return row


async def digest_fallback(eid: int, c: dict) -> tuple[str, str]:
    image = c.get("Image", "")
    try:
        info = await portainer.image_inspect(eid, c["ImageID"])
    except PortainerError as e:
        return UNKNOWN, f"cannot inspect local image ({e})"
    if image.startswith("sha256:") or not image:
        # the tag moved to a newer image: recover the original reference from the container's own config
        config_image = None
        try:
            config_image = ((await portainer.container_inspect(eid, c["Id"])).get("Config") or {}).get("Image")
        except PortainerError:
            pass
        image = pick_image_reference(image, info.get("RepoTags"), config_image)
        if not image:
            return UNKNOWN, "container was started from an untagged image ID"
    ref = parse_image_ref(image)
    if ref.digest:
        return UNKNOWN, "image is pinned by digest"
    try:
        remote = await registry.get_digest(ref)
    except RateLimited as e:
        return UNKNOWN, f"rate limited: {e}"
    except RegistryError as e:
        return UNKNOWN, str(e)
    return compare_digests(ref, info.get("RepoDigests"), remote)


async def scan_environment(ep: dict, sem: asyncio.Semaphore) -> tuple[list[dict], dict]:
    eid, name = ep["Id"], ep["Name"]
    info = {"id": eid, "name": name, "online": True, "error": None}
    if ep.get("Status") == 2:
        info.update(online=False, error="Portainer reports this environment as down")
        return [], info
    try:
        containers = await portainer.containers(eid)
    except PortainerError as e:
        info.update(online=False, error=str(e))
        return [], info
    rows = await asyncio.gather(*(resolve_container(eid, name, c, sem) for c in containers))
    return list(rows), info


async def refresh() -> None:
    if _lock.locked():
        return
    async with _lock:
        state.refreshing = True
        try:
            registry.new_run()
            portainer.status_supported = None  # re-probe each run
            eps = [e for e in await portainer.endpoints() if e.get("Type") in DOCKER_ENDPOINT_TYPES]
            sem = asyncio.Semaphore(int(os.getenv("CONCURRENCY", "8")))
            results = await asyncio.gather(*(scan_environment(e, sem) for e in eps), return_exceptions=True)
            rows, envs = [], []
            for ep, res in zip(eps, results):
                if isinstance(res, Exception):
                    envs.append({"id": ep["Id"], "name": ep["Name"], "online": False, "error": str(res)})
                else:
                    rows += res[0]
                    envs.append(res[1])
            # keep last known rows for environments that are currently unreachable
            offline = {e["id"] for e in envs if not e["online"]}
            rows += [r for r in state.rows if r["env_id"] in offline]
            state.rows, state.environments = rows, envs
            state.last_refresh, state.error = now(), None
            await notifier.after_refresh(state.rows)  # never raises
        except PortainerError as e:
            state.error = f"Cannot reach Portainer: {e}"
            log.error(state.error)
        except Exception as e:
            state.error = f"Refresh failed: {type(e).__name__}: {e}"
            log.exception("refresh failed")
        finally:
            state.refreshing = False


async def loop() -> None:
    interval = float(os.getenv("REFRESH_INTERVAL_HOURS", "6")) * 3600
    while True:
        await refresh()
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global portainer
    log.info("Manifest %s starting", __version__)
    url, token = os.getenv("PORTAINER_URL"), os.getenv("PORTAINER_API_TOKEN")
    if not url or not token:
        raise RuntimeError("PORTAINER_URL and PORTAINER_API_TOKEN must be set")
    accounts.setup()  # loads users; creates the default admin/manifest login on first run
    portainer = Portainer(url, token, verify_tls=not env_bool("PORTAINER_SKIP_TLS_VERIFY"),
                          status_path=os.getenv("PORTAINER_STATUS_PATH") or None)
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    await portainer.aclose()
    await registry.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

PUBLIC_PATHS = {"/login", "/api/login", "/api/login/2fa", "/api/logout", "/static/logo.svg",
                "/api/stats"}  # /api/stats checks its own API key
# a user who must choose a new password can do nothing else until they have
FORCED_CHANGE_ALLOWED = {"/change-password", "/api/change-password", "/api/me", "/api/logout", "/static/logo.svg"}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            allowed = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
            if urlparse(origin).netloc not in allowed:
                return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    user = user_for(request)
    request.state.user = user
    if accounts.enabled and path not in PUBLIC_PATHS:
        is_api = path.startswith("/api/")
        if user is None:
            if is_api:
                return JSONResponse({"detail": "Not signed in"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        if user["must_change"] and path not in FORCED_CHANGE_ALLOWED:
            if is_api:
                return JSONResponse({"detail": "You must choose a new password first", "code": "must_change"},
                                    status_code=403)
            return RedirectResponse("/change-password", status_code=303)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if is_https(request):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if path.startswith("/api/") or path in ("/login", "/change-password"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


app.include_router(accounts_router)


@app.get("/api/containers")
async def containers(request: Request):
    rows = state.rows
    return {
        "me": request.state.user,
        "version": __version__,
        "rows": rows,
        "environments": state.environments,
        "last_refresh": state.last_refresh,
        "refreshing": state.refreshing,
        "updates_enabled": updates_enabled(),
        "auth_enabled": accounts.enabled,
        "error": state.error,
        "summary": summary_counts(rows),
    }


def summary_counts(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "outdated": sum(r["status"] == UPDATE_AVAILABLE for r in rows),
        "up_to_date": sum(r["status"] == UP_TO_DATE for r in rows),
        "unknown": sum(r["status"] == UNKNOWN for r in rows),
    }


@app.get("/api/stats")
async def stats(request: Request):
    """Counts only, for dashboard widgets (e.g. Homepage). Needs an API key, not a login: either a
    per-user key created in Settings, or the optional global STATS_API_KEY from the environment."""
    env_key = os.getenv("STATS_API_KEY", "").strip()
    user_keys = accounts.enabled and accounts.store.any_api_keys()
    if not env_key and not user_keys:
        raise HTTPException(404, "Not found")  # feature off: indistinguishable from a missing page
    rl = f"stats:{client_ip(request)}"
    if limiter.blocked_for(rl):
        raise HTTPException(429, "Too many failed attempts")
    auth_header = request.headers.get("authorization", "")
    given = (request.headers.get("x-api-key")
             or (auth_header[7:] if auth_header.lower().startswith("bearer ") else "")).strip()
    ok = bool(given) and (
        (bool(env_key) and hmac.compare_digest(given.encode(), env_key.encode()))
        or (user_keys and accounts.store.find_key(given) is not None))
    if not ok:
        limiter.fail(rl)
        raise HTTPException(401, "Invalid API key")
    limiter.success(rl)
    return {**summary_counts(state.rows),
            "offline_environments": sum(not e["online"] for e in state.environments),
            "refreshing": state.refreshing,
            "last_refresh": state.last_refresh}


def updates_enabled() -> bool:
    return os.getenv("ENABLE_UPDATES", "true").strip().lower() not in ("0", "false", "no", "off")


_updating: set[tuple[int, str]] = set()


@app.post("/api/containers/{eid}/{cid}/update")
async def update_container(eid: int, cid: str, _: dict = Depends(admin)):
    """Pull the new image and recreate the container via Portainer, then re-check it."""
    if not updates_enabled():
        raise HTTPException(403, "Updates are disabled (ENABLE_UPDATES=false)")
    row = next((r for r in state.rows if r["env_id"] == eid and r.get("id") == cid), None)
    if not row:
        raise HTTPException(404, "Container not found; refresh and try again")
    if row["status"] != UPDATE_AVAILABLE:
        raise HTTPException(400, "Container is not marked as having an update")
    own = os.getenv("HOSTNAME", "")
    if own and cid.startswith(own):
        raise HTTPException(400, "Manifest cannot update itself; do it from Portainer")
    key = (eid, cid)
    if key in _updating:
        raise HTTPException(409, "An update for this container is already running")
    _updating.add(key)
    try:
        try:
            await portainer.recreate(eid, cid)
        except PortainerError as e:
            raise HTTPException(502, str(e))
        # re-check the recreated container (new ID, same name)
        registry.new_run()
        try:
            fresh = next((c for c in await portainer.containers(eid)
                          if row["container"] in [n.lstrip("/") for n in c.get("Names", [])]), None)
        except PortainerError:
            fresh = None
        if fresh is None:
            raise HTTPException(502, "Recreated, but could not find the new container; press Refresh")
        new_row = await resolve_container(eid, row["machine"], fresh, asyncio.Semaphore(1))
        state.rows = [new_row if r is row else r for r in state.rows]
        return {"status": new_row["status"], "reason": new_row["reason"]}
    finally:
        _updating.discard(key)


class SettingsIn(BaseModel):
    discord_webhook_url: str | None = None  # omitted = keep, "" = remove
    public_url: str | None = None           # omitted = keep, "" = remove


@app.get("/api/settings")
async def get_settings(_: dict = Depends(admin)):
    return notifier.public_view()


@app.put("/api/settings")
async def put_settings(body: SettingsIn, _: dict = Depends(admin)):
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        notifier.update_settings(fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except NotifyError as e:
        raise HTTPException(500, str(e))
    return notifier.public_view()


@app.post("/api/notify/test")
async def notify_test(_: dict = Depends(admin)):
    if not notifier.enabled:
        raise HTTPException(400, "No Discord webhook is configured")
    try:
        await notifier.send_test()
    except NotifyError as e:
        raise HTTPException(502, f"Test notification failed: {e}")
    return {"sent": True}


@app.post("/api/refresh")
async def trigger_refresh():
    if not state.refreshing:
        asyncio.create_task(refresh())
        state.refreshing = True
    return {"refreshing": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
