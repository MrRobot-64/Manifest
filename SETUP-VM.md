# Setting up Manifest on a VM

A step-by-step guide for running Manifest on a fresh Linux VM (written for Ubuntu 22.04/24.04 or Debian 12; works the same on a Proxmox VM or any hypervisor). It only needs network access to your Portainer server and to the internet (for registry digest checks).

## 1. VM requirements

- 1 vCPU, 512 MB RAM (1 GB is comfortable), 4 GB disk
- Network access to `PORTAINER_URL` (check the port, usually 9443 or 9000)
- A static IP or DHCP reservation, so the URL doesn't change

Manifest does not need access to the Docker socket on this VM or on your other machines. It uses only the Portainer API.

## 2. Install Docker

```bash
sudo apt update && sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in (so the group applies), then confirm:

```bash
docker run --rm hello-world
docker compose version
```

## 3. Get the files onto the VM

Make a folder called `manifest` and download the compose file and settings template into it. Keep that folder name: Docker Compose names the data volume after the folder, so a different name would start with an empty data volume.

```bash
mkdir ~/manifest && cd ~/manifest
curl -O https://raw.githubusercontent.com/MrRobot-64/Manifest/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/MrRobot-64/Manifest/main/.env.example
```

Docker pulls the ready-made image (`ghcr.io/mrrobot-64/manifest`) in step 6, so nothing is built on the VM.

*Prefer to build from source?* Clone the repo instead (`git clone https://github.com/MrRobot-64/Manifest.git ~/manifest && cd ~/manifest`) and use `docker compose up -d --build` in step 6 and `git pull && docker compose up -d --build` for updates.

## 4. Create a Portainer API token

In Portainer: click your username (top right) → **My account** → **Access tokens** → **Add access token**. Copy the `ptr_...` value now; it is shown once.

Tip: create a dedicated user with read-only access to your environments and use its token, since a token has all the rights of its user.

## 5. Configure

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Set at least:

```
PORTAINER_URL=https://<portainer-host>:9443
PORTAINER_API_TOKEN=ptr_xxxxxxxx
PORTAINER_SKIP_TLS_VERIFY=true   # only if Portainer uses a self-signed cert
REFRESH_INTERVAL_HOURS=6
```

Quick check that the VM can reach Portainer and the token works:

```bash
curl -k -H "X-API-Key: <token>" https://<portainer-host>:9443/api/endpoints | head -c 300
```

You should see JSON listing your environments. A `401` means a bad token; a timeout means a network or firewall problem.

## 6. Start it

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

Open `http://<vm-ip>:8080` and sign in with username `admin` and password `manifest`; you will be asked to choose a new password straight away. The first scan starts immediately; the page shows "Refreshing…" until it finishes. The logs say which Portainer image-status route was used, or that it fell back to registry digest comparison.

The container has `restart: unless-stopped`, so it comes back after VM reboots as long as the Docker service starts on boot (it does by default; check with `systemctl is-enabled docker`).

## 7. Limit who can reach the port (recommended)

Manifest has its own login, but port 8080 speaks plain HTTP, so passwords and sessions are not encrypted on the wire. Keep that port on your LAN, and if you want access from the internet put an HTTPS reverse proxy in front instead of opening 8080.

**LAN only:** allow just your subnet to reach the port:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp   # adjust to your subnet
sudo ufw allow OpenSSH
sudo ufw enable
```

Docker-published ports can bypass ufw rules. For strict control, change the compose port to `"<vm-lan-ip>:8080:8080"` or filter at your router.

**From the internet:** use a reverse proxy with a certificate (for example Nginx Proxy Manager with Let's Encrypt, or a Cloudflare Tunnel), forward only the proxy's ports, and then:

1. Set `TRUST_PROXY=true` in `.env` so Manifest sees each visitor's real address (needed for login rate limits and the LAN-only default login).
2. Make the proxy pass the `Host` header through unchanged.
3. Change the default `admin` password first, from your LAN, and consider turning on 2FA.
4. Restrict port 8080 to the proxy's address only, so nobody can bypass HTTPS.

See "Before you put this on the internet" in the README for the full checklist.

## 8. Day-to-day

| Task | Command |
|---|---|
| View logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Change config | edit `.env`, then `docker compose up -d` |
| Update the app | `docker compose pull && docker compose up -d` |
| Stop and remove | `docker compose down` |

## Troubleshooting

- **Banner "Cannot reach Portainer"**: wrong `PORTAINER_URL`, firewall, or a TLS error. For self-signed certs set `PORTAINER_SKIP_TLS_VERIFY=true`.
- **Token rejected (401/403)**: regenerate the token; make sure the user has access to the environments.
- **A machine shows as offline**: Portainer reports it down or its agent is unreachable. Other machines are unaffected, and its last known rows stay visible.
- **Lots of "unknown"**: hover or read the reason under the badge. Common causes are registry rate limits (they clear on their own; lower the refresh frequency), private registries, and locally built images.
- **Everything "unknown" with a "Portainer image status unavailable" reason**: expected on editions without the status route; the registry fallback then covers public images.
- **Wrong time in "Last checked"**: times are shown in your browser's timezone.
- **"The default login only works from your local network"**: until the `admin` password has been changed, `admin` / `manifest` is refused from outside your LAN. Sign in from a LAN address first and choose a new password. Behind a reverse proxy this needs `TRUST_PROXY=true`.
- **"Too many failed attempts"**: 5 wrong passwords or codes from one address blocks it for 15 minutes.
- **Locked out (forgot the admin password, or lost the only admin's 2FA device and recovery codes)**: delete the users file and restart. The default login comes back and all users are removed: `docker compose exec manifest rm /data/users.json && docker compose restart`.
- **Logged out after every restart**: the session key could not be saved. Make sure the `manifest-data` volume is present (`docker volume ls`) and that `docker-compose.yml` still mounts it at `/data`.
