#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! command -v python3 >/dev/null 2>&1; then
  printf 'python3 is required. Please install the distribution python3 package.\n' >&2
  exit 1
fi
exec python3 "$SCRIPT_DIR/src/installer.py" "$@"
