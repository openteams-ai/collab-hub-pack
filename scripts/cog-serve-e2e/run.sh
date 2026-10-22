#!/usr/bin/env bash
# End-to-end for Cog pulls through the Hub (issue #179), against a real registry.
#
# Starts a private OCI registry (the "backing" registry, behind a credential
# only the Hub holds), publishes a Cog bundle to it with a standard client,
# starts the Hub with that registry as a `static` source and serving on, and
# then pulls the Cog *from the Hub host* with `oras`, holding nothing but a
# Hub sign-in. The same script runs against two
# different registries:
#
#   scripts/cog-serve-e2e/run.sh distribution   # distribution/registry
#   scripts/cog-serve-e2e/run.sh zot            # project-zot/zot
#
# Nothing between the two runs changes on the client side, which is the
# point: the registry behind the Hub is the operator's choice.
#
# The layout, so the clients see what they would in a deployment:
#
#   client container (oras) --https--> hub.test (a TLS proxy) --> the Hub
#                                                                        |
#                    backing.test (the registry) <--- its own credential-+
#
# The Hub runs as a process on this machine (`uv run`), everything else is a
# container on one docker network. Clients reach the Hub at https://hub.test
# with a throwaway CA, as they would in a deployment.
#
# Needs docker, uv, curl and jq.
set -euo pipefail

BACKING="${1:-distribution}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BACKING_PORT="${BACKING_PORT:-5081}"
HUB_PORT="${HUB_PORT:-8081}"
# Under the repository so the client container can bind-mount it.
WORK="$(mktemp -d "$ROOT/.cog-serve-e2e.XXXXXX")"
NETWORK="cog-serve-e2e-$$"
CONTAINER="cog-serve-e2e-backing-$$"
PROXY="cog-serve-e2e-proxy-$$"
CLIENT_IMAGE="collab-hub/cog-serve-e2e-client:test"
HUB_PID=""

BACKING_USER="hub-robot"
BACKING_PASSWORD="backing-robot-secret-$$"
BACKING_ADDR="backing.test:5000"   # the backing registry, as a publisher reaches it
HUB_REGISTRY="hub.test"            # the only registry address a client is ever given
HUB="http://localhost:$HUB_PORT"   # this script's own calls to the Hub API
REPO="cogs/cog-audio-transcriber"

pass() { printf 'ok   %s\n' "$1"; }
die() {
  printf 'FAIL %s\n' "$1" >&2
  if [ -f "$WORK/hub.log" ]; then echo "--- hub log (tail)" >&2; tail -40 "$WORK/hub.log" >&2; fi
  exit 1
}
cleanup() {
  [ -n "$HUB_PID" ] && kill "$HUB_PID" 2>/dev/null || true
  docker rm -f "$CONTAINER" "$PROXY" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# client <dir> <command...>: run a registry client in its container, in <dir>,
# trusting the proxy's CA once there is one.
client() {
  local dir="$1"; shift
  docker run --rm --network "$NETWORK" --user "$(id -u):$(id -g)" -v "$WORK:$WORK" -w "$dir" \
    -e SSL_CERT_FILE="$WORK/hub-ca.crt" "$CLIENT_IMAGE" "$@"
}
oras_in() { local dir="$1"; shift; client "$dir" oras "$@"; }

echo "== build the client image (oras, as released)"
docker build -q -t "$CLIENT_IMAGE" "$HERE" >/dev/null
docker network create "$NETWORK" >/dev/null
# Until the proxy has issued its CA there is nothing to trust; the backing
# registry is reached over plain HTTP on the docker network.
: > "$WORK/hub-ca.crt"

echo "== start the backing registry ($BACKING), private: only $BACKING_USER may read it"
mkdir -p "$WORK/auth" "$WORK/bundle" "$WORK/plain"
docker run --rm httpd:2.4-alpine htpasswd -Bbn "$BACKING_USER" "$BACKING_PASSWORD" > "$WORK/auth/htpasswd"
case "$BACKING" in
  distribution)
    docker run -d --name "$CONTAINER" --network "$NETWORK" --network-alias backing.test \
      -p "$BACKING_PORT:5000" -v "$WORK/auth:/auth:ro" \
      -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=backing \
      -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
      registry:2.8.3 >/dev/null
    ;;
  zot)
    cat > "$WORK/auth/zot.json" <<JSON
{"distSpecVersion": "1.1.0",
 "storage": {"rootDirectory": "/tmp/zot"},
 "http": {"address": "0.0.0.0", "port": "5000", "auth": {"htpasswd": {"path": "/auth/htpasswd"}}},
 "log": {"level": "warn"}}
JSON
    case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; *) arch=amd64 ;; esac
    docker run -d --name "$CONTAINER" --network "$NETWORK" --network-alias backing.test \
      -p "$BACKING_PORT:5000" -v "$WORK/auth:/auth:ro" \
      "ghcr.io/project-zot/zot-linux-$arch:v2.1.2" serve /auth/zot.json >/dev/null
    ;;
  *) die "unknown backing registry '$BACKING' (distribution|zot)" ;;
esac
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$BACKING_PORT/v2/" || true)"
  [ "$code" = "401" ] && break
  sleep 1
done
[ "$code" = "401" ] || die "the backing registry did not come up private (GET /v2/ answered $code)"

echo "== publish a Cog bundle to the backing registry, as a publisher would"
cp "$ROOT/api/tests/fixtures/cogs/pixi-complete/COG.md" "$ROOT/api/tests/fixtures/cogs/pixi-complete/pixi.toml" "$WORK/bundle/"
printf 'version: 6\nenvironments: {}\npackages: []\n' > "$WORK/bundle/pixi.lock"
head -c 300000 /dev/urandom > "$WORK/bundle/weights.bin"   # several stream chunks
printf '{}' > "$WORK/bundle/config.json"
oras_in "$WORK/bundle" push --plain-http -u "$BACKING_USER" -p "$BACKING_PASSWORD" \
  --config config.json:application/vnd.pixi.config.v1+toml \
  "$BACKING_ADDR/$REPO:0.1.0" \
  pixi.toml:application/vnd.pixi.toml.v1+toml \
  pixi.lock:application/vnd.pixi.lock.v1+yaml \
  COG.md:application/vnd.nebi.asset.v1 \
  weights.bin:application/vnd.nebi.asset.v1 >/dev/null
# In the same repository, but not a Cog: the Hub must never serve it.
printf 'not a cog\n' > "$WORK/plain/README.txt"
oras_in "$WORK/plain" push --plain-http -u "$BACKING_USER" -p "$BACKING_PASSWORD" \
  "$BACKING_ADDR/$REPO:plain" README.txt:text/plain >/dev/null
oras_in "$WORK" manifest fetch --plain-http -u "$BACKING_USER" -p "$BACKING_PASSWORD" \
  "$BACKING_ADDR/$REPO:0.1.0" > "$WORK/backing-manifest.json"

echo "== start the Hub: that registry as a static source, serving on"
(
  cd "$ROOT/api"
  export COLLAB_HUB_API__SERVER__HOSTNAME=0.0.0.0
  export COLLAB_HUB_API__SERVER__PORT="$HUB_PORT"
  export COLLAB_HUB_API__STORAGE__FRAMES_PATH="$WORK/frames"
  export COLLAB_HUB_API__FRAMES__MCP_SESSION_MANAGER_ENABLED=false
  export COLLAB_HUB_API__TASKS__BACKEND=memory
  export COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory
  export COLLAB_HUB_API__COGS__REGISTRY_SOURCES='[{"id":"backing","kind":"static","url":"http://localhost:'"$BACKING_PORT"'","repositories":["'"$REPO"'"],"credentials":{"username_env":"E2E_BACKING_USER","password_env":"E2E_BACKING_PASSWORD"}}]'
  export E2E_BACKING_USER="$BACKING_USER" E2E_BACKING_PASSWORD="$BACKING_PASSWORD"
  export COLLAB_HUB_API__COGS__INDEX__ENABLED=true
  export COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS=10
  export COLLAB_HUB_API__COGS__SERVE__ENABLED=true
  export COLLAB_HUB_API__COGS__SERVE__PUBLIC_URL="https://$HUB_REGISTRY"
  # A Hub sign-in for this script: unsigned bearer tokens, local development only.
  export FRAMES_UNSAFE_AUTH_ENABLED=true FRAMES_BEARER_ALLOW_UNSIGNED=true
  exec uv run --quiet python -m collab_hub_api
) > "$WORK/hub.log" 2>&1 &
HUB_PID=$!

echo "== put the Hub behind TLS at https://$HUB_REGISTRY"
docker run -d --name "$PROXY" --network "$NETWORK" --network-alias "$HUB_REGISTRY" \
  --add-host host.docker.internal:host-gateway caddy:2.10-alpine \
  caddy reverse-proxy --from "$HUB_REGISTRY" --to "host.docker.internal:$HUB_PORT" --internal-certs >/dev/null
for _ in $(seq 1 60); do
  docker cp "$PROXY:/data/caddy/pki/authorities/local/root.crt" "$WORK/hub-ca.crt" >/dev/null 2>&1 && break
  sleep 1
done
[ -s "$WORK/hub-ca.crt" ] || die "the TLS proxy did not issue its CA"

b64() { printf '%s' "$1" | base64 | tr -d '=\n' | tr '/+' '_-'; }
HUB_TOKEN="$(b64 '{"alg":"none"}').$(b64 '{"preferred_username":"e2e-user","org_id":"e2e-org","workspace_id":"default","sid":"e2e-session"}')."
hub() { curl -sS -H "Authorization: Bearer $HUB_TOKEN" "$@"; }

for _ in $(seq 1 90); do
  kill -0 "$HUB_PID" 2>/dev/null || die "the Hub exited during startup"
  listed="$(hub "$HUB/v1/cogs" 2>/dev/null | jq -r '.items | length' 2>/dev/null || echo 0)"
  [ "$listed" = "1" ] && break
  sleep 1
done
[ "$listed" = "1" ] || die "the Hub did not index the Cog (listed: $listed)"
pass "the Hub indexed the Cog from the $BACKING registry with its own credential"

REFERENCE="$(hub "$HUB/v1/cogs" | jq -r '.items[0].reference')"
DIGEST="$(hub "$HUB/v1/cogs" | jq -r '.items[0].digest')"
[ "$REFERENCE" = "$HUB_REGISTRY/$REPO@$DIGEST" ] || die "the catalog reference does not name the Hub: $REFERENCE"
pass "the catalog reference names the Hub: $REFERENCE"

echo "== the challenge a registry client follows"
challenge="$(curl -sS -o /dev/null -D - "$HUB/v2/" | tr -d '\r' | grep -i '^www-authenticate:')"
case "$challenge" in
  *"Bearer realm=\"https://$HUB_REGISTRY/v2/token\",service=\"$HUB_REGISTRY\""*) pass "GET /v2/ answers 401 with the bearer challenge" ;;
  *) die "unexpected challenge: $challenge" ;;
esac

echo "== without a Hub sign-in the pull is refused"
mkdir -p "$WORK/anon" "$WORK/out" "$WORK/bytag"
if oras_in "$WORK/anon" pull "$REFERENCE" >/dev/null 2>&1; then die "an anonymous pull succeeded"; fi
pass "anonymous pull refused"
if oras_in "$WORK/anon" pull -u "$BACKING_USER" -p "$BACKING_PASSWORD" "$REFERENCE" >/dev/null 2>&1; then
  die "the backing registry's credential opened the Hub"
fi
pass "the backing registry's credential is not a Hub credential"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$HUB/v1/cogs/registry-credentials")" = "401" ] || die "the exchange answered an anonymous caller"

echo "== exchange the Hub session for a registry credential, and pull with oras"
credential="$(hub -X POST "$HUB/v1/cogs/registry-credentials")"
[ "$(printf '%s' "$credential" | jq -r .registry)" = "$HUB_REGISTRY" ] || die "exchange answered: $credential"
REG_USER="$(printf '%s' "$credential" | jq -r .username)"
REG_SECRET="$(printf '%s' "$credential" | jq -r .secret)"
CRED_ID="$(printf '%s' "$credential" | jq -r .id)"

oras_in "$WORK/out" pull -u "$REG_USER" -p "$REG_SECRET" "$REFERENCE" >/dev/null || die "oras pull by digest failed"
for file in COG.md pixi.toml pixi.lock weights.bin; do
  cmp -s "$WORK/bundle/$file" "$WORK/out/$file" || die "$file differs after the round trip"
done
pass "oras pull by digest: every file identical to what was published"

oras_in "$WORK/bytag" pull -u "$REG_USER" -p "$REG_SECRET" "$HUB_REGISTRY/$REPO:0.1.0" >/dev/null || die "oras pull by tag failed"
cmp -s "$WORK/bundle/weights.bin" "$WORK/bytag/weights.bin" || die "pull by tag returned different bytes"
pass "oras pull by tag"

oras_in "$WORK" manifest fetch -u "$REG_USER" -p "$REG_SECRET" "$REFERENCE" > "$WORK/hub-manifest.json"
cmp -s "$WORK/backing-manifest.json" "$WORK/hub-manifest.json" || die "the manifest served by the Hub is not byte-identical"
[ "$(jq -r .config.mediaType "$WORK/hub-manifest.json")" = "application/vnd.pixi.config.v1+toml" ] || die "config media type changed"
jq -e '[.layers[].mediaType] | index("application/vnd.nebi.asset.v1")' "$WORK/hub-manifest.json" >/dev/null || die "asset media type missing"
pass "the manifest is byte-identical and the nebi media types round-trip"

tags="$(oras_in "$WORK" repo tags -u "$REG_USER" -p "$REG_SECRET" "$HUB_REGISTRY/$REPO" | tr '\n' ' ')"
[ "$tags" = "0.1.0 " ] || die "tags/list answered '$tags' (the non-Cog tag must not be listed)"
pass "tags/list lists what the catalog indexed, and only that"

echo "== what the catalog did not index is not served, though the registry holds it"
if oras_in "$WORK/anon" pull -u "$REG_USER" -p "$REG_SECRET" "$HUB_REGISTRY/$REPO:plain" >/dev/null 2>&1; then
  die "a non-Cog artifact was served"
fi
if oras_in "$WORK/anon" pull -u "$REG_USER" -p "$REG_SECRET" "$HUB_REGISTRY/cogs/not-indexed:latest" >/dev/null 2>&1; then
  die "an unindexed repository was served"
fi
pass "non-Cog artifact and unindexed repository refused"

echo "== the registry credential opens nothing else, anywhere"
for path in /v1/cogs /v1/frames /v1/me; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -u "$REG_USER:$REG_SECRET" "$HUB$path")"
  [ "$code" = "401" ] || die "the registry credential got $code from $path"
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $REG_SECRET" "$HUB$path")"
  [ "$code" = "401" ] || die "the registry secret as a bearer token got $code from $path"
done
if oras_in "$WORK" manifest fetch --plain-http -u "$REG_USER" -p "$REG_SECRET" "$BACKING_ADDR/$REPO:0.1.0" >/dev/null 2>&1; then
  die "the Hub's registry credential opened the backing registry"
fi
pass "refused by the Hub's other APIs and by the backing registry"

echo "== revocation is immediate"
[ "$(hub -o /dev/null -w '%{http_code}' -X DELETE "$HUB/v1/cogs/registry-credentials/$CRED_ID")" = "204" ] || die "revoke did not answer 204"
if oras_in "$WORK/anon" pull -u "$REG_USER" -p "$REG_SECRET" "$REFERENCE" >/dev/null 2>&1; then
  die "a revoked credential still pulls"
fi
[ "$(hub -o /dev/null -w '%{http_code}' -X DELETE "$HUB/v1/cogs/registry-credentials")" = "204" ] || die "revoke-all did not answer 204"
pass "a revoked credential no longer pulls"

echo "== nothing about the backing registry reached a client or a log"
hub "$HUB/v1/cogs" > "$WORK/seen.txt"
hub "$HUB/v1/cogs/example/cog-audio-transcriber/versions/$DIGEST/reference" >> "$WORK/seen.txt" || true
curl -sS -i "$HUB/v2/$REPO/manifests/0.1.0" >> "$WORK/seen.txt"
curl -sS -i "$HUB/v2/token" >> "$WORK/seen.txt"
if grep -q -e ":$BACKING_PORT" -e "backing.test" -e "$BACKING_PASSWORD" -e "$BACKING_USER" "$WORK/seen.txt"; then
  die "a response names the backing registry or its credential"
fi
if grep -q -e "$BACKING_PASSWORD" -e "$REG_SECRET" "$WORK/hub.log"; then die "a secret reached the Hub's log"; fi
pass "no backing address or credential in any response; no secret in the log"

echo "== result: PASSED ($BACKING)"
