#!/usr/bin/env bash
# Run from the installed /opt/fluidweb directory (or your chosen directory).
set -euo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ -f "$DIR/compose.yaml" && -f "$DIR/.env" ]] || { echo 'No installation found.' >&2; exit 1; }
[[ $EUID == 0 ]] || { echo 'Run this script with sudo.' >&2; exit 1; }
dc() { docker compose --project-directory "$DIR" --env-file "$DIR/.env" -f "$DIR/compose.yaml" "$@"; }
case "${1:-help}" in
  start) dc up -d;;
  stop) dc down;;
  restart) dc restart;;
  status) dc ps;;
  logs) shift; dc logs --tail 100 "$@";;
  renew-cert)
    grep -Fxq 'SAVED_SSL=1' "$DIR/.installer-settings" || { echo 'SSL was not enabled.' >&2; exit 1; }
    dc run --rm certbot renew --non-interactive --webroot -w /var/www/certbot
    dc exec -T nginx nginx -s reload
    ;;
  help) printf 'Usage: sudo ./command.sh {start|stop|restart|status|logs|renew-cert}\n';;
  *) echo "Unknown command: $1" >&2; exit 2;;
esac
