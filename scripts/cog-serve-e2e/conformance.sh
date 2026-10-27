#!/usr/bin/env bash
# The OCI distribution-spec conformance suite (pull and push workflows) against the Hub's /v2/ surface.
#
#   scripts/cog-serve-e2e/conformance.sh
#
# The suite pushes fixtures of its own, which are not Cog bundles, and the Hub
# refuses to commit a manifest its catalog could not index. So this runs the
# Hub through conformance_hub.py, a TEST-ONLY launcher that replaces that one
# check in its own process; everything else -- routing, authentication, the
# publish permission, upload sessions, streaming, the write-through to a real
# registry, serving the result back -- is the code that ships. There is no
# setting that relaxes validation, in the chart or in the API.
#
# Needs docker, uv, curl and jq.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BACKING_PORT="${BACKING_PORT:-5082}"
HUB_PORT="${HUB_PORT:-8082}"
SUITE_IMAGE="${SUITE_IMAGE:-ghcr.io/opencontainers/distribution-spec/conformance:v1.1.0}"
WORK="$(mktemp -d "$ROOT/.cog-serve-e2e.XXXXXX")"
CONTAINER="cog-conformance-backing-$$"
HUB_PID=""

cleanup() {
  [ -n "$HUB_PID" ] && kill "$HUB_PID" 2>/dev/null || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
die() { printf 'FAIL %s\n' "$1" >&2; [ -f "$WORK/hub.log" ] && tail -30 "$WORK/hub.log" >&2; exit 1; }

echo "== start a backing registry and the Hub in front of it (validation relaxed: test-only launcher)"
docker run -d --name "$CONTAINER" -p "$BACKING_PORT:5000" registry:2.8.3 >/dev/null
(
  cd "$ROOT/api"
  export CONF_PORT="$HUB_PORT"
  export COLLAB_HUB_API__STORAGE__FRAMES_PATH="$WORK/frames"
  export COLLAB_HUB_API__FRAMES__MCP_SESSION_MANAGER_ENABLED=false
  export COLLAB_HUB_API__TASKS__BACKEND=memory
  export COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory
  export COLLAB_HUB_API__COGS__REGISTRY_SOURCES='[{"id":"backing","kind":"static","url":"http://localhost:'"$BACKING_PORT"'","publish":true}]'
  export COLLAB_HUB_API__COGS__SERVE__ENABLED=true
  # The suite runs in a container and reaches the Hub, and its token endpoint, by this name.
  export COLLAB_HUB_API__COGS__SERVE__PUBLIC_URL="http://host.docker.internal:$HUB_PORT"
  export COLLAB_HUB_API__COGS__PUBLISH__ALLOWED_USERS='["conformance-user"]'
  export FRAMES_UNSAFE_AUTH_ENABLED=true FRAMES_BEARER_ALLOW_UNSIGNED=true
  exec uv run --quiet python "$HERE/conformance_hub.py"
) > "$WORK/hub.log" 2>&1 &
HUB_PID=$!
for _ in $(seq 1 60); do
  kill -0 "$HUB_PID" 2>/dev/null || die "the Hub exited during startup"
  curl -s -o /dev/null "http://localhost:$HUB_PORT/health" && break
  sleep 1
done

b64() { printf '%s' "$1" | base64 | tr -d '=\n' | tr '/+' '_-'; }
TOKEN="$(b64 '{"alg":"none"}').$(b64 '{"preferred_username":"conformance-user","org_id":"conformance-org","workspace_id":"default"}')."
credential="$(curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"scope":"publish"}' "http://localhost:$HUB_PORT/v1/cogs/registry-credentials")"
[ "$(printf '%s' "$credential" | jq -r .scope)" = "publish" ] || die "publish exchange answered: $credential"

echo "== run the conformance suite: pull and push workflows"
docker run --rm --add-host host.docker.internal:host-gateway \
  -e OCI_ROOT_URL="http://host.docker.internal:$HUB_PORT" \
  -e OCI_NAMESPACE=cogs/conformance \
  -e OCI_CROSSMOUNT_NAMESPACE=cogs/conformance-mount \
  -e OCI_USERNAME="$(printf '%s' "$credential" | jq -r .username)" \
  -e OCI_PASSWORD="$(printf '%s' "$credential" | jq -r .secret)" \
  -e OCI_TEST_PULL=1 -e OCI_TEST_PUSH=1 \
  -e OCI_HIDE_SKIPPED_WORKFLOWS=1 -e OCI_DEBUG=0 \
  "$SUITE_IMAGE" | tee "$WORK/suite.log" | tail -8
grep -q "0 Failed" "$WORK/suite.log" && grep -q "SUCCESS" "$WORK/suite.log" || die "the conformance suite did not pass"
echo "== result: PASSED"
