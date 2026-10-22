#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
. "${SCRIPT_DIR}/smoke_frames_common.sh"

NAMESPACE="${NAMESPACE:-collab-hub}"
RELEASE="${RELEASE:-collab-hub}"
LOCAL_PORT="${LOCAL_PORT:-18088}"
PYTHON_BIN="$(resolve_python "${PYTHON_BIN:-}")"
SERVICE_NAME="${SERVICE_NAME:-$(chart_service_name "${RELEASE}" "${ROOT_DIR}")}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-$(chart_deployment_name "${RELEASE}" "${ROOT_DIR}")}"
REQUEST_ID_PREFIX="${REQUEST_ID_PREFIX:-obs-smoke}"

kubectl -n "${NAMESPACE}" port-forward "svc/${SERVICE_NAME}" "${LOCAL_PORT}:80" >/tmp/collab-hub-frames-observability-port-forward.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} >/dev/null 2>&1 || true' EXIT
sleep 3

FRAME_ID="$("${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_frames_http.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}" \
    --request-id-prefix "${REQUEST_ID_PREFIX}" \
    --check-active-state \
    --keep-frame | tail -n 1)"

# /metrics is protected by the path-protection map, so the scrape
# carries the same smoke credentials the REST calls above use.
METRICS_COOKIE="$(PYTHONPATH="${SCRIPT_DIR}" "${PYTHON_BIN}" -c \
    'from smoke_frames_mcp import auth_headers; print(auth_headers()["Cookie"])')"
METRICS="$(curl -sf -H "Cookie: ${METRICS_COOKIE}" "http://127.0.0.1:${LOCAL_PORT}/metrics")"
echo "${METRICS}" | grep 'frames_server_http_requests_total'
echo "${METRICS}" | grep 'frames_server_audit_events_total{action="frame_create"}'
echo "${METRICS}" | grep 'frames_server_http_request_duration_seconds_bucket'

for _ in $(seq 1 20); do
    LOGS="$(kubectl -n "${NAMESPACE}" logs "deployment/${DEPLOYMENT_NAME}" --tail=300)"
    if echo "${LOGS}" | grep -q "\"request_id\":\"${REQUEST_ID_PREFIX}-create\"" \
        && echo "${LOGS}" | grep -q '"action":"frame_create"' \
        && echo "${LOGS}" | grep -q "\"frame_id\":\"${FRAME_ID}\""; then
        exit 0
    fi
    sleep 1
done

kubectl -n "${NAMESPACE}" logs "deployment/${DEPLOYMENT_NAME}" --tail=300
echo "expected request/audit logs for frame ${FRAME_ID}" >&2
exit 1
