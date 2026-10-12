#!/usr/bin/env bash
# Explicit invocation on a clean disposable VM authorizes official Docker apt setup.
set -euo pipefail
cd "$(dirname "$0")/.."
EXPECTED="${1:?Usage: vm-acceptance.sh ubuntu:24.04|debian:13}"
python3 - "$EXPECTED" <<'PY'
import sys
sys.path.insert(0,'src')
import installer as i
i.validate_host()
actual=':'.join(i.read_os_release())
if actual != sys.argv[1]:
    raise SystemExit('VM OS mismatch: '+actual)
if i.docker_available():
    raise SystemExit('Clean VM gate requires Docker absent before bootstrap')
i.install_docker(*i.read_os_release())
PY
bash tests/run-integration.sh /opt/installer-v2-vm-acceptance
