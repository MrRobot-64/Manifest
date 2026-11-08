# Manifest

<p align="center">
  <img src="assets/manifest-icon.png" alt="Manifest logo" width="200">
</p>

One page showing every Docker container across all your Portainer environments and whether a newer image exists. It talks only to the Portainer API; nothing is installed on your machines beyond the Portainer Agent. The API token stays server-side.

## Install

Manifest runs as a Docker container. The image is published at `ghcr.io/mrrobot-64/manifest` (amd64 and arm64), so there is nothing to build.

**1. Get a Portainer API token.** In Portainer click your username (top right) → **My account** → **Access tokens** → **Add access token**. Name it and copy the `ptr_...` value (shown once). The token has the same rights as your user; a dedicated read-only user is safer.

**2. Download the compose file and the settings template** on the machine that will run Manifest:

```bash
curl -O https://raw.githubusercontent.com/MrRobot-64/Manifest/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/MrRobot-64/Manifest/main/.env.example
```

**3. Edit `.env`:** set `PORTAINER_URL` and `PORTAINER_API_TOKEN` (everything else is optional; see [Configuration](#configuration)).

**4. Start it:**

```bash
docker compose pull
docker compose up -d
```

**5. Open `http://<host>:8080`** and sign in with `admin` / `manifest`. You are asked to choose a new password straight away.

To update later: `docker compose pull && docker compose up -d`. To pin a version, change the image tag in `docker-compose.yml` to e.g. `:1.0.0`. To build from source instead, clone the repo and run `docker compose up -d --build`.

Setting it up on a fresh VM? See [SETUP-VM.md](SETUP-VM.md).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORTAINER_URL` | required | e.g. `https://portainer.lan:9443` |
| `PORTAINER_API_TOKEN` | required | Sent as `X-API-Key` |
| `PORTAINER_SKIP_TLS_VERIFY` | `false` | `true` for self-signed certs |
| `REFRESH_INTERVAL_HOURS` | `6` | Background refresh interval (fractions allowed) |
| `PORTAINER_STATUS_PATH` | auto | Override Portainer image-status route, e.g. `/api/docker/{eid}/containers/{cid}/image_status` |
| `CONCURRENCY` | `8` | Parallel container checks |
| `ENABLE_UPDATES` | `true` | Show the **Update** button. Set `false` for a read-only dashboard |

## Login and public access

Everything (page, API, settings, Update button) requires signing in.

**First sign-in:** username `admin`, password `manifest`. You are immediately forced to choose a new password (at least 10 characters) before you can see anything else. Afterwards, click the gear icon to change your password, create users, reset or delete users.

- **Roles:** *Administrators* can do everything: update containers, change settings (Discord) and manage users. *Standard users* are read-only: they can see the table and press Refresh, but the server refuses Update, settings and user management for them. An admin picks the role when creating a user and can switch it later with **Make admin** / **Make standard** in the Users list (you can't change your own role, so there is always an administrator). Users created by an admin must choose their own password at first sign-in, as do users whose password an admin resets.
- **Default login is LAN-only:** until the password has been changed, `admin/manifest` is refused from addresses outside your local network (private, loopback, Tailscale/CGNAT ranges). This stops a stranger who finds a freshly deployed public instance from claiming it. It relies on Manifest seeing the real visitor address: behind a reverse proxy you **must** set `TRUST_PROXY=true`. (`BOOTSTRAP_ANYWHERE=true` removes the restriction; don't.)
- **Lost the admin password?** Delete the users file and restart; the default login comes back (LAN only): `sudo docker compose exec manifest rm /data/users.json && sudo docker compose restart`. This deletes all users.
- Accounts live in `/data/users.json` in the `manifest-data` volume (passwords as scrypt hashes).

### Two-factor authentication (2FA)

Every user can turn on 2FA from the gear icon → **Two-factor authentication**: confirm your password, scan the QR code with an authenticator app (Google Authenticator, Authy, 1Password, Bitwarden...), enter the first 6-digit code, and save the **8 one-time recovery codes** (shown only once). From then on, signing in asks for the password and then a code.

- Standard TOTP (RFC 6238); a code can be used only once, and guessing is rate-limited per address and per user.
- A recovery code works in place of an app code and is used up. Turning 2FA off needs your password **and** a current code or recovery code.
- Lost the phone and the recovery codes? An administrator can use **Reset 2FA** next to the user in the Users list. If the only admin is locked out, delete `/data/users.json` (see above).
- The TOTP secret is stored in `users.json` so codes can be checked; protect the data volume and its backups like a password file.
- Users cannot enable 2FA while a forced password change is pending (they finish that first).

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_HOURS` | `168` | How long a login lasts |
| `TRUST_PROXY` | `false` | `true` behind a reverse proxy, so the real visitor address is used for login limits and the default-login restriction |
| `BOOTSTRAP_ANYWHERE` | `false` | Allow the default login from any address |
| `MANIFEST_SECRET_KEY` | auto | Session signing key; auto-generated and kept in the data volume |
| `AUTH_DISABLED` | `false` | `true` turns the login off entirely (LAN only!) |

Details: sessions are a signed, HttpOnly, SameSite=Strict cookie (Secure over HTTPS); 5 failed logins from one address block it for 15 minutes; changing or resetting a password signs that user out everywhere; cross-origin POSTs are rejected; security headers are sent.

### Before you put this on the internet

- **Change the default password first**, from your LAN, before exposing anything.
- **Use HTTPS.** Never publish plain `http://host:8080`: passwords would travel in clear text. Put Manifest behind a reverse proxy with a certificate (e.g. Nginx Proxy Manager + Let's Encrypt, or a Cloudflare Tunnel), publish only the proxy, set `TRUST_PROXY=true`, and don't port-forward 8080 directly. Make the proxy pass the `Host` header unchanged.
- **Consider whether it needs to be public at all.** A VPN (WireGuard/Tailscale) or Cloudflare Access in front of it is much safer than a password alone. This app holds a Portainer API token and can recreate containers.
- **Limit the damage:** use long unique passwords, create the Portainer token from a dedicated user with only the access it needs, give most people the Standard role, and set `ENABLE_UPDATES=false` if you don't need the Update button remotely.
- The page shows your container and image names to anyone who signs in.

## Dashboard widget (Homepage)

`GET /api/stats` returns only the counts (`total`, `outdated`, `up_to_date`, `unknown`, `offline_environments`, `refreshing`, `last_refresh`) for dashboards that can't sign in. It needs an API key in an `X-API-Key` header (or `Authorization: Bearer ...`); wrong keys are rate-limited and a key is never accepted in the URL. It stays off (404) until at least one key exists.

**Create a key in the app:** gear icon → **API keys** → name it (e.g. "Homepage") → **Create API key**, and copy it right away (only a hash is stored, so it can't be shown again). Each key belongs to the user who created it: it stops working if that user is deleted, an admin presses **Revoke keys** for them, or an admin resets their password. Users can revoke their own keys any time; up to 5 per user. A key can read the counts and nothing else, whatever role its owner has.

*Optional:* a single global key via `STATS_API_KEY` in `.env` (`openssl rand -hex 24`) also works, for setups with the login disabled.

Homepage `services.yaml`:

```yaml
- Manifest:
    icon: http://<manifest-host>:8080/static/logo.svg
    href: http://<manifest-host>:8080/
    description: Container update status
    siteMonitor: http://<manifest-host>:8080
    widget:
      type: customapi
      url: http://<manifest-host>:8080/api/stats
      refreshInterval: 60000
      headers:
        X-API-Key: "{{HOMEPAGE_VAR_MANIFEST_KEY}}"   # or paste the key; env var keeps it out of git
      mappings:
        - { field: outdated,   label: Outdated,   format: number }
        - { field: up_to_date, label: Up to date, format: number }
        - { field: unknown,    label: Unknown,    format: number }
        - { field: total,      label: Containers, format: number }
```

If you use the `{{HOMEPAGE_VAR_...}}` form, add `HOMEPAGE_VAR_MANIFEST_KEY=<the same key>` to the Homepage container's environment.

## Discord alerts

Manifest can post to a Discord channel when containers newly have an update.

1. In Discord: channel settings → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.
2. In Manifest click the **gear icon**, paste the URL, optionally fill in the Manifest address (so alerts link back here), and press **Save**, then **Send test**.

Settings are stored on the server in the `manifest-data` volume (`/data/settings.json`); the webhook URL is never sent back to the browser (only its last 4 characters are shown). Only real `https://discord.com/api/webhooks/...` URLs are accepted. Alternatively set `DISCORD_WEBHOOK_URL` / `PUBLIC_URL` in `.env` as defaults; anything saved in the dialog overrides them. Only signed-in users can change these settings.

Behavior: one message per refresh, grouped by machine. A container alerts once when it becomes outdated, and again only after it has been updated and a newer image appears. The first refresh after enabling alerts lists everything that is already outdated. Containers that become "unknown" (e.g. rate limited) do not trigger or reset alerts. Alert state is kept in the `manifest-data` volume, so restarts don't resend. If Discord is unreachable, Manifest retries at the next refresh.

## Updating containers

Rows with "Update available" have an **Update** button. It asks Portainer to pull the latest image and recreate the container (the same as Portainer's "Recreate" with "Pull latest image"), then re-checks it. Notes:

- The Portainer token's user needs rights to manage containers (a read-only user gets a clear error).
- It recreates the container with its existing settings, which works well for standalone and Compose containers. Containers that need a compose-file change to update should still be redeployed from their stack.
- Manifest refuses to update its own container.
- Only signed-in users can press Update. Set `ENABLE_UPDATES=false` to hide the button entirely.

## How update status is determined

1. Portainer's own per-container image status (`updated` / `outdated`) when its endpoint responds. The route isn't in the public docs for CE, so the app probes `/api/docker/{eid}/containers/{cid}/image_status` and `/api/endpoints/{eid}/docker/containers/{cid}/image_status` and uses whichever answers. The startup log says which, or that none did.
2. Otherwise (endpoint missing, or status `skipped`/`error`/`processing`/`preparing`), the local image's `RepoDigests` (read through Portainer) are compared to the registry's manifest digest (anonymous HEAD request, so Docker Hub pulls aren't consumed).

Shown as **unknown, with a reason** when: registry rate-limited (HTTP 429, that registry is then skipped until `Retry-After`), private image needing credentials, locally built image, image pinned by digest, or untagged image ID.

Offline environments appear as a banner; their last known rows are kept and the rest of the page is unaffected. Kubernetes environments are skipped.

## Notes

- Private registries are not supported (unknown with reason).
- A login is required (see above).
- Tests: `pip install pytest && pytest tests`
- Dev run: `PORTAINER_URL=... PORTAINER_API_TOKEN=... uvicorn app.main:app --port 8080`

## Versions and releases

Current version: **1.0.0** (shown at the bottom of the page and in the startup log). See [CHANGELOG.md](CHANGELOG.md) and the [Releases page](https://github.com/MrRobot-64/Manifest/releases).

**Cutting a release** (maintainers):

1. Choose the new number (e.g. `1.1.0`) and update it in three places: `app/version.py`, the `org.opencontainers.image.version` label in `Dockerfile`, and a new `## [1.1.0] - YYYY-MM-DD` section at the top of `CHANGELOG.md`.
2. Run `python -m pytest tests`. It fails if those three disagree.
3. Commit, tag and push:
   ```bash
   git commit -am "Release 1.1.0"
   git tag -a v1.1.0 -m "Manifest 1.1.0"
   git push origin main --tags
   ```
4. The GitHub Actions workflow then tests, builds and publishes the image (`1.1.0`, `1.1` and `latest`) and creates the GitHub release from the changelog. Nothing else to do.

## License

Manifest is released under the [MIT License](LICENSE). Its dependencies (FastAPI, Uvicorn, HTTPX, Segno) use MIT or BSD licenses.
