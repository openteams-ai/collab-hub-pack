#!/usr/bin/env bash
# Kind end-to-end for Cog execution: materialize real Cog worker pods and run a
# gated multi-step Op via the KubernetesCogExecutor. Separate from the app code
# (api/); it builds the app's execution module into an image and runs it.
#
#   scripts/cog-execution-e2e/run.sh          # create/reuse kind, build, run, assert
#   KEEP=1 scripts/cog-execution-e2e/run.sh   # leave the cluster up afterward
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"   # repo root = docker build context
CLUSTER="${KIND_CLUSTER:-cog-e2e}"
CTX="kind-${CLUSTER}"
IMAGE="collab-hub/cog-e2e:test"

echo "== ensure kind cluster '${CLUSTER}'"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER"

echo "== build + load the E2E image (context: repo root)"
# --pull: re-resolve the base image, so a rebuild picks up one republished under the
# same tag (a ca-certificates fix, say) rather than a stale local layer.
docker build --pull -t "$IMAGE" -f "$HERE/Dockerfile" "$ROOT"
kind load docker-image "$IMAGE" --name "$CLUSTER"

echo "== apply namespace, RBAC, and the driver Job"
kubectl --context "$CTX" delete job op-driver -n cogs-e2e --ignore-not-found >/dev/null 2>&1 || true
kubectl --context "$CTX" apply -f "$HERE/k8s.yaml"

echo "== wait for the driver Job (materializes cog pods, runs the gated Op)"
deadline=$(( SECONDS + 300 ))
status=""
while [ $SECONDS -lt $deadline ]; do
  if kubectl --context "$CTX" -n cogs-e2e get job op-driver -o jsonpath='{.status.succeeded}' | grep -q 1; then status=PASSED; break; fi
  if kubectl --context "$CTX" -n cogs-e2e get job op-driver -o jsonpath='{.status.failed}' | grep -q 1; then status=FAILED; break; fi
  sleep 3
done

echo "== driver logs"
kubectl --context "$CTX" -n cogs-e2e logs job/op-driver || true

[ "${KEEP:-0}" = "1" ] || { echo "== tearing down kind cluster (KEEP=1 to keep it)"; kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; }

echo "== result: ${status:-TIMEOUT}"
[ "$status" = "PASSED" ]
