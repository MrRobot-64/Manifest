"""Pure logic: image reference parsing and local-vs-registry digest comparison."""
from __future__ import annotations

import re
from dataclasses import dataclass

UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "update_available"
UNKNOWN = "unknown"

DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}


@dataclass(frozen=True)
class ImageRef:
    registry: str      # host used for API calls, e.g. registry-1.docker.io
    repo: str          # e.g. library/nginx
    tag: str | None    # e.g. latest
    digest: str | None # set if the reference is pinned with @sha256:...

    @property
    def reference(self) -> str:
        return self.digest or self.tag or "latest"

    @property
    def canonical_repo(self) -> str:
        """Registry+repo as it appears in RepoDigests (docker.io is omitted by Docker)."""
        return self.repo if self.registry == "registry-1.docker.io" else f"{self.registry}/{self.repo}"


def parse_image_ref(image: str) -> ImageRef:
    """Parse 'nginx', 'user/app:1.2', 'ghcr.io/o/r:tag', 'host:5000/x@sha256:..'."""
    image = image.strip()
    digest = None
    if "@" in image:
        image, digest = image.split("@", 1)
    tag = None
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        image, tag = image.rsplit(":", 1)
    parts = image.split("/", 1)
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts
    else:
        registry, repo = "docker.io", image
    if registry in DOCKER_HUB_ALIASES:
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    if tag is None and digest is None:
        tag = "latest"
    return ImageRef(registry, repo, tag, digest)


def _normalise_repo_digest(rd: str) -> tuple[str, str] | None:
    if "@" not in rd:
        return None
    repo, dig = rd.split("@", 1)
    ref = parse_image_ref(repo)
    return ref.canonical_repo, dig


def compare_digests(ref: ImageRef, local_repo_digests: list[str] | None, remote_digest: str | None) -> tuple[str, str]:
    """Return (status, reason). `local_repo_digests` is Docker's RepoDigests list."""
    if ref.digest:
        return UNKNOWN, "image is pinned by digest"
    if not remote_digest:
        return UNKNOWN, "no digest returned by registry"
    if not local_repo_digests:
        return UNKNOWN, "local image has no repo digest (built locally or pulled by ID)"
    mine = [d for r in local_repo_digests if (n := _normalise_repo_digest(r)) and n[0] == ref.canonical_repo for d in [n[1]]]
    if not mine:
        return UNKNOWN, "local image digests do not match this repository"
    if remote_digest in mine:
        return UP_TO_DATE, ""
    return UPDATE_AVAILABLE, ""


def pick_image_reference(listed: str | None, repo_tags: list[str] | None, config_image: str | None) -> str | None:
    """Work out which image:tag a container was started from.

    Docker's container list shows a bare image ID (sha256:...) instead of the name when the tag has since
    moved to a newer image, i.e. exactly when an update has been pulled but the container not recreated.
    The container's own config still records the original reference, so prefer that, then any tag on
    the old image.
    """
    def named(ref: str | None) -> bool:
        return bool(ref) and not ref.startswith("sha256:")

    if named(listed):
        return listed
    if named(config_image):
        return config_image
    for tag in repo_tags or []:
        if named(tag) and tag != "<none>:<none>":
            return tag
    return None


def map_portainer_status(status: str | None) -> str | None:
    """Map Portainer's image status to ours. None means 'fall back' (skipped/error/...)."""
    s = (status or "").lower()
    if s == "updated":
        return UP_TO_DATE
    if s == "outdated":
        return UPDATE_AVAILABLE
    return None


_PARAM = re.compile(r'(\w+)="([^"]*)"')


def parse_www_authenticate(header: str) -> tuple[str, dict[str, str]] | None:
    """'Bearer realm="..",service=".."' -> ('bearer', {realm:..., service:...})."""
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    return scheme.lower(), {k.lower(): v for k, v in _PARAM.findall(rest)}
