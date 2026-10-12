# Simple Docker infrastructure installer

Installs **PHP-FPM 8.4, PostgreSQL 18, Redis 8 and optionally Nginx**. No application repository, application image, framework, migrations or deployment hooks.

On Ubuntu 22.04/24.04 or Debian 12/13, with Python 3 and root access:

```sh
sudo bash install.sh
```

Answer three questions: installation directory, Nginx container or external web server, and port. The default directory is `/opt/fluidweb`. If Docker is missing, the installer asks whether to install it from Docker's official apt repository. Existing broken Docker installations are diagnosed rather than replaced. For unattended installation:

```sh
sudo bash install.sh --web-mode nginx --http-port 8080
sudo bash install.sh --web-mode external --fpm-port 9080 --install-dir /opt/my-stack
# Explicitly permit Docker installation if it is missing:
sudo bash install.sh --install-docker --web-mode nginx
```

Use `--http-bind 127.0.0.1` for local HTTP, or keep the default `0.0.0.0`. External mode binds FastCGI only to loopback and writes Nginx/Apache example vhosts in `config/`. You manage the host web server, firewall and HTTPS; this script does not alter them. The examples serve the shared document root and forward PHP requests to loopback FastCGI.

Official versioned images are resolved once to digests. PostgreSQL and Redis have no published host ports. Data is under `data/`; PostgreSQL credentials are in the private `.env` file. PHP files go in `data/public/`. The initial `index.php` and `health.php` only report the PHP version. PHP extensions/application deployment are your responsibility.

Rerunning starts stopped services and checks real PostgreSQL, Redis and PHP-FPM health, preserving credentials, settings and data. Unknown directories and old application-installer formats are rejected without modification. A failed service startup can be retried by rerunning the installer; incomplete configuration requires operator inspection. There is no automatic migration of old installations.

## Manage the stack

```sh
sudo /opt/fluidweb/command.sh start
sudo /opt/fluidweb/command.sh stop
sudo /opt/fluidweb/command.sh restart
sudo /opt/fluidweb/command.sh status
sudo /opt/fluidweb/command.sh health
sudo /opt/fluidweb/command.sh logs php
sudo /opt/fluidweb/command.sh version
sudo /opt/fluidweb/command.sh backup
```

Backups briefly stop the **entire stack** and archive its persistent data, settings and credentials after clean PostgreSQL/Redis shutdown. Services restart afterwards. The archive has a SHA-256 manifest and private permissions. Copy backups off-host; retention is your responsibility. Backups reject links/special files and custom PostgreSQL tablespaces are not supported.

Restore/update require the project ID printed by `version`:

```sh
sudo /opt/fluidweb/command.sh restore /opt/fluidweb/backups/TIMESTAMP --confirm fluidweb_PROJECT
sudo /opt/fluidweb/command.sh update php php:8.4-fpm-alpine --confirm fluidweb_PROJECT
sudo /opt/fluidweb/command.sh update postgres postgres:18-alpine --confirm fluidweb_PROJECT
sudo /opt/fluidweb/command.sh update redis redis:8-alpine --confirm fluidweb_PROJECT
sudo /opt/fluidweb/command.sh update nginx nginx:1.28-alpine --confirm fluidweb_PROJECT
```

Restore validates the backup before changing data and saves a safety backup. Backups restore to the original installation path/project. Updates pull an official versioned image within the installed family, take a backup, recreate services and check health; failed updates restore the previous images and data. PostgreSQL major-version migration is deliberately not automated. An interrupted restore requires operator inspection and the retained safety backup; there are no recovery journals. Do not edit generated `settings.json` or `compose.yaml` directly.

All Docker operations use the local daemon and the installation's project. There is no global cleanup; unrelated containers, volumes and networks are untouched.

## Tests

```sh
python3 -m unittest discover -s tests -v
sudo bash tests/run-integration.sh /opt/disposable-stack-test
```

The smoke suite uses official images without building applications or running a test registry. It checks both web modes, health, rerun, stopped-service recovery, recreation persistence, database/file/Redis backup and restore, updates, corrupt-backup and major-update rejection, port conflicts and unrelated-container isolation. It removes only its own containers and retains data.

CI has exactly two jobs: syntax/essential unit checks and real Docker smoke tests. No self-hosted VM workflow or release gate is required. Supported-OS checks and Docker bootstrap code remain, but clean-host apt bootstrap has not been exercised here; no production-readiness claim is made. See `evidence/REPORT.md` for current test results.
