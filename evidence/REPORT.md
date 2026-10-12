# Installer qualification evidence — 2026-10-12

The installer implementation and real-service acceptance suite are complete in this checkout. **Production qualification remains incomplete:** clean-server Docker bootstrap gates and remote GitHub Actions execution must be confirmed after publishing. `production_ready` remains false; no release tag or main merge was made.

## Environment and successful checks

The shared host is Debian 13 (trixie), Docker Engine 29.9.0, Compose 5.6.0 and Python 3.13.5. See `environment.json` and `source-sha256.json` for the environment and source inventory.

- **20 original unit tests passed** on the host and in each Ubuntu 22.04/24.04 and Debian 12/13 container. Each OS container also passes actual OS preflight and ShellCheck for all shell entry points. These are container compatibility checks, **not clean-VM tests**. Logs: `unit-tests.txt`, `os-container-0.txt` (Ubuntu 22.04), `os-container-1.txt` (Ubuntu 24.04), `os-container-2.txt` (Debian 12), `os-container-3.txt` (Debian 13).
- Contract validates against its Draft 2020-12 JSON schema; workflow YAML parses. These checks are structural, not runtime qualification. See `schema.txt`.
- The current official Docker Debian signing key passes the exact single-primary-key fingerprint check. This read-only check does not install Docker or prove apt bootstrap. See `docker-key-check.txt`.
- **The complete real Docker harness exited 0** on the shared Debian 13 host. See `docker-integration.log`. It uses only the self-contained PHP 8.4-FPM fixture, PostgreSQL 18, Redis and container web servers; it does not use any application source repository.

## Real runtime assertions exercised

The log records first installation; an actually killed installer resumed with unchanged credentials; completed-install rerun; recovery of stopped services; both Nginx and external modes; loopback FastCGI; actual operator-style Nginx and Apache vhosts; HTTPS with a validated certificate and renewal/reload; HTTP denial of arbitrary PHP and hidden files; configured port collision rejection before provisioning; database, file and Redis persistence after container recreation; backup and restored database/files; destructive approval rejection; checksum-corrupt backup rejection; incompatible-image rejection before mutation; migration failure that changes data and creates an extra schema, followed by restoration of both data and the old image and removal of the extra schema; post-upgrade health failure recovery; successful distinct-image upgrade with changed public assets; interrupted migration recovery; interrupted restore recovery with damaged configuration and a missing public directory; database outage recovery; low-disk and read-only backup failure with data and service health preserved; actual private PostgreSQL/Redis port bindings and an internal backend network.

The suite verifies that every container in its initial snapshot has unchanged state. The snapshot includes all **ten pre-existing application containers**, including stopped containers, plus the two test registries. See `containers-before.json`. The supplemental PHP-account test writes to storage, custom, cache and public mounts as `www-data`, recreates containers, and verifies contents and service health in both modes. It also passes.

Only suite-owned projects and explicitly named auxiliary containers are removed. Installation data and backups remain at `/opt/installer-v2-qualification`. Service diagnostics are retained in `nginx-services.log`, `external-services.log` and `interrupted-services.log`. No global cleanup or unrelated-service operations were used.

## Test changes and one inconclusive run

The old rerun unit test required no Docker calls, which concealed outages. It now requires service recovery and real health validation while retaining unchanged file bytes and timestamps. All original release gates and safety contract values remain required. The contract/schema adds explicit administration requirements, and CI adds full real-service tests; no runtime assertion was removed to obtain a pass.

One intermediate run failed the strict isolation check because concurrently launched OS compatibility containers exited naturally during that run. Its log is retained as `docker-integration-concurrency-failure.txt`. The final run was serial, retains the same isolation assertions, passes every original-container state comparison, and exits 0.

## Remaining validation and delivery gates

1. **Clean Ubuntu/Debian VM bootstrap is untested.** Docker was already installed on this host. OS containers cannot test host apt installation, systemd startup, or initial Docker networking. QEMU and KVM are unavailable here; no clean VM runner is connected. `tests/vm-acceptance.sh` and the manually dispatched VM workflow require clean disposable operator-provided runners and exercise official Docker bootstrap plus the full suite. Until these run with logs, the clean-VM gates remain incomplete.
2. **Published implementation CI passed.** Commit `345c6770797f19a0ee3b0b135224ae04de300a18` passed all seven checks in runs [38306907176](https://github.com/cristake007/installer-v2/actions/runs/38306907176) and [38306902307](https://github.com/cristake007/installer-v2/actions/runs/38306902307), including full real Docker qualification on both Ubuntu runners. This subsequent revision simplifies CI without removing runtime assertions; its results are reported separately after publication.
3. **Environment isolation regression passes locally.** The latest host suite has 21 passing unit tests. `environment-isolation.log` records real start/health/backup/restore in both web modes with hostile Docker/Compose variables injected. Full CI also tests rerun and upgrade under those variables. Docker commands enforce the local daemon, managed project and saved settings.
4. **Branch publication uses the connected GitHub integration.** Command-line Git authentication is unavailable; the Git data API publishes the tested tree with an expected-head check. No main merge or release tag is performed.

Certificate issuance and scheduled renewal are operator-managed: configured container HTTPS and explicit certificate reload are implemented and tested; automatic ACME issuance is not claimed. Restore operates on the original installation identity/path, requires explicit project confirmation, and first takes a safety backup. Destructive operations require a healthy baseline for that safety copy; interrupted operations use the validated recorded backup through `recover`. Automated relocation of a completely lost installation root is not implemented.

Committed log formatting normalizes carriage-return progress updates and trailing whitespace; test assertions and their outcomes are retained.
