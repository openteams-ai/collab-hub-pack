#!/usr/bin/env bash
# End-to-end for publishing and pulling Cogs through the Hub (issues #179, #180), against a real registry.
#
# Starts a private OCI registry (the "backing" registry, behind a credential
# only the Hub holds), starts the Hub with that registry as its publish
# source -- a `static` source with no repository list -- and then publishes
# a Cog bundle *to the Hub host* and pulls it back *from the Hub host* with
# `oras`, holding nothing but a Hub sign-in. The same script runs against two
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

echo "== prepare a Cog bundle"
cp "$ROOT/api/tests/fixtures/cogs/pixi-complete/COG.md" "$ROOT/api/tests/fixtures/cogs/pixi-complete/pixi.toml" "$WORK/bundle/"
printf 'version: 6\nenvironments: {}\npackages: []\n' > "$WORK/bundle/pixi.lock"
head -c 300000 /dev/urandom > "$WORK/bundle/weights.bin"   # several stream chunks
printf '{}' > "$WORK/bundle/config.json"
printf 'not a cog\n' > "$WORK/plain/README.txt"

echo "== start the Hub: that registry as its publish source, with no repository list"
(
  cd "$ROOT/api"
  export COLLAB_HUB_API__SERVER__HOSTNAME=0.0.0.0
  export COLLAB_HUB_API__SERVER__PORT="$HUB_PORT"
  export COLLAB_HUB_API__STORAGE__FRAMES_PATH="$WORK/frames"
  export COLLAB_HUB_API__FRAMES__MCP_SESSION_MANAGER_ENABLED=false
  export COLLAB_HUB_API__TASKS__BACKEND=memory
  export COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory
  # A static source with nothing to enumerate: what it holds is what is published through the Hub.
  export COLLAB_HUB_API__COGS__REGISTRY_SOURCES='[{"id":"backing","kind":"static","url":"http://localhost:'"$BACKING_PORT"'","publish":true,"credentials":{"username_env":"E2E_BACKING_USER","password_env":"E2E_BACKING_PASSWORD"}}]'
  export E2E_BACKING_USER="$BACKING_USER" E2E_BACKING_PASSWORD="$BACKING_PASSWORD"
  export COLLAB_HUB_API__COGS__INDEX__ENABLED=true
  export COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS=10
  export COLLAB_HUB_API__COGS__SERVE__ENABLED=true
  export COLLAB_HUB_API__COGS__SERVE__PUBLIC_URL="https://$HUB_REGISTRY"
  # The publish permission: one named user. Nobody holds it by default.
  export COLLAB_HUB_API__COGS__PUBLISH__ALLOWED_USERS='["e2e-user"]'
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
bearer_for() { # <user> <org>: an unsigned Hub bearer token for that user
  local payload
  payload="$(printf '{"preferred_username":"%s","org_id":"%s","workspace_id":"default","sid":"e2e-session"}' "$1" "$2")"
  printf '%s.%s.' "$(b64 '{"alg":"none"}')" "$(b64 "$payload")"
}
HUB_TOKEN="$(bearer_for e2e-user e2e-org)"
OTHER_TOKEN="$(bearer_for other-user other-org)"
hub() { curl -sS -H "Authorization: Bearer $HUB_TOKEN" "$@"; }

for _ in $(seq 1 90); do
  kill -0 "$HUB_PID" 2>/dev/null || die "the Hub exited during startup"
  [ "$(hub -o /dev/null -w '%{http_code}' "$HUB/v1/cogs" 2>/dev/null)" = "200" ] && break
  sleep 1
done
[ "$(hub "$HUB/v1/cogs" | jq -r '.items | length')" = "0" ] || die "the catalog is not empty before anything was published"

push_cog() { # <user> <secret> <tag>: push the bundle to the Hub with oras
  oras_in "$WORK/bundle" push -u "$1" -p "$2" \
    --config config.json:application/vnd.pixi.config.v1+toml \
    "$HUB_REGISTRY/$REPO:$3" \
    pixi.toml:application/vnd.pixi.toml.v1+toml \
    pixi.lock:application/vnd.pixi.lock.v1+yaml \
    COG.md:application/vnd.nebi.asset.v1 \
    weights.bin:application/vnd.nebi.asset.v1
}

echo "== publish through the Hub: nobody without the permission, nothing without a publish credential"
code="$(curl -s -o "$WORK/other.json" -w '%{http_code}' -X POST -H "Authorization: Bearer $OTHER_TOKEN" \
  -H 'Content-Type: application/json' -d '{"scope":"publish"}' "$HUB/v1/cogs/registry-credentials")"
[ "$code" = "403" ] || die "a user without the publish permission got $code from the publish exchange: $(cat "$WORK/other.json")"
pull_only="$(hub -X POST "$HUB/v1/cogs/registry-credentials")"
if push_cog "$(printf '%s' "$pull_only" | jq -r .username)" "$(printf '%s' "$pull_only" | jq -r .secret)" 0.1.0 >/dev/null 2>&1; then
  die "a pull credential pushed"
fi
if push_cog "$BACKING_USER" "$BACKING_PASSWORD" 0.1.0 >/dev/null 2>&1; then die "the backing credential pushed through the Hub"; fi
pass "publish refused without the permission, with a pull credential, and with the backing credential"

publish_credential="$(hub -X POST -H 'Content-Type: application/json' -d '{"scope":"publish"}' "$HUB/v1/cogs/registry-credentials")"
[ "$(printf '%s' "$publish_credential" | jq -r .scope)" = "publish" ] || die "publish exchange answered: $publish_credential"
PUB_USER="$(printf '%s' "$publish_credential" | jq -r .username)"
PUB_SECRET="$(printf '%s' "$publish_credential" | jq -r .secret)"

# A bundle the catalog could not index is refused at the manifest, with the reader's reason.
if oras_in "$WORK/plain" push -u "$PUB_USER" -p "$PUB_SECRET" "$HUB_REGISTRY/$REPO:plain" README.txt:text/plain >"$WORK/plain.log" 2>&1; then
  die "a bundle that is not a Cog was accepted"
fi
grep -qi "MANIFEST_INVALID\|manifest invalid" "$WORK/plain.log" || { cat "$WORK/plain.log" >&2; die "the refusal did not say MANIFEST_INVALID"; }
[ "$(hub "$HUB/v1/cogs" | jq -r '.items | length')" = "0" ] || die "a refused bundle was listed"
pass "a bundle that would not index is refused with MANIFEST_INVALID, and nothing is listed"

push_cog "$PUB_USER" "$PUB_SECRET" 0.1.0 >"$WORK/push.log" 2>&1 || { cat "$WORK/push.log" >&2; die "oras push through the Hub failed"; }
# Listed at once: no sweep has had to find it.
listed="$(hub "$HUB/v1/cogs")"
[ "$(printf '%s' "$listed" | jq -r '.items | length')" = "1" ] || die "the published Cog is not listed: $listed"
[ "$(printf '%s' "$listed" | jq -r '.items[0].published_by')" = "e2e-user" ] || die "the publisher was not recorded: $listed"
[ "$(printf '%s' "$listed" | jq -r '.items[0].published_org')" = "e2e-org" ] || die "the publisher's organization was not recorded"
pass "oras push through the Hub into $BACKING: listed immediately, publisher recorded, no repository list configured"

# Another organization cannot overwrite it, even with the permission... which it does not have here; and
# the repository is now owned: the same user under another organization is refused.
foreign="$(curl -sS -X POST -H "Authorization: Bearer $(bearer_for e2e-user other-org)" -H 'Content-Type: application/json' -d '{"scope":"publish"}' "$HUB/v1/cogs/registry-credentials")"
if push_cog "$(printf '%s' "$foreign" | jq -r .username)" "$(printf '%s' "$foreign" | jq -r .secret)" 0.2.0 >"$WORK/foreign.log" 2>&1; then
  die "another organization pushed to an owned repository"
fi
pass "a repository published through the Hub is its organization's: another organization is refused"

# One token, push on this repository and pull on another: it pushes to this one only.
mixed="$(curl -sS -u "$PUB_USER:$PUB_SECRET" \
  "$HUB/v2/token?service=$HUB_REGISTRY&scope=repository:$REPO:pull,push&scope=repository:cogs/elsewhere:pull" | jq -r .token)"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $mixed" "$HUB/v2/cogs/elsewhere/blobs/uploads/")"
[ "$code" = "403" ] || die "push on one repository opened an upload in another asked for pull only ($code)"
session="$(curl -sS -o /dev/null -D - -X POST -H "Authorization: Bearer $mixed" "$HUB/v2/$REPO/blobs/uploads/" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')"
case "$session" in /v2/"$REPO"/blobs/uploads/up-*) ;; *) die "opening an upload answered Location '$session'" ;; esac
code="$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $mixed" "$HUB$session")"
[ "$code" = "204" ] || die "cancelling an upload answered $code"
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $mixed" "$HUB$session")"
[ "$code" = "404" ] || die "a cancelled upload still answers $code"
pass "push is per repository; an upload is opened and cancelled through the Hub's own session path"

# Pushed straight to the registry, behind the Hub's back, and not a Cog: the Hub must never serve it.
oras_in "$WORK/plain" push --plain-http -u "$BACKING_USER" -p "$BACKING_PASSWORD" \
  "$BACKING_ADDR/$REPO:plain" README.txt:text/plain >/dev/null
oras_in "$WORK" manifest fetch --plain-http -u "$BACKING_USER" -p "$BACKING_PASSWORD" \
  "$BACKING_ADDR/$REPO:0.1.0" > "$WORK/backing-manifest.json"

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
mkdir -p "$WORK/anon" "$WORK/out" "$WORK/bytag" "$WORK/latest"
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

echo "== a tag moved to a new version is on that version alone, and a sweep leaves it there"
push_cog "$PUB_USER" "$PUB_SECRET" latest >"$WORK/push.log" 2>&1 || { cat "$WORK/push.log" >&2; die "tagging the published version failed"; }
head -c 300000 /dev/urandom > "$WORK/bundle/weights.bin"
push_cog "$PUB_USER" "$PUB_SECRET" latest >"$WORK/push.log" 2>&1 || { cat "$WORK/push.log" >&2; die "publishing a second version failed"; }
check_latest() { # <when>
  local versions
  versions="$(hub "$HUB/v1/cogs/example/cog-audio-transcriber" | jq -c '[.versions[] | select(.removed_at == null) | {digest, tags, published_by}]')"
  [ "$(printf '%s' "$versions" | jq -r '[.[] | select(.tags | index("latest"))] | length')" = "1" ] || die "$1: 'latest' is not on exactly one version: $versions"
  [ "$(printf '%s' "$versions" | jq -r --arg d "$DIGEST" '.[] | select(.digest == $d) | .tags | join(",")')" = "0.1.0" ] || die "$1: the first version's tags are wrong: $versions"
  [ "$(printf '%s' "$versions" | jq -r '[.[] | select(.published_by != "e2e-user")] | length')" = "0" ] || die "$1: a publisher was lost: $versions"
}
check_latest "after the publish"
oras_in "$WORK/latest" pull -u "$REG_USER" -p "$REG_SECRET" "$HUB_REGISTRY/$REPO:latest" >/dev/null || die "oras pull of the moved tag failed"
cmp -s "$WORK/bundle/weights.bin" "$WORK/latest/weights.bin" || die "the moved tag does not serve the new version"
sleep 12   # at least one sweep (every 10 seconds here) has now reconciled the repository
check_latest "after a sweep"
pass "a moved tag is on exactly one version, before and after a sweep; publishers kept"

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
if grep -q "localhost:$BACKING_PORT/v2/.*blobs/uploads" "$WORK/hub.log"; then die "an upload session URL reached the Hub's log"; fi
pass "no backing address or credential in any response; no secret, and no upload session URL, in the log"

echo "== result: PASSED ($BACKING)"
