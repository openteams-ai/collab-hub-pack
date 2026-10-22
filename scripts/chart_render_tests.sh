#!/usr/bin/env sh
# Render tests for the collab-hub chart's Cog registry block (issue #87).
#
# The validations templates fail the render on purpose; helm lint alone never
# exercises those branches, and a values fixture that renders proves only the
# happy path. This script renders the positive cases and asserts on their
# output, then hands every negative case (one fixture, shared with the API's
# tests) to chart_rules_parity.py. Run from anywhere; needs `helm` on PATH (or
# HELM=... to point at a wrapper, e.g. a docker alias).
#
#   scripts/chart_render_tests.sh
#   HELM="docker run --rm -v $PWD:$PWD -w $PWD alpine/helm" PYTHON="uv run --project api python" scripts/chart_render_tests.sh
set -eu

HELM="${HELM:-helm}"
PYTHON="${PYTHON:-python3}"   # needs PyYAML; the assertions live in chart_render_check.py
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHART="${CHART:-$ROOT/helm/collab-hub}"
CI_VALUES="$CHART/ci/cogs-values.yaml"
TESTDATA="$ROOT/scripts/testdata/chart"
failures=0

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n     %s\n' "$1" "$2"; failures=$((failures + 1)); }

# check <name> <case in chart_render_check.py> [helm args...]
check() {
  name="$1"; case="$2"; shift 2
  if out="$($HELM template t "$CHART" "$@" 2>&1)"; then
    if err="$(printf '%s' "$out" | $PYTHON "$ROOT/scripts/chart_render_check.py" "$case" 2>&1)"; then
      pass "$name"
    else
      fail "$name" "$err"
    fi
  else
    fail "$name" "render failed: $(printf '%s' "$out" | head -3)"
  fi
}

STATIC='--set cogs.registry.sources[0].id=public --set cogs.registry.sources[0].kind=static --set cogs.registry.sources[0].url=https://registry.example.com --set cogs.registry.sources[0].repositories[0]=cogs/alpha'

# --- positive -----------------------------------------------------------------
check "default values: only the enabled flag, no CA mount" default
check "ci fixture: harbor + static, Secrets attached, CA mounted read-only" fixture -f "$CI_VALUES"
check "sources with the indexer off: JSON renders, tuning vars do not" static-only $STATIC
check "three sources: each credential attached to its own source" three-sources -f "$TESTDATA/cogs-three-sources.yaml"
check "middle source removed and order reversed: attachments follow the id" reordered -f "$TESTDATA/cogs-reordered.yaml"
NEBARIAPP='--set api.nebariapp.enabled=true --set api.nebariapp.hostname=hub.example.com'
HTTPROUTE='--set api.ingress.enabled=true --set api.ingress.kind=HTTPRoute --set api.ingress.host=hub.example.com --set api.ingress.parentRefs[0].name=gateway --set server.forwardedAllowIps[0]=10.0.0.0/8'
check "serving off: no serve variables, the gateway's public routes untouched" serve-off $STATIC $NEBARIAPP
ENVOY_CRDS='--api-versions gateway.envoyproxy.io/v1alpha1/BackendTrafficPolicy'
check "serving on behind the Nebari gateway: URL derived, /v2 made a public route with a timeout policy" serve-nebariapp $STATIC $NEBARIAPP $ENVOY_CRDS --set cogs.serve.enabled=true
check "serving on behind the Nebari gateway with routeTimeout off: no policy" serve-nebariapp-no-timeout $STATIC $NEBARIAPP --set cogs.serve.enabled=true --set cogs.serve.routeTimeout=false
check "serving and sweeping on: the serve settings reach the API replicas, not the indexer" serve-with-indexer $STATIC $HTTPROUTE --set cogs.serve.enabled=true --set cogs.index.enabled=true
check "serving on behind an HTTPRoute: explicit URL, integer limits, /v2 rule with a timeout" serve-httproute $STATIC $HTTPROUTE --set cogs.serve.enabled=true --set cogs.serve.publicUrl=https://hub.example.com:8443 --set cogs.serve.maxBlobBytes=5368709120 --set cogs.serve.maxBlobSeconds=1800 --set cogs.serve.credentialTtlSeconds=600
check "serving on with routeTimeout off: no /v2 rule, no timeout" serve-httproute-no-timeout $STATIC $HTTPROUTE --set cogs.serve.enabled=true --set cogs.serve.routeTimeout=false
check "indexer on with its own resources: one Recreate replica, the API's env, the API does not sweep" indexer-resources $STATIC --set cogs.index.enabled=true --set cogs.indexer.resources.requests.memory=1Gi --set cogs.indexer.resources.limits.memory=2Gi
check "feature flags: each entry renders as its variable" features --set features.cogs_ui=true --set features.new_thing=false
refused() { # <label> <needle> <helm args...>: the chart must refuse to render, naming <needle>
  local label="$1" needle="$2" out
  shift 2
  if out="$($HELM template t "$CHART" "$@" 2>&1)"; then
    fail "$label" "rendered"
  elif printf '%s' "$out" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label" "failed for another reason: $(printf '%s' "$out" | head -3)"
  fi
}
refused "feature flags: a non-boolean value is refused" "/features/cogs_ui" --set-string features.cogs_ui=maybe
refused "feature flags: a dashed name is refused" "cogs-ui" --set features.cogs-ui=true
refused "feature flags: a name with a double underscore is refused" "cogs__ui" --set features.cogs__ui=true
refused "feature flags: an uppercase name is refused" "Cogs_ui" --set features.Cogs_ui=true

# The indexer's replica count is fixed in the template (issue #148): there is
# no value to set, so the schema refuses one.
refused "indexer: a replica count is not a value" "replicas" $STATIC --set cogs.index.enabled=true --set cogs.indexer.replicas=2

# server.rootPath is a prefix with a leading and no trailing slash; config.py
# refuses the same values (api/tests/test_path_protection.py).
refused "root path: a trailing slash is refused" "/server/rootPath" --set server.rootPath=/hub/
refused "root path: a bare slash is refused" "/server/rootPath" --set server.rootPath=/
refused "root path: a missing leading slash is refused" "/server/rootPath" --set server.rootPath=hub

# Without Envoy Gateway the /v2 timeout policy cannot exist; the render says so
# rather than leaving large pulls on the gateway's 15 s default.
refused "serving on behind the Nebari gateway without Envoy Gateway's CRDs is refused, not left on the default timeout" "cogs.serve.routeTimeout" $STATIC $NEBARIAPP --set cogs.serve.enabled=true

# --- negative: every refused configuration, from one fixture ------------------
# scripts/testdata/chart/cogs-negative-cases.yaml holds each refusal once, in
# two forms: the chart values and the settings the chart renders from them.
# chart_rules_parity.py renders every case and asserts the refusal, proves the
# two forms equivalent by rendering without the rules, and checks that every
# fail() in templates/cogs-validations.yaml fired. api/tests/test_config_cogs.py
# feeds the same fixture to the API's Config, so a rule that drifts between the
# schema, the template and config.py fails one side or the other.
if ! $PYTHON "$ROOT/scripts/chart_rules_parity.py" --helm "$HELM" --chart "$CHART" "$TESTDATA/cogs-negative-cases.yaml"; then
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf '\n%d chart render test(s) failed\n' "$failures"
  exit 1
fi
printf '\nall chart render tests passed\n'
