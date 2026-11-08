"""Minimal anonymous registry client: fetch a manifest digest via HEAD."""
from __future__ import annotations

import time

import httpx

from .digest import ImageRef, parse_www_authenticate

MANIFEST_TYPES = ", ".join([
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])


class RegistryError(Exception):
    pass


class RateLimited(RegistryError):
    pass


class RegistryClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15, follow_redirects=True,
                                       headers={"User-Agent": "manifest-update-checker"})
        self._cache: dict[tuple, str] = {}
        self._blocked_until: dict[str, float] = {}

    def new_run(self) -> None:
        self._cache.clear()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _check_blocked(self, registry: str) -> None:
        until = self._blocked_until.get(registry, 0)
        if until > time.time():
            raise RateLimited(f"{registry} rate limit hit; retry in {int(until - time.time())}s")

    def _rate_limited(self, registry: str, resp: httpx.Response) -> RateLimited:
        try:
            wait = int(resp.headers.get("retry-after", "600"))
        except ValueError:
            wait = 600
        self._blocked_until[registry] = time.time() + wait
        return RateLimited(f"{registry} rate limit hit (HTTP 429); retry in ~{wait}s")

    async def get_digest(self, ref: ImageRef) -> str:
        key = (ref.registry, ref.repo, ref.reference)
        if key in self._cache:
            return self._cache[key]
        self._check_blocked(ref.registry)
        url = f"https://{ref.registry}/v2/{ref.repo}/manifests/{ref.reference}"
        headers = {"Accept": MANIFEST_TYPES}
        try:
            r = await self._http.head(url, headers=headers)
            if r.status_code == 401:
                token = await self._token(ref, r.headers.get("www-authenticate", ""))
                headers["Authorization"] = f"Bearer {token}"
                r = await self._http.head(url, headers=headers)
        except httpx.HTTPError as e:
            raise RegistryError(f"registry unreachable: {type(e).__name__}") from e
        if r.status_code == 429:
            raise self._rate_limited(ref.registry, r)
        if r.status_code == 404:
            raise RegistryError("image/tag not found in registry")
        if r.status_code in (401, 403):
            raise RegistryError("registry requires authentication (private image?)")
        if r.status_code != 200:
            raise RegistryError(f"registry returned HTTP {r.status_code}")
        digest = r.headers.get("docker-content-digest")
        if not digest:
            raise RegistryError("registry did not return a digest")
        self._cache[key] = digest
        return digest

    async def _token(self, ref: ImageRef, www_auth: str) -> str:
        parsed = parse_www_authenticate(www_auth)
        if not parsed or parsed[0] != "bearer" or "realm" not in parsed[1]:
            raise RegistryError("registry requires authentication (private image?)")
        p = parsed[1]
        params = {"scope": f"repository:{ref.repo}:pull"}
        if "service" in p:
            params["service"] = p["service"]
        try:
            r = await self._http.get(p["realm"], params=params)
        except httpx.HTTPError as e:
            raise RegistryError(f"token service unreachable: {type(e).__name__}") from e
        if r.status_code == 429:
            raise self._rate_limited(ref.registry, r)
        if r.status_code != 200:
            raise RegistryError(f"token request failed (HTTP {r.status_code}); private image?")
        data = r.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RegistryError("token service returned no token")
        return token
