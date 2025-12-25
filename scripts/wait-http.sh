#!/usr/bin/env bash
set -euo pipefail

NAME="service"
URL=""
METHOD="GET"
JSON_BODY=""
TIMEOUT=180
SLEEP=1

log() { printf '%s\n' "[$(date -Is)] wait-http: $*"; }
die() { printf '%s\n' "[$(date -Is)] wait-http: ERROR: $*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: wait-http.sh --name <name> --url <url> [--method GET|POST] [--json '<json>'] [--timeout <seconds>] [--sleep <seconds>]

Examples:
  wait-http.sh --name qdrant --url http://127.0.0.1:6333/collections --timeout 180
  wait-http.sh --name tei-embed --url http://127.0.0.1:8081/v1/embeddings --method POST --json '{"model":"X","input":"ready"}' --timeout 600
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --method) METHOD="$2"; shift 2;;
    --json) JSON_BODY="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --sleep) SLEEP="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) die "Unknown arg: $1";;
  esac
done

[[ -n "${URL}" ]] || die "--url is required"

deadline=$(( $(date +%s) + TIMEOUT ))

# Keep curl quiet unless final failure
curl_args=(--noproxy '*' -fsS --max-time 3)

log "Waiting for ${NAME} (${METHOD} ${URL}) timeout=${TIMEOUT}s"

while (( $(date +%s) < deadline )); do
  if [[ "${METHOD}" == "GET" ]]; then
    if curl "${curl_args[@]}" "${URL}" >/dev/null 2>&1; then
      log "${NAME} is ready"
      exit 0
    fi
  elif [[ "${METHOD}" == "POST" ]]; then
    if [[ -z "${JSON_BODY}" ]]; then
      die "--json is required for POST"
    fi
    if curl "${curl_args[@]}" -H "Content-Type: application/json" -X POST "${URL}" -d "${JSON_BODY}" >/dev/null 2>&1; then
      log "${NAME} is ready"
      exit 0
    fi
  else
    die "Unsupported --method: ${METHOD} (use GET or POST)"
  fi

  sleep "${SLEEP}"
done

# Final attempt with error output for diagnosis
log "Timed out waiting for ${NAME}. Last attempt output:"
if [[ "${METHOD}" == "GET" ]]; then
  curl --noproxy '*' -S "${URL}" || true
else
  curl --noproxy '*' -S -H "Content-Type: application/json" -X POST "${URL}" -d "${JSON_BODY}" || true
fi

die "${NAME} not ready after ${TIMEOUT}s"

