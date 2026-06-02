#!/usr/bin/env bash
# Smoke-test a built WatchAgent image: start the container, wait for the API to
# come up, and assert that GET /health returns 200 with status "ok".
#
# Used by the CI "Docker build" job and runnable locally:
#
#   ./scripts/smoke_test.sh                 # builds watchagent:ci first, then tests
#   ./scripts/smoke_test.sh watchagent:ci   # tests an already-built tag
#
# Requires no API keys: /health only reads the local SQLite store, so the
# container is healthy even with no outbound network access.
set -euo pipefail

IMAGE="${1:-watchagent:ci}"
CONTAINER="watchagent-smoke-$$"
PORT="${SMOKE_PORT:-8000}"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Build only if the caller didn't hand us an existing tag to reuse.
if [[ -z "${1:-}" ]]; then
  echo "==> Building $IMAGE"
  docker build -t "$IMAGE" .
fi

echo "==> Starting container $CONTAINER from $IMAGE"
docker run -d --name "$CONTAINER" -p "${PORT}:8000" "$IMAGE" >/dev/null

echo "==> Waiting for http://localhost:${PORT}/health"
for attempt in $(seq 1 30); do
  if curl -fsS "http://localhost:${PORT}/health" >/tmp/smoke_health.json 2>/dev/null; then
    echo "==> /health responded:"
    cat /tmp/smoke_health.json
    echo
    if grep -q '"status":"ok"' /tmp/smoke_health.json; then
      echo "==> SMOKE TEST PASSED"
      exit 0
    fi
    echo "==> /health did not report status=ok" >&2
    break
  fi
  sleep 1
done

echo "==> SMOKE TEST FAILED — container logs:" >&2
docker logs "$CONTAINER" >&2 || true
exit 1
