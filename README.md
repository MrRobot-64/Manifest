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

**3. Edit `.env`:** set `PORTAINER_URL` and `PORTAINER_API_TOKEN` (everything else is optional; see [Configuration](https://github.com/MrRobot-64/Manifest/wiki/Configuration)).

**4. Start it:**

```bash
docker compose pull
docker compose up -d
```

**5. Open `http://<host>:8080`** and sign in with `admin` / `manifest`. You are asked to choose a new password straight away.

To update later: `docker compose pull && docker compose up -d`. To pin a version, change the image tag in `docker-compose.yml` to e.g. `:1.0.0`. To build from source instead, clone the repo and run `docker compose up -d --build`.

Setting it up on a fresh VM? See [SETUP-VM.md](SETUP-VM.md).

## Documentation

Full documentation is in the [wiki](https://github.com/MrRobot-64/Manifest/wiki):

- [Configuration](https://github.com/MrRobot-64/Manifest/wiki/Configuration): all environment variables
- [Login and Security](https://github.com/MrRobot-64/Manifest/wiki/Login-and-Security): users, roles, two-factor authentication, and what to do before exposing Manifest to the internet
- [Homepage Widget](https://github.com/MrRobot-64/Manifest/wiki/Homepage-Widget): show counts on a Homepage dashboard
- [Discord Alerts](https://github.com/MrRobot-64/Manifest/wiki/Discord-Alerts)
- [Updating Containers](https://github.com/MrRobot-64/Manifest/wiki/Updating-Containers): the Update button
- [How Update Status Works](https://github.com/MrRobot-64/Manifest/wiki/How-Update-Status-Works)
- [Development](https://github.com/MrRobot-64/Manifest/wiki/Development): tests and running from source

**Exposing Manifest to the internet?** Read [Login and Security](https://github.com/MrRobot-64/Manifest/wiki/Login-and-Security) first: use HTTPS and change the default password.

## Releases

Current version: **1.0.0**. See the [Releases page](https://github.com/MrRobot-64/Manifest/releases) and [CHANGELOG.md](CHANGELOG.md). Maintainers: see [Releasing](https://github.com/MrRobot-64/Manifest/wiki/Releasing).

## License

Manifest is released under the [MIT License](LICENSE). Its dependencies (FastAPI, Uvicorn, HTTPX, Segno) use MIT or BSD licenses.
