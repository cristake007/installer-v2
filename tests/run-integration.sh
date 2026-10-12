#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
REGISTRY="installer-v2-registry-$$"
trap 'docker rm -f "$REGISTRY" >/dev/null' EXIT
docker run -d --name "$REGISTRY" -p 127.0.0.1:15001:5000 registry:2
for variant in good bad next incompatible unhealthy; do
  failure=0
  version=1
  compatibility=0
  health=0
  if [[ "$variant" == incompatible ]]; then compatibility=1; fi
  if [[ "$variant" == unhealthy ]]; then health=1; fi
  if [[ "$variant" == next ]]; then version=2; fi
  if [[ "$variant" == bad ]]; then failure=1; fi
  docker build --build-arg "FAIL_MIGRATION=$failure" --build-arg "FIXTURE_VERSION=$version" --build-arg "FAIL_COMPATIBILITY=$compatibility" --build-arg "FAIL_HEALTH=$health" -t "localhost:15001/installer-fixture:$variant" tests/fixture
  docker push "localhost:15001/installer-fixture:$variant"
done
GOOD="$(docker image inspect localhost:15001/installer-fixture:good --format '{{index .RepoDigests 0}}')"
BAD="$(docker image inspect localhost:15001/installer-fixture:bad --format '{{index .RepoDigests 0}}')"
NEXT="$(docker image inspect localhost:15001/installer-fixture:next --format '{{index .RepoDigests 0}}')"
INCOMPATIBLE="$(docker image inspect localhost:15001/installer-fixture:incompatible --format '{{index .RepoDigests 0}}')"
UNHEALTHY="$(docker image inspect localhost:15001/installer-fixture:unhealthy --format '{{index .RepoDigests 0}}')"
TEST_ROOT="${1:-/opt/installer-v2-test-$$}"
python3 tests/integration.py --image "$GOOD" --bad-image "$BAD" --next-image "$NEXT" --incompatible-image "$INCOMPATIBLE" --unhealthy-image "$UNHEALTHY" --root "$TEST_ROOT"
python3 tests/check-write-permissions.py --root "$TEST_ROOT"
