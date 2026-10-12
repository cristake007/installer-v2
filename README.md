# Docker infrastructure installer

A simple interactive workflow inspired by [EspoCRM's installer](https://github.com/espocrm/espocrm-installer), adapted to **PHP-FPM 8.4, PostgreSQL 18, Redis 8 and optional Nginx**. No application repository, application image, framework, migrations or deployment hooks.

## Install

Download **one file** and run it on Ubuntu 22.04/24.04 or Debian 12/13:

```sh
curl -fsSLo install.sh https://raw.githubusercontent.com/cristake007/installer-v2/feat/installer-core/install.sh
sudo bash install.sh
```

Python 3 is required. All installer code/templates are embedded in this script; no repository checkout is needed. The assistant asks for directory, web-server mode, domain/IP, HTTP/HTTPS and ports, then confirms installation. Docker can be installed from its official apt repository with your approval. Existing Docker installations are validated rather than replaced.

The default directory is `/opt/fluidweb`; any new directory under `/opt/` can be selected. Noninteractive examples:

```sh
sudo bash install.sh -y --web-mode nginx --domain example.com --http-port 8080
sudo bash install.sh -y --ssl --letsencrypt --domain example.com --email admin@example.com
sudo bash install.sh --web-mode nginx --tls-mode own --domain example.com \
  --tls-cert /path/fullchain.pem --tls-key /path/private-key.pem
sudo bash install.sh --web-mode external --fpm-port 9080 --install-dir /opt/my-stack
```

`-y` skips first-install prompts and permits installing missing Docker; it does **not** approve destructive administration. `--install-docker` explicitly enables Docker bootstrap; `--environment` checks/prepares Docker without creating a stack.

Official versioned images are resolved to digests and recorded. PostgreSQL and Redis have no published host ports. External mode binds FastCGI only to `127.0.0.1`; generated Nginx/Apache examples are in `config/`. You manage the external web server, TLS and firewall yourself. The installer does not modify them.

Reruns preserve credentials, settings and data, restart stopped services and check real health. Explicit `--domain`/HTTPS options on a rerun change web configuration with a safety backup. Unknown directories and old application-installer formats fail without modification. Interrupted creation with incomplete configuration requires inspection; no automatic destructive cleanup is performed.

## HTTPS and domain

Let's Encrypt uses Certbot in Docker and real HTTP-01 validation. DNS must point to this server and **public port 80 must reach this installation's Nginx**. HTTPS defaults to 443. When HTTPS is active, ordinary HTTP requests redirect to HTTPS; `/health` and ACME challenges remain available over HTTP.

Certificates and keys persist under `config/`. Automatic renewal runs in a small Certbot container every 12 hours; Nginx reloads hourly to pick up renewed certificates. Explicit renewal reloads immediately. Host crontab/systemd/web services are untouched.

```sh
sudo /opt/fluidweb/command.sh configure-web letsencrypt example.com admin@example.com
sudo /opt/fluidweb/command.sh configure-web own example.com /path/cert.pem /path/key.pem
sudo /opt/fluidweb/command.sh configure-web http example.com
sudo /opt/fluidweb/command.sh apply-domain new.example.com
sudo /opt/fluidweb/command.sh cert-install example.com /path/new-cert.pem /path/new-key.pem
sudo /opt/fluidweb/command.sh cert-generate
sudo /opt/fluidweb/command.sh cert-renew
sudo /opt/fluidweb/command.sh cert-cron-add
sudo /opt/fluidweb/command.sh cert-cron-remove
```

The `cert-cron-*` names match the familiar interface but enable/disable the **container scheduler**, not host cron. Own certificates must cover the configured domain/IP. For private ACME providers, advanced `--acme-server` and `--acme-ca` options accept an HTTPS directory and its CA PEM; no TLS verification bypass is used.

## Manage

Run `sudo /opt/fluidweb/command.sh` for the interactive administration menu. Direct commands also work:

```sh
sudo /opt/fluidweb/command.sh help
sudo /opt/fluidweb/command.sh start
sudo /opt/fluidweb/command.sh stop
sudo /opt/fluidweb/command.sh restart
sudo /opt/fluidweb/command.sh status
sudo /opt/fluidweb/command.sh health
sudo /opt/fluidweb/command.sh logs php
sudo /opt/fluidweb/command.sh restart redis
sudo /opt/fluidweb/command.sh version
sudo /opt/fluidweb/command.sh self-update
```

`self-update` downloads this repository's standalone installer and updates administration scripts only. From a newly downloaded script, `sudo bash install.sh --command --install-dir /opt/fluidweb` does the same. Neither replaces credentials/data nor restarts services.

PHP files live in `data/public/`; the initial `index.php` and `health.php` only report PHP 8.4. PHP extensions and application deployment are your responsibility. Edit `config/php.ini`, `config/php-fpm.conf` or `config/nginx.conf`, then run `command.sh apply-config`. Proposed configuration is validated before recreation. Generated `settings.json` and `compose.yaml` are managed files; don't edit them directly.

## Backup, restore and updates

```sh
sudo /opt/fluidweb/command.sh backup
sudo /opt/fluidweb/command.sh backup /mnt/backups
sudo /opt/fluidweb/command.sh restore /mnt/backups/TIMESTAMP
sudo /opt/fluidweb/command.sh upgrade
sudo /opt/fluidweb/command.sh update php php:8.4-fpm-alpine
```

Destructive operations ask for confirmation in a terminal. For automation, supply `--confirm PROJECT_ID` using the ID from `version`. Image upgrades stay within the installed version families and save a safety backup; failed upgrades restore previous images and data. Database major migrations are not automated.

Backups briefly stop the whole stack, verify clean PostgreSQL/Redis shutdown, archive data/settings/credentials and restart services. Archives have a SHA-256 manifest and private permissions. Copy the **whole timestamp directory**, including `stack.tar` and `manifest.json`, off-host. Retention is your responsibility. Links/special files are rejected except Certbot's checked internal certificate links. Custom PostgreSQL tablespaces are unsupported.

Restore validates the archive before changes and first saves a safety backup. To restore on a fresh compatible server/path:

```sh
sudo bash install.sh --restore /mnt/backups/TIMESTAMP --install-dir /opt/restored-stack \
  --http-port 8081 --install-docker
```

The destination must not exist. Credentials/data/images are preserved, project identity is adapted to the new path and ports can be overridden. Cold PostgreSQL backups require the same CPU architecture and PostgreSQL version family. DNS/firewall changes and stopping the old server remain operator tasks. Interrupted existing-install restores require inspection using retained safety backups; there are no recovery journals.

PostgreSQL SQL files can be transferred separately:

```sh
sudo /opt/fluidweb/command.sh export-sql /mnt/backups/database.sql
sudo /opt/fluidweb/command.sh export-table-sql /mnt/backups/table.sql public.my_table
sudo /opt/fluidweb/command.sh import-sql /mnt/backups/database.sql
```

SQL import replaces this installation's database, requires confirmation and makes a safety backup. Failed import restores the previous database/data. Dumps are private and transactional; export refuses to overwrite existing files.

## Tests

```sh
python3 -m unittest discover -s tests -v
sudo bash tests/run-integration.sh /opt/disposable-stack-test
```

CI has exactly two jobs: syntax/essential units and real Docker smoke tests. The suite exercises both web modes, persistence, backup/restore, distinct official-image changes, editable PHP configuration, SQL import failure rollback, fresh-path restoration, own HTTPS and actual ACME HTTP-01 issuance/renewal with the official Pebble test CA, plus installation/administration through a real terminal. It compares every pre-existing container's state/start time/restart count and removes only suite-owned containers. No private registry, simulated application images, application builds or mandatory VM runners.

`install.sh` is generated from the small modules/templates by `python3 tools/build-installer.py`. Unit tests enforce that its embedded payload matches the source files.

Public Let's Encrypt issuance needs operator-controlled DNS and reachability; local ACME tests don't establish public DNS readiness. Optional Docker apt bootstrap on clean hosts is not tested here. See `evidence/REPORT.md` for actual results and limits; no production-readiness claim is made.
