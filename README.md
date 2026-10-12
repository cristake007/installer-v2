# Standalone installer v2

This repository contains only installation and administration tooling. It has no dependency on an application source repository. The complete reference application is the PHP 8.4-FPM fixture under `tests/fixture`.

**Not production qualified.** Real Docker qualification and its logs are recorded in `evidence/REPORT.md`. Clean Ubuntu/Debian VM gates are mandatory and have not yet been run. No release or production readiness claim is made.

Ubuntu 22.04/24.04 and Debian 12/13 are supported. Python 3.10+, root, 2 GiB free disk, Docker Engine 24+ and Compose 2.20+ are required. `--install-docker` explicitly authorizes installation from Docker's fingerprint-verified official apt repository when Docker is absent. An existing broken Docker installation or unrelated container runtime is diagnosed rather than replaced. Apt refuses package removals and automatic restarts of unrelated services are suppressed. Preflight never installs packages or changes host services:

```sh
./install.sh --preflight
sudo ./install.sh --web-mode nginx --http-bind 127.0.0.1 --http-port 8080 \
  --app-image 'registry.example/application@sha256:<64-hex-digest>'
```

The default path is `/opt/fluidweb`; use `--install-dir /absolute/path` to change it. Application images must implement [the image interface](contracts/application.v1.json). They contain public assets, a `www-data` account, PHP 8.4-FPM on port 9000, a CLI health probe, HTTP health endpoint, compatibility probe and idempotent migration hook. No Symfony or other framework is assumed. Dependency images are pulled once and recorded as immutable digests. PostgreSQL 18 and Redis persist on private networks; external mode publishes FastCGI only on IPv4 loopback. Compose operations are scoped to the installation's unique project.

First installation initializes assets and image-specific data ownership, runs migrations, and checks database, Redis and application health. Reruns preserve settings/secrets/data, start stopped services, and verify actual health. Interrupted provisioning resumes with preserved credentials; corrupt or unknown configuration fails closed. Configuration integrity records detect edits or mismatches. Do not edit generated settings, Compose or `.env` directly. An interrupted destructive operation requires explicitly approved `recover` before further changes.

## Web server and TLS

Nginx runs in Docker by default. Ports and IPv4 binding are configurable. To use an operator-managed host Nginx/Apache, select `--web-mode external --fpm-port 9080`; rendered example vhosts are written under `config/`. The installer does not install, configure or reload host web servers or alter firewall rules. External health verifies loopback FastCGI but does not claim public HTTP readiness. Operators must restrict filesystem access, configure DNS/TLS and validate their vhosts.

Container TLS uses operator-provided PEM files:

```sh
sudo ./install.sh --web-mode nginx --app-image 'registry.example/application@sha256:<digest>' \
  --tls-cert /path/fullchain.pem --tls-key /path/key.pem --https-port 443
sudo /opt/fluidweb/command.sh renew-tls /path/new-fullchain.pem /path/new-key.pem
```

Keys are copied with private permissions; certificate/key matching is validated before loading. Renewal validates the pair, tests Nginx configuration and reloads it. Certificate issuance, expiry monitoring and scheduling renewal belong to the operator; automatic ACME issuance is not implemented. HTTP remains available alongside HTTPS.

## Administration

Run the installed `command.sh`. All operations share the installer lock:

```sh
sudo /opt/fluidweb/command.sh start
sudo /opt/fluidweb/command.sh stop
sudo /opt/fluidweb/command.sh restart
sudo /opt/fluidweb/command.sh status
sudo /opt/fluidweb/command.sh health
sudo /opt/fluidweb/command.sh logs app
sudo /opt/fluidweb/command.sh version
sudo /opt/fluidweb/command.sh backup
```

`status` prints container state; `health` probes functionality and exits nonzero on failure. Lifecycle commands verify health. Logs accept only known service names. Version reports the exact configured image digests. Errors exit 1; invalid CLI syntax exits 2. Secrets and backups are private; Docker/root access itself grants access to container environment credentials.

Backups stop application writers, create a transactional PostgreSQL custom-format dump, stop Redis to snapshot its persisted files, and archive public/upload/configuration files. SHA-256 checksums and safe archive inventory are validated. Services are restarted even if backup fails. Backups remain under `backups/`; copy them securely off-host and establish retention separately. Backup requires enough disk for the data snapshot and headroom. No automatic deletion is performed.

Restore and upgrade require independent explicit approval using the exact project ID printed by `version`:

```sh
sudo /opt/fluidweb/command.sh restore /opt/fluidweb/backups/<timestamp> --confirm fluidweb_<project-id>
sudo /opt/fluidweb/command.sh upgrade 'registry.example/application@sha256:<digest>' --confirm fluidweb_<project-id>
sudo /opt/fluidweb/command.sh recover --confirm fluidweb_<project-id>
```

Restore verifies both checksums and the PostgreSQL archive before replacing this installation's data; it also saves a safety backup of the current state. Backups from other projects are rejected. Restoration recreates and restores the application database, including all schemas, replaces application/Redis files, and verifies health. Upgrade runs an isolated compatibility hook, takes a validated backup, updates the pinned application image and tracked public assets, and runs the migration hook. A failed migration restores the old image, database and files. Operation journals allow explicit recovery after process interruption, including partially updated configuration. Persistent user uploads must not conflict with baked application asset names. Database/Redis major upgrades are not implicitly performed by application upgrades.

Destructive operations require a healthy installation to take the initial safety backup. If an operation is interrupted, retain all journals, backups and data and use `recover`. A completely lost installation root requires reconstructing the original project path/configuration from an off-host backup; automated relocation to a different project is not supported.

## Tests and release gates

```sh
python3 -m unittest discover -s tests -v
sudo bash tests/run-integration.sh /opt/installer-v2-disposable-test
```

The real integration suite builds its own good, upgraded and failing PHP-FPM images, pushes them to an isolated loopback registry and installs only immutable digests. It tests both modes, operator Nginx/Apache vhosts, HTTPS/reload, collisions, credentials, interruption, recreation persistence, logical database/file backup and restore, destructive migration failure recovery, outage recovery, corruption rejection and unrelated-container isolation. Only suite-owned projects/auxiliary containers are stopped/removed. Test installation data is retained.

CI enforces unit/schema/shell checks, Compose rendering and real Docker tests on Ubuntu 22.04/24.04 runners. Debian 12/13 container tests validate Python/OS compatibility; they do not establish VM installation readiness. The separate manually dispatched VM workflow requires operator-provided **clean disposable** self-hosted VMs with Python, Git, sudo and no Docker, labeled as specified in `.github/workflows/vm-acceptance.yml`. Its bootstrap and full integration gates fail on actual defects and retain logs. These VMs are not available in the current workspace.

Machine-readable requirements are in `contracts/installer.v1.json` and its schema. The original release gates remain required. See `AGENTS.md` for the execution and isolation contract.
