# Changelog

All notable changes to Manifest are listed here.

## [1.0.0] - 2026-11-08

First release.

### Added
- **Dashboard:** one page listing every Docker container across all your Portainer environments, with machine,
  container, image:tag, state, update status and last-checked time. Summary counts, filter by machine and status,
  search, sortable columns, group by machine, light and dark themes, responsive layout.
- **Update detection:** compares each container's local image digest with the registry digest (anonymous, Docker Hub
  HEAD requests, GHCR, Quay and other token-auth registries). Uses Portainer's own image status when its endpoint is
  available. Recovers the original image name when a newer image has been pulled but the container was not
  recreated. Registry rate limits, private images and offline environments are shown as "unknown" with a reason and
  never break the page.
- **Background refresh** on an interval (default 6 hours) plus a manual Refresh button.
- **Update button:** pulls the latest image and recreates the container through Portainer, then re-checks it.
- **Discord alerts** when containers newly have an update, configured in Settings.
- **Accounts:** sign-in required. Default `admin` / `manifest` login that must be changed at first sign-in and is
  refused from outside the local network. Administrators and read-only standard users, user management in Settings,
  forced password change for new and reset users.
- **Two-factor authentication (TOTP)** with one-time recovery codes.
- **Per-user API keys** and a read-only `/api/stats` endpoint for dashboard widgets such as Homepage.
- **Security hardening:** scrypt password hashes, signed HttpOnly SameSite=Strict session cookies, login rate
  limiting, cross-origin request blocking, security headers, the Portainer token never sent to the browser.
- Dockerfile and docker-compose.yml, VM setup guide, and an automated test suite.

[1.0.0]: https://github.com/MrRobot-64/Manifest/releases/tag/v1.0.0
