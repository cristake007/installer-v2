# FluidWeb installer-v2

Standalone installation and operations tooling for [FluidWeb](https://github.com/cristake007/FluidWeb). No application code is maintained here.

**Status: ALPHA / NOT PRODUCTION READY.** A first working code slice has been added with unit tests and CI. End-to-end real-server support, backup/restore, upgrades, complete HTTPS and recovery qualification are **not yet implemented or verified**. Do not run this against a production server.

## Architecture

Ubuntu LTS / Debian stable; Docker Engine + Compose; FluidWeb image (PHP 8.4-FPM) plus PostgreSQL 18 and Redis. Nginx is an optional Docker gateway. Choosing `external` publishes only PHP-FPM to host loopback and provides example Nginx and Apache vhosts; no host web-server configuration is changed.

The install path defaults to `/opt/fluidweb`, can be changed with `--install-dir`, and holds Compose, protected secrets, settings and bind-mounted persistent data. Application image must be specified by an **immutable digest**. Other images are pulled and resolved to immutable digests before deployment. No FluidWeb release image exists yet, so a genuine validated release digest is required before an end-to-end install can succeed.

## Commands

Read-only host preflight (no Docker packages installed):

```bash
./install.sh --preflight
```

Example install **only on a disposable test server** with a real published FluidWeb release digest:

```bash
sudo ./install.sh --web-mode nginx --http-port 8080 \
  --app-image 'ghcr.io/cristake007/fluidweb@sha256:<REAL_64_HEX_DIGEST>'
```

For host Nginx or Apache, select `--web-mode external --fpm-port 9080`. In this mode the operator configures the host web server from `/opt/fluidweb/config/*.conf.example`; no automatic changes to the host web server are made.

If Docker is absent, setup stops with a clear error by default. `--install-docker` explicitly authorizes bootstrap from Docker's official apt repository. Host changes are **never** performed during `--preflight`.

Rerunning the installer against a complete existing install is a no-op. If a previous attempt generated valid settings but did not finish, it resumes with existing credentials and volumes. Unknown/partial configuration fails safely, never silently wipes state. File/port conflicts fail before provisioning. An exclusive operation lock prevents concurrent installs.

Currently installed `command.sh` supports `status`, `start`, `stop`, `restart`, `logs`, `version`. Backup, restore, upgrades, stronger live verification and disaster recovery are **release blockers**, not silently claimed capabilities.

## CI and verification

- Python stdlib unit tests exercise OS support, preflight invariants, immutable images, app/port modes, persistent layout, rerun/no-op and interrupted install resume.
- GitHub Actions checks shell syntax, Python tests on Ubuntu 22.04/24.04, Debian 12/13 containers and Compose config on a Docker-capable Ubuntu runner. The Compose job validates rendering only; it is **not** an end-to-end install.
- No fake green status for live Ubuntu/Debian tests. The release readiness contract requires those tests separately and they are not yet satisfied.

Machine-readable requirements: `contracts/installer.v1.json` and `contracts/installer.schema.json`. Human/agent guardrails: `AGENTS.md`.

## Release blockers / not done

- Real published FluidWeb release image with fully tested PHP-FPM+PostgreSQL boot and reliable health endpoint.
- Clean server Docker auto-install tested on Ubuntu and Debian **virtual machines**.
- Host-Nginx and host-Apache external mode tested with real FastCGI and static assets.
- Tested data ownership for PostgreSQL/Redis storage bind mounts, lifecycle restart and replacement containers.
- Secure consistent backup, verified restore, safe upgrade and migration-aware rollback.
- Robust test coverage for outages, interrupted database migrations, permission failures and disk exhaustion.
- Managed HTTPS / Let's Encrypt certificate flow if included in the final product.

Until these gates are resolved, this is a development implementation and no production deployment should be attempted.
