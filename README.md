# Installer v2

Simple **Bash-only** infrastructure installer for Ubuntu and Debian. No Python, `whiptail`, application image or external application repository is needed.

Current implementation: **Nginx in Docker**. External web server mode is intentionally not implemented yet.

## Install

```bash
sudo bash install.sh
```

Use **Up/Down** to navigate, **Enter** to select, **Space** to toggle the SSL checkbox and **Esc** to go back. When running without a TTY, numeric menus are available.

The wizard asks for:

- Nginx in Docker
- Optional **SSL (Let's Encrypt)**. If checked, the wizard asks for a domain that resolves to the server's public IPv4 address. Inbound **TCP 80 and 443** must be reachable from the internet for certificate issuance; DNS resolution by itself is not proof of correct routing.
- Public HTTP port (default **80**, customizable *only when SSL is disabled*)
- Installation directory (default `/opt/fluidweb`)
- Final confirmation before modifying the host

Nginx binds explicitly to **`0.0.0.0:80`** (or the selected public port). With SSL enabled, it also binds **`0.0.0.0:443`**. These are **public interface** bindings, not `127.0.0.1`. Host/firewall/provider firewall settings must still permit traffic.

The installer checks for root, Ubuntu/Debian, port conflicts, existing files, Docker Engine and Compose. If Docker is missing, it requests permission before using Docker's official convenience installation script. It does not stop other Docker applications.

## Services

- PHP-FPM 8.4 (`php:8.4-fpm`)
- PostgreSQL 18 (`postgres:18`)
- Redis 8 (`redis:8`), append-only persistence
- Nginx (`nginx:stable`)
- Certbot (`certbot/certbot:latest`, used **only** when SSL is selected)

The installer creates a new installation at `/opt/fluidweb` by default. An existing `/opt/infra-stack` deployment is **not** renamed or migrated automatically; stop it before a fresh test if its public HTTP/HTTPS ports are occupied.

A minimal `public/index.php` returns `Installer v2: PHP-FPM OK` to verify Nginx → PHP-FPM connectivity. **This is an infrastructure check, not a deployed application.** PostgreSQL and Redis have no public ports. PostgreSQL data lives in a Docker-managed **named volume** (postgres_data within the installation's Compose project); Redis still uses data/redis/ in the installation directory. These persist when containers are recreated. Do not run docker compose down -v or docker volume prune if you want to retain the database. PHP database/Redis extensions for a future application are not configured yet.

Rerunning the wizard with the **same** directory, port, domain and SSL option preserves passwords and data, repairs the PostgreSQL 18 Alpine bind-mount directory ownership when it is root-owned, and starts/verifies the services again. No recursive ownership changes or data deletion are performed. It refuses an unrecognized non-empty installation directory. To add SSL to an existing HTTP installation on **public port 80**, rerun the wizard, check SSL and enter the same installation directory; it preserves the database and credentials. Older failed installations using an **empty** PostgreSQL bind mount are switched automatically to a named volume on rerun; if the old mount contains data, the installer stops without altering it. SSL activation is tested with a simulated Docker backend; a real Let's Encrypt issuance is still to be tested on a VPS.

## Management

After installation:

```bash
sudo /opt/fluidweb/command.sh status
sudo /opt/fluidweb/command.sh logs
sudo /opt/fluidweb/command.sh stop
sudo /opt/fluidweb/command.sh start
sudo /opt/fluidweb/command.sh restart
```

Replace `/opt/fluidweb` with your chosen directory.

**SSL checkbox:** highlight "Enable SSL", press **Space** so `[x]` appears, then select **Continue**. The domain prompt appears only when SSL is enabled. Certificate issuance runs **after PostgreSQL, Redis, PHP-FPM and Nginx are healthy**. If the summary shows `SSL: off`, Let's Encrypt will not run.

**SSL renewal is not yet automatic.** Until renewal scheduling is added, periodically run:

```bash
sudo /opt/fluidweb/command.sh renew-cert
```

This renews eligible Let's Encrypt certificates and reloads Nginx. Do not leave certificate renewal unattended in a long-running production installation.

## Testing status

Bash syntax, interactive menu navigation, checkbox behavior, generated Compose/Nginx configurations, and HTTP/SSL installation paths have been checked locally **using simulated Docker commands**. Rerunning an installation was checked for preservation of credentials. Real Docker installation and real Let's Encrypt certificate issuance must still be tested on a suitable VPS.