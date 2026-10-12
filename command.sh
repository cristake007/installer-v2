#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$HERE"
if [[ ! -f settings.json || ! -f .env || ! -f compose.yaml ]]; then
  printf 'Invalid/incomplete FluidWeb installation\n' >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  printf 'Docker unavailable\n' >&2
  exit 1
fi
case "${1:-}" in
  status) exec docker compose --project-directory "$HERE" -f compose.yaml ps ;;
  start) exec docker compose --project-directory "$HERE" -f compose.yaml up -d --wait ;;
  stop) exec docker compose --project-directory "$HERE" -f compose.yaml stop ;;
  restart) exec docker compose --project-directory "$HERE" -f compose.yaml restart ;;
  logs) shift; exec docker compose --project-directory "$HERE" -f compose.yaml logs --tail=200 "$@" ;;
  version) python3 - "$HERE" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);print(json.dumps(json.loads((p/'settings.json').read_text())['images'],indent=2))
PY
  ;;
  *) printf 'Usage: %s {status|start|stop|restart|logs [service]|version}\n' "$0" >&2; exit 2 ;;
esac
