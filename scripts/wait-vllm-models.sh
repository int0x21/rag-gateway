#!/usr/bin/env bash
set -euo pipefail

# Wait until the vLLM OpenAI-compatible endpoint is reachable AND has at least
# one model loaded. Optionally assert that a specific model id is present.
#
# Dependencies: curl, python3

NAME="vllm"
URL=""
TIMEOUT=120
SLEEP_SECS=1
MIN_MODELS=1
EXPECT_ID=""
VERBOSE_EVERY=10

usage() {
  cat <<'USAGE'
wait-vllm-models.sh

Required:
  --url <url>           vLLM /v1/models endpoint URL (e.g., http://127.0.0.1:8000/v1/models)

Optional:
  --timeout <seconds>   Total time to wait (default: 120)
  --sleep <seconds>     Sleep between polls (default: 1)
  --min-models <n>      Require at least N models in the response (default: 1)
  --expect-id <id>      Require that a model with .id == <id> exists
  --name <name>         Friendly name for log output (default: vllm)
USAGE
}

log() {
  echo "[$(date --iso-8601=seconds)] wait-vllm: $*" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --sleep) SLEEP_SECS="$2"; shift 2;;
    --min-models) MIN_MODELS="$2"; shift 2;;
    --expect-id) EXPECT_ID="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *)
      log "Unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

if [[ -z "${URL}" ]]; then
  usage
  exit 2
fi

start_epoch="$(date +%s)"
end_epoch="$((start_epoch + TIMEOUT))"

log "Waiting for ${NAME} models at ${URL} timeout=${TIMEOUT}s min_models=${MIN_MODELS} expect_id='${EXPECT_ID}'"

attempt=0
while true; do
  now="$(date +%s)"
  if (( now >= end_epoch )); then
    log "Timeout reached waiting for ${NAME} models"
    exit 1
  fi

  attempt=$((attempt + 1))

  # Pull models list. Keep curl timeouts short to avoid hanging systemd.
  body=""
  if ! body="$(curl --noproxy "*" -sS --show-error --max-time 3 "${URL}" 2>/dev/null)"; then
    sleep "${SLEEP_SECS}"
    continue
  fi

  # Validate JSON and compute conditions using python (dependency-light).
  # Exit codes:
  #   0 -> ready
  #   1 -> not ready yet
  if python3 - "${MIN_MODELS}" "${EXPECT_ID}" <<'PY' <<<"${body}"; then
import json, sys

min_models = int(sys.argv[1])
expect_id = sys.argv[2]

try:
    data = json.loads(sys.stdin.read())
except Exception:
    raise SystemExit(1)

models = data.get("data", [])
if not isinstance(models, list):
    raise SystemExit(1)

if len(models) < min_models:
    raise SystemExit(1)

if expect_id:
    if not any(isinstance(m, dict) and m.get("id") == expect_id for m in models):
        raise SystemExit(1)

raise SystemExit(0)
PY
  then
    log "${NAME} is ready (models loaded)"
    exit 0
  fi

  if (( attempt % VERBOSE_EVERY == 0 )); then
    # Provide a tiny hint without spamming logs.
    snippet="$(echo "${body}" | head -c 200 | tr '\n' ' ' || true)"
    log "${NAME} not ready yet. Snippet: ${snippet}"
  fi

  sleep "${SLEEP_SECS}"
done

