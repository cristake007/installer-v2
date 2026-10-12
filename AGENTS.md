# Installer execution rules

This repository installs Docker infrastructure only: official PHP-FPM 8.4, PostgreSQL 18, Redis and optional Nginx. No application repository or image is required. Keep installation and administration straightforward. The interactive flow is inspired by EspoCRM; single-file distribution, configurable backups, fresh-server restoration, PostgreSQL SQL transfer and container-managed HTTPS/renewal are in scope.

- Support Ubuntu LTS and Debian stable; optional official Docker installation needs explicit authorization. Never install/change host web servers or firewall rules.
- Preserve data, settings and credentials on rerun. Reject unexpected directories and unsupported old installation formats without modifying them.
- Scope Docker commands to the installation project and local daemon. Never prune Docker, use `down -v`, or stop/modify unrelated services.
- Use official versioned infrastructure images and record their resolved digests.
- Publish FastCGI only on loopback in external mode; PostgreSQL and Redis remain private.
- Restore/image updates need explicit project confirmation and a validated backup. Do not automate database major migrations.
- Keep contracts synchronized with behavior. Test essential safety and real Docker health, rerun, persistence, backup/restore and isolation.
- CI has exactly two jobs: syntax/essential units and real Docker smoke tests. No private test registry, simulated application images or mandatory VM runners.
- Record actual test evidence and limits honestly; successful container tests do not prove clean-host Docker bootstrap or production readiness.

Application-specific deployment logic and tests remain excluded. Certificate renewal must run inside the installation's containers, not alter host cron or host web servers. ACME tests must validate actual HTTP-01 challenges; never bypass CA validation or claim public DNS readiness from a local test CA. Git history retains previous evidence.
