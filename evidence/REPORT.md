# Current implementation evidence

The current revision adds a standalone interactive installer, an administration menu, configurable HTTP/HTTPS, certificate issuance and renewal, SQL operations, configuration application, tooling updates and fresh-path restoration to the basic PHP-FPM 8.4/PostgreSQL 18/Redis/Nginx stack. No application repository is required.

Before publication was requested, twelve essential unit tests passed, Bash syntax and Python compilation passed, and the JSON contract validated against its schema. The focused real Docker suite passed actual ACME HTTP-01 issuance and forced renewal, renewal-container management, certificate backup/restoration, real terminal installation and administration, and isolation from all pre-existing containers on the shared Debian 13 host.

The subsequent complete combined Docker run was interrupted at the operator’s explicit request to publish without running further tests. It is not reported as passing. No CI result is claimed for this revision; publication uses a skip-CI commit message in accordance with that request. Optional Docker installation on a clean host and public Let’s Encrypt/DNS provisioning remain unverified. This is not a production-readiness claim.

## Historical evidence for the previously published simplified revision

# Simplified infrastructure installer evidence

Scope was explicitly reduced by the operator: PHP-FPM 8.4, PostgreSQL 18, Redis and optional Nginx only. Application image contracts, assets/deployment logic, migration/compatibility hooks, test registries, simulated application images, journals, TLS automation and VM workflows were removed intentionally. Previous results remain in Git history and are not evidence for this implementation.

## Executed locally on Debian 13 with real Docker

- Eight essential unit tests pass (`unit-tests.txt`), plus Bash syntax, Python compilation and JSON contract/schema validation.
- The complete smoke test exits **0** (`docker-smoke.txt`). Both Nginx and external/loopback FastCGI modes pass real health checks, repeated installation with identical credentials, stopped-service recovery, container recreation with PostgreSQL/Redis/file persistence, cold backup and restored database/Redis/files, unconfirmed restore rejection, corrupt-backup rejection, major-update rejection, image refresh, port collisions, unknown-directory preservation and ambient Docker/Compose isolation.
- The complete run confirms all pre-existing containers retain their status, start timestamp and restart count. Only suite-owned containers are removed. Persistent smoke data remains under `/opt/installer-v2-simple-final`.
- A separate real update changes PHP to official `php:8.4.13-fpm-alpine` and back to `php:8.4-fpm-alpine`, checks distinct digests and verifies PostgreSQL, Redis, files and credentials after both updates (`image-update.txt`). The smoke suite now also includes this distinct-image update in Nginx mode.
- An initial test found Compose interpolating a PHP healthcheck variable. The corrected healthcheck avoids that variable; the failure log is retained (`initial-health-failure.txt`). A subsequent overlapping run passed both modes but failed strict isolation because another suite removed its own containers. Its log is retained (`overlapping-tests.txt`); the final complete run was serial and passed the unchanged assertion.

## CI and limits

CI has exactly two jobs: syntax/essential units and real Docker smoke tests. No registry, application build, OS matrix or self-hosted VM requirements. Remote results for the published revision are reported after execution.

Supported-host parsing covers Ubuntu 22.04/24.04 and Debian 12/13. Real tests above ran on the shared Debian 13 host with Docker already installed. Optional Docker apt bootstrap on clean hosts was not executed. This is a reported testing limit, not a mandatory VM workflow. External operator-managed Nginx/Apache examples are generated but were not provisioned as host services.

Backups intentionally require downtime and restore to the original path/project. Major database migration and automatic interrupted-restore recovery are not provided; retain safety backups and inspect interrupted operations. Failed-image rollback is covered by an essential unit test; real tests cover successful distinct-image changes and actual backup restoration. No production-readiness claim is made.
