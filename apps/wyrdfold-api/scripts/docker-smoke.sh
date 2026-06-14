#!/usr/bin/env bash
# Local pre-ship check for the wyrdfold-api container: build the image the
# way Railway does (context = monorepo root), boot it, and confirm /health
# returns 200. Catches the build failures that otherwise only surface after
# a push: dependency resolution, build stages, missing system deps (pandoc),
# and whether the app actually starts.
#
# Boot step points at the local Supabase stack (`supabase start`). If that
# stack isn't running, the build is still validated and the boot step is
# skipped with a notice.
#
# Usage: pnpm nx docker:smoke wyrdfold-api   (or: bash apps/wyrdfold-api/scripts/docker-smoke.sh)
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

IMAGE="wyrdfold-api:local-smoke"
CONTAINER="wyrdfold-api-smoke"
HOST_PORT="8011"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Building $IMAGE (context: $ROOT)"
docker build -f apps/wyrdfold-api/Dockerfile -t "$IMAGE" .

SRK="$(supabase status -o env 2>/dev/null | grep '^SERVICE_ROLE_KEY=' | cut -d'"' -f2 || true)"
if [ -z "$SRK" ]; then
  echo "==> Build OK. Local Supabase stack not detected (run 'supabase start')."
  echo "    Skipping boot check — the image built cleanly."
  exit 0
fi

echo "==> Booting container and probing /health"
cleanup
docker run -d --name "$CONTAINER" -p "${HOST_PORT}:8001" \
  -e ALLOWED_HOSTS="*" \
  -e WYRDFOLD_API_KEY="smoketest" \
  -e SUPABASE_URL="http://host.docker.internal:54321" \
  -e SUPABASE_SERVICE_ROLE_KEY="$SRK" \
  "$IMAGE" >/dev/null

# Poll /health for up to ~20s.
for _ in $(seq 1 20); do
  body="$(curl -fsS -m 3 "http://127.0.0.1:${HOST_PORT}/health" 2>/dev/null || true)"
  if [ -n "$body" ]; then
    echo "==> /health -> $body"
    echo "==> Smoke check PASSED"
    exit 0
  fi
  sleep 1
done

echo "==> Smoke check FAILED — /health never responded. Container logs:" >&2
docker logs "$CONTAINER" 2>&1 | tail -20 >&2
exit 1
