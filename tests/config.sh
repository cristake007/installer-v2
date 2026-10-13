#!/usr/bin/env bash
# Generated configuration and safety guards only; no images or containers started.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
bash -n install.sh command.sh
source ./install.sh <<< q >/dev/null
check_root=$(mktemp -d /tmp/fluidweb-config-check.XXXXXX)
trap 'rm -rf -- "$check_root"' EXIT
# Syntax-only image reference, never pulled or run.
FLUIDWEB_IMAGE=ghcr.io/cristake007/fluidweb@sha256:$(printf '%064d' 0)
DIR=$check_root/nginx
write_new_installation
dc config -q
ensure_fluidweb_installation
before=$(sha256sum "$DIR/.env" "$DIR/compose.yaml" "$DIR/.installer-settings")
validate_dir_and_ports
[[ $before == "$(sha256sum "$DIR/.env" "$DIR/compose.yaml" "$DIR/.installer-settings")" ]]
DOMAIN=fluidweb.test
SSL_ENABLED=1
enable_ssl_on_existing_http
https_config
dc config -q
DIR="$check_root/external with spaces"
write_external_installation
dc config -q
[[ $(dc config --services | sort) == $'php\npostgres\nredis' ]]
grep -Fq '127.0.0.1:${FPM_PORT}:9000' "$DIR/compose.yaml"
ensure_fluidweb_installation
# Infrastructure-only installations must fail before changing existing files.
cp "$DIR/.env" "$check_root/valid-env"
sed -i '/^FLUIDWEB_IMAGE=/d' "$DIR/.env"
before=$(sha256sum "$DIR/.env" "$DIR/compose.yaml")
if (ensure_fluidweb_installation) >/dev/null 2>&1; then
  echo 'Infrastructure-only installation was incorrectly accepted.' >&2; exit 1
fi
[[ $before == "$(sha256sum "$DIR/.env" "$DIR/compose.yaml")" ]]
cp "$check_root/valid-env" "$DIR/.env"
# Mode switching and legacy PostgreSQL mounts remain rejected.
INSTALL_MODE=nginx
if (validate_dir_and_ports) >/dev/null 2>&1; then
  echo 'External-to-Nginx conversion was incorrectly accepted.' >&2; exit 1
fi
printf '\n      - ./data/postgres:/var/lib/postgresql\n' >> "$DIR/compose.yaml"
if (ensure_postgres_volume) >/dev/null 2>&1; then
  echo 'Legacy PostgreSQL storage was incorrectly accepted.' >&2; exit 1
fi
printf 'PASS: Bash syntax, HTTP/SSL/external Compose configuration, unchanged rerun settings, and refusal guards.\n'
