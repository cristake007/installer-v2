# FluidWeb installer

Bash + Docker Compose installer for the actual FluidWeb application on Ubuntu and Debian. FluidWeb is the only supported application. No application checkout, Composer installation, new Symfony project or locally built application image is needed.

```bash
sudo bash install.sh
```

Use Up/Down and Enter to navigate, Space to toggle SSL, and Esc to go back. Numeric menus are available without a TTY. The default installation directory is `/opt/fluidweb`.

The installer requires root, checks ports and existing files, and checks Docker Engine and Compose. If Docker is missing, it asks before using Docker's official convenience installation script. It does not stop unrelated containers or change host web servers or firewall rules.

## Application and storage

Fresh installations pull the existing production image `ghcr.io/cristake007/fluidweb:dev` and save its resolved digest in `.env`. Reruns use that digest and preserve the generated `APP_SECRET`, database password and Compose project name. Authenticate Docker to GHCR with package read access if the image is private. A failed application pull stops before writing installation files; there is no test-page fallback.

The `php` service runs FluidWeb's production entrypoint, which publishes its public files, bootstraps the platform schema and clears/warms the Symfony cache before starting PHP-FPM. The installer supplies production environment settings, PostgreSQL variables and `DATABASE_URL`, and waits for the application's database and FPM health checks. Open `/setup` to create the first administrator using FluidWeb's own setup form.

Services:

- FluidWeb production image, including PHP-FPM and its PostgreSQL driver.
- PostgreSQL 18 (`postgres:18`), without published ports.
- Redis 8 (`redis:8`), without published ports and with append-only persistence.
- Nginx (`nginx:stable`), in Docker Nginx mode.
- Certbot (`certbot/certbot:latest`), under the `tools` profile, only for SSL operations.

PostgreSQL uses `postgres_data:/var/lib/postgresql`, as required by PostgreSQL 18. Installation-local storage includes:

- `data/redis`: Redis persistence.
- `data/application`: application data, uploaded files and extensions.
- `data/custom`: custom application metadata.
- `data/var`: cache, logs and persistent PHP sessions.
- `public`: image-owned public files shared with the web server. FluidWeb replaces these on startup; do not store custom persistent files here.

Do not run `docker compose down -v` or prune volumes if you want to retain the database. Secrets and application data remain outside the public document root.

**Redis limitation:** the inspected FluidWeb production source has no Redis extension or Composer client and no Redis cache/session integration. The installer supplies `REDIS_URL` and verifies Redis PING from the application container using PHP streams. This proves network connectivity; it does not make FluidWeb use Redis. PHP sessions persist in `data/var/sessions`. Actual Redis-backed application features require a change in FluidWeb, which this installer task does not make.

## Nginx in Docker and SSL

Docker Nginx binds publicly to `0.0.0.0:80`. The SSL checkbox adds public port 443 and prompts for a domain resolving to the server. Inbound TCP 80 and 443 must be reachable for Let's Encrypt issuance. DNS resolution alone does not prove routing.

The existing Nginx configuration and Certbot webroot flow are retained. Certificate issuance starts after FluidWeb and its HTTP login/setup page are verified. HTTPS verification requests the real application login/setup page. A failed issuance leaves HTTP running and permits retrying the domain.

Rerunning with the same directory and options preserves configuration, credentials and storage. Enabling SSL on an existing FluidWeb HTTP installation on public port 80 is supported. Different ports/domains, mode conversion, infrastructure-only test-page installations and legacy PostgreSQL bind mounts are refused without converting or deleting data. Clear previous test deployments yourself before a fresh installation.

Certificate renewal is not automatic. Periodically run:

```bash
sudo /opt/fluidweb/command.sh renew-cert
```

## External Nginx or Apache

Select External web server and choose a local PHP-FPM port (default 9000). The installer publishes FPM exclusively on `127.0.0.1` and runs only FluidWeb, PostgreSQL and Redis.

Examples are generated at `config/nginx-external.conf` and `config/apache-external.conf`. Copy the appropriate example to your host configuration, set its server name and manage SSL yourself. Apache requires `proxy` and `proxy_fcgi`; its template includes `FallbackResource /index.php` for Symfony routes. The installer does not install or control a host web server.

Both templates retain the installation's absolute `public` path. Inside the application container, a path link maps that location to `/var/www/html/public`, then invokes FluidWeb's original entrypoint. This keeps FastCGI filenames valid without moving or duplicating the application. Custom parent directories must permit traversal by the host web server. External paths may contain letters, numbers, spaces, dots, slashes, hyphens and underscores.

Reruns preserve the selected FPM port and generated examples. External HTTP requests require your configured host web server and are not verified by the installer.

## Management

```bash
sudo /opt/fluidweb/command.sh status
sudo /opt/fluidweb/command.sh logs
sudo /opt/fluidweb/command.sh stop
sudo /opt/fluidweb/command.sh start
sudo /opt/fluidweb/command.sh restart
```

Replace `/opt/fluidweb` with your chosen directory. Upgrade and clean-slate commands are outside this task.

## Backup and restore

Install the updated management script into an existing deployment without running the installer again:

```bash
# From the installer-v2 repository on feat/installer-core:
sudo install -m 700 command.sh /opt/fluidweb/command.sh
```

This replaces only the management script. It does not restart the application, change configuration or reset data. Copy it while no management command is running.

```bash
sudo /opt/fluidweb/command.sh backup
sudo /opt/fluidweb/command.sh restore /opt/fluidweb-backups/fluidweb-TIMESTAMP-ID.tar.gz
```

Backups go to the directory alongside the installation, `${installation_directory}-backups` (normally `/opt/fluidweb-backups`). The directory has mode 700 and archives have mode 600. Each timestamped archive contains a PostgreSQL custom-format logical dump, all `data` directories (including Redis, application files, uploads, custom files and sessions), public files, configuration, secrets, Compose settings, the pinned FluidWeb image reference and `command.sh`. Docker-managed certificates and Certbot renewal configuration are included when present. Certbot's relative certificate links are preserved.

Start the installation before backing it up. Backup briefly stops Docker Nginx, PHP-FPM and Redis, then takes the database dump and copies the stopped services' files. PostgreSQL remains running; its physical data directory is never archived. Redis shuts down cleanly to flush its append-only data. Services restart and are verified before success is reported. A failure attempts to restart the paused services. Do not write directly to PostgreSQL/Redis or edit installation files during a backup. Free disk space must accommodate the temporary copy and compressed archive. Internal custom symbolic links are copied as their contents; links outside the installation and special files are refused.

Restore validates gzip/archive structure, file checksums, certificate links, the full PostgreSQL dump, original directory, project identity, mode and Compose configuration before touching installation data. It checks that the pinned application image is available. Type **RESTORE** at the confirmation prompt to replace this instance's data and configuration. The restored database role password comes from the backed-up environment. Restore starts the services, checks Symfony and database/Redis readiness, and checks the application's login page through Docker Nginx. It leaves the application running.

A restore removes this project's containers and recreates them while retaining its PostgreSQL named volume; it replaces the application's database using the logical dump. Existing tables and files added after the backup are removed. It never removes unrelated containers or volumes and never runs a prune command. Concurrent mutations through `command.sh` are rejected; status and logs remain available.

Restore requires the original installation directory and, for an existing deployment, the same Compose project and web-server mode. It does not relocate external FastCGI paths or convert modes. In external mode, application readiness is verified inside the containers; host web-server configuration and host-managed certificates are outside this backup. Restore the host server separately using the saved example configurations.

To recover on a replacement host, prepare Docker Engine, Compose and the usual command-line utilities, create the **original directory**, and install this `command.sh` there. The directory must contain only `command.sh`:

```bash
sudo mkdir -p /opt/fluidweb
sudo install -m 700 command.sh /opt/fluidweb/command.sh
sudo /opt/fluidweb/command.sh restore /path/to/backup.tar.gz
```

Only restore trusted backups: checksums detect damage, but do not authenticate an archive containing Compose configuration and secrets. Keep a copy off the VPS yourself. An expired SSL certificate can be recovered, but HTTPS verification will fail until it is renewed. If restore fails after replacement starts, services may remain stopped; inspect the error and logs, fix the cause, and retry the same validated backup. There is no automatic rollback or database-major-version migration.

### Backup/restore verification

Executed on the disposable VPS on 2026-10-13 with Docker Nginx over HTTP:

- Created identifiable PostgreSQL, Redis and application/custom file values and a backup; changed those values, sessions, database password, application secret, image-reference setting and configuration; restored the backup and verified the original values and configuration hashes.
- Verified removal of a table, Redis key and file created after the backup, plus restoration of session contents and certificate files with their relative links. Certificate tests used self-signed test files; no public SSL issuance was tested.
- Verified successful Symfony/schema/cache startup, database authentication, Redis connectivity and the actual FluidWeb login page (HTTP 200).
- Verified unrelated container IDs/states and every pre-existing volume name/label remained unchanged.
- Corrupt gzip, checksum mismatch, declined confirmation and concurrent management commands were rejected before replacing installation data.

External-mode backup/restore, replacement-host recovery, interrupted restores and expired/real Let's Encrypt certificates have not had end-to-end runtime tests. Generated external Compose configuration still passes the existing configuration check.

## Validation for this change

Run the small configuration check with an existing Docker Compose plugin:

```bash
bash tests/config.sh
```

Executed on 2026-10-13:

- Bash syntax and real `docker compose config -q` for HTTP, SSL and external mode, including an external path with spaces.
- Generated settings preservation during rerun validation; refusal of infrastructure-only installations, mode changes and legacy PostgreSQL bind mounts.
- Docker Nginx SSL configuration syntax with a temporary self-signed certificate; external Nginx and Apache configuration syntax in isolated containers, without binding host ports. These checks do not establish application HTTP execution or Let's Encrypt issuance.
- After the package was made public, a real fresh HTTP installation at `/opt/fluidweb` passed: service startup, Symfony bootstrap, application/setup HTTP responses, database and Redis connectivity, container-recreation persistence and installer reruns. Credentials, configuration, application files and sessions survived recreation. Unrelated containers remained unchanged.

**Not executed:** external host web-server runtime tests, clean-host Docker bootstrap, or public Let's Encrypt issuance/renewal. Automated checks used localhost and self-signed certificate files, without requiring a public domain. Backup/restore results and limitations are listed above.

Source inspected: FluidWeb commit `61826ed8f1e00f4c7aa6e5898f5cf150c2a3583f`, including its production Dockerfile, entrypoint, production Compose template, application configuration and setup controller. The FluidWeb repository was not modified.
