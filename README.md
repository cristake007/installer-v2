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

Replace `/opt/fluidweb` with your chosen directory. Upgrade, backup, restore and clean-slate commands are outside this task.

## Validation for this change

Run the small configuration check with an existing Docker Compose plugin:

```bash
bash tests/config.sh
```

Executed on 2026-10-13:

- Bash syntax and real `docker compose config -q` for HTTP, SSL and external mode, including an external path with spaces.
- Generated settings preservation during rerun validation; refusal of infrastructure-only installations, mode changes and legacy PostgreSQL bind mounts.
- Docker Nginx SSL configuration syntax with a temporary self-signed certificate; external Nginx and Apache configuration syntax in isolated containers, without binding host ports. These checks do not establish application HTTP execution or Let's Encrypt issuance.
- The actual fresh-install wizard was run against a new directory; the image pull returned `unauthorized` for `ghcr.io/cristake007/fluidweb:dev`. The wizard exited with an explicit error and created no installation files. This prevents an actual fresh application deployment in this environment.

**Not executed:** actual FluidWeb startup, application database/Redis connectivity, HTTP responses through either web server, container-recreation persistence or full runtime installer reruns. These require pull access to the published image. After access is available, verify `/setup`, database schema, Redis PING from `php`, and records/files/sessions after recreating the installation containers. Test both external templates with host-managed web servers on a suitable VPS. Clean-host Docker bootstrap and public Let's Encrypt issuance/renewal also require VPS verification; automated checks do not need a public domain or real certificate issuance.

Source inspected: FluidWeb commit `61826ed8f1e00f4c7aa6e5898f5cf150c2a3583f`, including its production Dockerfile, entrypoint, production Compose template, application configuration and setup controller. The FluidWeb repository was not modified.
