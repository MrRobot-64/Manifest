"""Thin Portainer API client. The token never leaves this process."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("portainer")

# Candidate routes for Portainer's container image-status feature. The route is not
# documented in the public Swagger for CE, so these are probed at runtime and the first
# that answers 200 with a Status field is remembered. Override with PORTAINER_STATUS_PATH.
STATUS_PATH_CANDIDATES = [
    "/api/docker/{eid}/containers/{cid}/image_status",
    "/api/endpoints/{eid}/docker/containers/{cid}/image_status",
]


class PortainerError(Exception):
    pass


class Portainer:
    def __init__(self, url: str, token: str, verify_tls: bool, status_path: str | None = None):
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"), headers={"X-API-Key": token},
            verify=verify_tls, timeout=httpx.Timeout(30, connect=10),
        )
        self._candidates = [status_path] if status_path else list(STATUS_PATH_CANDIDATES)
        self._status_path: str | None = None
        self.status_supported: bool | None = None  # None = not yet determined

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params):
        try:
            r = await self._http.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise PortainerError(f"{type(e).__name__}: {e}") from e
        return r

    async def _get_json(self, path: str, **params):
        r = await self._get(path, **params)
        if r.status_code in (401, 403):
            raise PortainerError(f"HTTP {r.status_code}: API token rejected or lacks access")
        if r.status_code != 200:
            raise PortainerError(f"HTTP {r.status_code} from {path}")
        return r.json()

    async def endpoints(self) -> list[dict]:
        return await self._get_json("/api/endpoints", limit=1000)

    async def containers(self, eid: int) -> list[dict]:
        return await self._get_json(f"/api/endpoints/{eid}/docker/containers/json", all="true")

    async def container_inspect(self, eid: int, cid: str) -> dict:
        return await self._get_json(f"/api/endpoints/{eid}/docker/containers/{cid}/json")

    async def image_inspect(self, eid: int, image_id: str) -> dict:
        return await self._get_json(f"/api/endpoints/{eid}/docker/images/{image_id}/json")

    async def recreate(self, eid: int, cid: str, pull_image: bool = True) -> None:
        """Pull the latest image and recreate the container (Portainer's own 'Recreate')."""
        try:
            r = await self._http.post(f"/api/docker/{eid}/containers/{cid}/recreate",
                                      json={"PullImage": pull_image}, timeout=httpx.Timeout(900, connect=10))
        except httpx.HTTPError as e:
            raise PortainerError(f"{type(e).__name__}: {e}") from e
        if r.status_code in (401, 403):
            raise PortainerError("Portainer token lacks permission to recreate containers")
        if r.status_code != 200:
            try:
                detail = r.json().get("details") or r.json().get("message") or ""
            except ValueError:
                detail = ""
            raise PortainerError(f"recreate failed (HTTP {r.status_code}) {detail}".strip())

    async def image_status(self, eid: int, cid: str) -> tuple[str | None, str]:
        """Return (portainer_status, message). status None => endpoint unavailable/unusable."""
        if self.status_supported is False:
            return None, "Portainer image-status endpoint not available on this server"
        paths = [self._status_path] if self._status_path else self._candidates
        last = "no response"
        for tpl in paths:
            r = await self._get(tpl.format(eid=eid, cid=cid))
            if r.status_code == 200:
                try:
                    body = r.json()
                except ValueError:
                    last = "non-JSON response"
                    continue
                if isinstance(body, dict):
                    low = {k.lower(): v for k, v in body.items()}
                    if "status" in low:
                        if self._status_path is None:
                            log.info("Using Portainer image status route %s", tpl)
                        self._status_path = tpl
                        self.status_supported = True
                        return str(low["status"]), str(low.get("message") or "")
                last = "unexpected response shape"
            elif r.status_code in (401, 403):
                last = f"HTTP {r.status_code}"
            elif r.status_code in (404, 405):
                last = f"HTTP {r.status_code}"
            else:
                # transient server error: don't conclude the route is missing
                return None, f"Portainer status check failed (HTTP {r.status_code})"
        if self._status_path is None and self.status_supported is None:
            if last.startswith("HTTP 40") and last[-3:] in ("404", "405"):
                self.status_supported = False
                log.warning("Portainer image status endpoint not found; using registry digest comparison only")
        return None, f"Portainer image status unavailable ({last})"
