#!/usr/bin/env bash
set -euo pipefail

TS() { date --iso-8601=seconds; }
log() { echo "[$(TS)] $*"; }

usage() {
  cat <<'EOF'
wait-vllm-models.sh
  Wait for vLLM / OpenAI-compatible /v1/models endpoint to return at least N models
  and optionally contain specific model IDs.

Usage:
  wait-vllm-models.sh --name vllm --url http://127.0.0.1:8000/v1/models \
    --timeout 180 --min-models 1 --expect-id generator

Options:
  --name NAME           Friendly name for logs (default: vllm)
  --url URL             Models endpoint URL (required)
  --timeout SECONDS     Total time to wait (default: 180)
  --interval SECONDS    Poll interval (default: 1)
  --max-time SECONDS    Per-request timeout for curl (default: 3)
  --min-models N        Minimum number of models required (default: 1)
  --expect-id ID        Expected model id (repeatable)

Exit codes:
  0 = ready
  1 = timeout
  2 = usage / argument error
  3 = check failed (transient; only meaningful inside loops/systemd)
EOF
}

NAME="vllm"
URL=""
TIMEOUT=180
INTERVAL=1
MAX_TIME=3
MIN_MODELS=1
EXPECT_IDS=()

# --- arg parsing ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2;;
    --url) URL="${2:-}"; shift 2;;
    --timeout) TIMEOUT="${2:-}"; shift 2;;
    --interval) INTERVAL="${2:-}"; shift 2;;
    --max-time) MAX_TIME="${2:-}"; shift 2;;
    --min-models) MIN_MODELS="${2:-}"; shift 2;;
    --expect-id) EXPECT_IDS+=("${2:-}"); shift 2;;
    -h|--help) usage; exit 0;;
    *)
      log "wait-vllm-models: Unknown arg: $1"
      usage
      exit 2
      ;;
  esac
done

if [[ -z "${URL}" ]]; then
  log "wait-vllm-models: --url is required"
  usage
  exit 2
fi

if ! [[ "${TIMEOUT}" =~ ^[0-9]+$ ]] || ! [[ "${INTERVAL}" =~ ^[0-9]+$ ]] || ! [[ "${MAX_TIME}" =~ ^[0-9]+$ ]] || ! [[ "${MIN_MODELS}" =~ ^[0-9]+$ ]]; then
  log "wait-vllm-models: timeout/interval/max-time/min-models must be integers"
  exit 2
fi

# --- check function ---
# Returns:
#   0 ready
#   3 not ready yet / transient failure
check_once() {
  local tmp rc http_code body

  tmp="$(mktemp)"
  # Use curl to capture HTTP code and body separately.
  # -sS: silent but show errors
  # --max-time: per call timeout
  # Accept header: encourages JSON
  http_code="$(
    curl -sS \
      --noproxy "*" \
      --max-time "${MAX_TIME}" \
      -H "Accept: application/json" \
      -o "${tmp}" \
      -w "%{http_code}" \
      "${URL}" \
      || true
  )"

  body="$(cat "${tmp}" 2>/dev/null || true)"
  rm -f "${tmp}" || true

  # If curl failed hard, http_code may be empty.
  if [[ -z "${http_code}" ]]; then
    return 3
  fi

  # Non-200 -> not ready yet
  if [[ "${http_code}" != "200" ]]; then
    return 3
  fi

  # Empty body -> not ready yet
  if [[ -z "${body}" ]]; then
    return 3
  fi

  # Parse JSON and evaluate conditions using python (no jq dependency).
  # Prints a single line "OK" when ready.
  rc=0
  python3 - <<PY 2>/dev/null || rc=$?
import json, sys

raw = ${body@Q}
try:
    doc = json.loads(raw)
except Exception:
    sys.exit(3)

data = doc.get("data")
if not isinstance(data, list):
    sys.exit(3)

ids = []
for item in data:
    if isinstance(item, dict) and "id" in item:
        ids.append(str(item["id"]))

min_models = int(${MIN_MODELS})
expect = ${EXPECT_IDS[@]+"["$(printf '"%s",' "${EXPECT_IDS[@]}" | sed 's/,$//')"]"}

if len(ids) < min_models:
    sys.exit(3)

for e in expect:
    if e not in ids:
        sys.exit(3)

print("OK")
PY

  if [[ $rc -ne 0 ]]; then
    return 3
  fi

  return 0
}

log "wait-vllm-models: Waiting for ${NAME} models (${URL}) timeout=${TIMEOUT}s min_models=${MIN_MODELS} expect_ids=${EXPECT_IDS[*]:-}"

start_epoch="$(date +%s)"
deadline=$((start_epoch + TIMEOUT))

while true; do
  if check_once; then
    log "wait-vllm-models: ${NAME} models are ready"
    exit 0
  fi

  now="$(date +%s)"
  if (( now >= deadline )); then
    log "wait-vllm-models: TIMEOUT waiting for ${NAME} models"
    exit 1
  fi

  log "wait-vllm-models: ${NAME} not ready yet (check_failed rc=3)"
  sleep "${INTERVAL}"
done

