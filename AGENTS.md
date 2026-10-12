# Installer v2: execution contract

This repository owns the **installer only**, not the FluidWeb application. Never change the FluidWeb repo to make installer tests pass without a separate approved app change.

- Treat `contracts/installer.v1.json` as the machine-readable acceptance requirements. The schema validates format; executable tests validate runtime behavior. Neither alone is sufficient.
- Never report production readiness based on mocked tests, successful YAML parsing, or running containers alone. Require real installation, rerun, persistence, failure/recovery and cross-app isolation tests on clean and shared hosts.
- Preserve credentials, configuration, database and uploads on rerun. An unexpected directory is an error, not a target for cleanup.
- NEVER use `docker system prune`, `docker volume prune`, broad `docker rm` filters, destructive `docker compose down -v`, or stop/modify an unrelated service.
- The only supported host OS families are Ubuntu LTS and Debian stable. Do not disable host services or install Nginx/Apache on host.
- No default `:dev`, `:latest` or mutable application image for production. Use and record immutable image digests.
- An external web server is managed by the operator. Installer must not edit vhosts, change firewall, or claim public HTTP readiness in this mode.
- New installation can be automated; destructive actions require explicit independent approval and a validated backup.
- Changes to CI or the contract cannot be used to conceal a failing implementation. If a test is altered, retain equivalent or stronger behavioral assertions; explain changes.
- No release tag or 'production ready' statement until all required gates pass with logs/evidence, including real VM Ubuntu/Debian tests.
- Read `README.md` for the current implementation status and explicitly marked incomplete work.
