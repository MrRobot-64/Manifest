"""Discord webhook alerts for newly outdated containers."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .digest import UNKNOWN, UPDATE_AVAILABLE

log = logging.getLogger("notify")

COLOR_AMBER = 0xF59E0B
MAX_LINES = 40


DISCORD_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}


class NotifyError(Exception):
    pass


def validate_webhook_url(url: str) -> str:
    """Only real Discord webhook URLs are accepted, so this can't be used to make the
    server POST to arbitrary hosts."""
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    if u.scheme != "https" or u.hostname not in DISCORD_HOSTS or u.username or u.password:
        raise ValueError("Not a Discord webhook URL (expected https://discord.com/api/webhooks/...)")
    if not u.path.startswith("/api/webhooks/") or len(u.path) <= len("/api/webhooks/"):
        raise ValueError("Not a Discord webhook URL (expected https://discord.com/api/webhooks/...)")
    return url.strip()


def validate_public_url(url: str) -> str:
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Manifest URL must start with http:// or https://")
    return url.strip()


def row_key(r: dict) -> str:
    """Stable across recreation (container IDs change, names don't)."""
    return f"{r['machine']}|{r['container']}"


def plan_alerts(rows: list[dict], alerted: set[str]) -> tuple[list[dict], set[str]]:
    """Return (containers to alert about now, alerted keys to keep).

    A key is dropped once its container is up to date or gone, so a later update alerts
    again. Keys are kept while the status is merely 'unknown' (e.g. a rate-limited check)
    so a transient failure doesn't cause a repeat alert.
    """
    outdated = {row_key(r): r for r in rows if r["status"] == UPDATE_AVAILABLE}
    unknown = {row_key(r) for r in rows if r["status"] == UNKNOWN}
    keep = {k for k in alerted if k in outdated or k in unknown}
    pending = sorted((r for k, r in outdated.items() if k not in alerted),
                     key=lambda r: (r["machine"].lower(), r["container"].lower()))
    return pending, keep


def build_message(pending: list[dict], public_url: str | None = None, limit: int = MAX_LINES) -> dict:
    n = len(pending)
    lines: list[str] = []
    shown = 0
    machine = None
    for r in pending:
        if shown >= limit:
            break
        if r["machine"] != machine:
            machine = r["machine"]
            lines.append(f"**{machine}**")
        lines.append(f"• `{r['container']}` — {r['image']}")
        shown += 1
    if n > shown:
        lines.append(f"…and {n - shown} more")
    embed = {
        "title": f"{n} container update{'s' if n != 1 else ''} available",
        "description": "\n".join(lines)[:4000],
        "color": COLOR_AMBER,
    }
    if public_url:
        embed["url"] = public_url
    return {"username": "Manifest", "embeds": [embed]}


class Notifier:
    def __init__(self) -> None:
        self.path = Path(os.getenv("STATE_FILE", "/data/alerted.json"))
        self.settings_path = Path(os.getenv("SETTINGS_FILE", "/data/settings.json"))
        self.webhook, self.public_url, self.source = self._load_config()
        self.alerted: set[str] = self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook)

    def _read_saved(self) -> dict:
        try:
            saved = json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def _load_config(self) -> tuple[str | None, str | None, str | None]:
        """Env vars are the defaults; anything saved from the settings dialog overrides them."""
        webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
        public = os.getenv("PUBLIC_URL", "").strip() or None
        source = "env" if webhook else None
        saved = self._read_saved()
        if "discord_webhook_url" in saved:
            webhook = saved["discord_webhook_url"] or None
            source = "settings" if webhook else None
        if "public_url" in saved:
            public = saved["public_url"] or None
        return webhook, public, source

    def update_settings(self, fields: dict) -> None:
        """`fields` may contain discord_webhook_url and/or public_url. '' clears a value.
        Raises ValueError for invalid input and NotifyError if it cannot be saved."""
        new: dict[str, str] = {}
        if "discord_webhook_url" in fields:
            v = (fields["discord_webhook_url"] or "").strip()
            new["discord_webhook_url"] = validate_webhook_url(v) if v else ""
        if "public_url" in fields:
            v = (fields["public_url"] or "").strip()
            new["public_url"] = validate_public_url(v) if v else ""
        merged = {**self._read_saved(), **new}
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(merged))
            try:
                os.chmod(self.settings_path, 0o600)
            except OSError:
                pass
        except OSError:
            raise NotifyError("Could not save settings (is the /data volume writable?)") from None
        self.webhook, self.public_url, self.source = self._load_config()

    def public_view(self) -> dict:
        """What the browser may see. The webhook URL itself never leaves the server."""
        return {"enabled": self.enabled, "source": self.source,
                "webhook_hint": ("…" + self.webhook[-4:]) if self.webhook else None,
                "public_url": self.public_url or ""}

    def _load(self) -> set[str]:
        try:
            return set(json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            return set()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(sorted(self.alerted)))
        except OSError as e:
            log.warning("Could not save alert state (%s); alerts may repeat after a restart", e.__class__.__name__)

    async def _post(self, payload: dict) -> None:
        import httpx  # lazy: keeps the pure functions importable without httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self.webhook, json=payload)
        except httpx.HTTPError as e:
            raise NotifyError(type(e).__name__) from None
        if r.status_code // 100 != 2:
            raise NotifyError(f"Discord returned HTTP {r.status_code}")

    async def send_test(self) -> None:
        await self._post({"username": "Manifest", "embeds": [{
            "title": "Manifest test notification",
            "description": "Alerts are working. You'll get a message here when a container has an update.",
            "color": COLOR_AMBER, **({"url": self.public_url} if self.public_url else {})}]})

    async def after_refresh(self, rows: list[dict]) -> None:
        """Never raises: alerting must not break a refresh."""
        if not self.enabled:
            return
        try:
            pending, keep = plan_alerts(rows, self.alerted)
            if pending:
                try:
                    await self._post(build_message(pending, self.public_url))
                    keep |= {row_key(r) for r in pending}
                    log.info("Sent Discord alert for %d container(s)", len(pending))
                except NotifyError as e:
                    log.warning("Discord alert failed (%s); will retry on next refresh", e)
            if keep != self.alerted:
                self.alerted = keep
                self._save()
        except Exception:
            log.exception("alert processing failed")
