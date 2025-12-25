#!/usr/bin/env bash
set -euo pipefail

NAME=""
URL=""
METHOD="GET"
JSON_BODY=""
TIMEOUT=60
INTERVAL=1
MAX_TIME_PER_REQ=3

usage() {
  cat <<'EOF'
wait-http.sh --name NAME --url URL [--method GET|POST|PUT] [--json JSON] [--timeout SECONDS]

Examples:
  wait-http.sh --name qdrant --url http://127.0.0.1:6333/collections --timeout 60
  wait-http.sh --name tei-embed --url http://127.0.0.1:8081/health --timeout 120
EOF
}

ts() { date -Is; }

log() { echo "[$(ts)] wait-http: $*"; }

fail() { echo "[$(ts)] wait-http: ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --method) METHOD="${2^^}"; shift 2;;
    --json) JSON_BODY="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --max-time) MAX_TIME_PER_REQ="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) fail "Unknown argument: $1";;
  esac
done

[[ -n "$NAME" ]] || fail "--name is required"
[[ -n "$URL" ]] || fail "--url is required"

start_epoch="$(date +%s)"
deadline=$(( start_epoch + TIMEOUT ))

log "Waiting for ${NAME} (${METHOD} ${URL}) timeout=${TIMEOUT}s"

last_code=""
last_body=""

while true; do
  now="$(date +%s)"
  if (( now >= deadline )); then
    fail "${NAME} not ready after ${TIMEOUT}s (last HTTP=${last_code:-n/a}) body=${last_body:-<empty>}"
  fi

  tmp_body="$(mktemp)"
  http_code=""

  # Do NOT use -f, because we want to capture the body/status for diagnostics.
  if [[ "$METHOD" == "GET" ]]; then
    http_code="$(curl --noproxy "*" -sS -o "$tmp_body" -w "%{http_code}" --max-time "$MAX_TIME_PER_REQ" "$URL" || true)"
  else
    # If sending JSON, force correct header.
    if [[ -n "$JSON_BODY" ]]; then
      http_code="$(curl --noproxy "*" -sS -o "$tmp_body" -w "%{http_code}" --max-time "$MAX_TIME_PER_REQ" \
        -H "Content-Type: application/json" -X "$METHOD" --data-raw "$JSON_BODY" "$URL" || true)"
    else
      http_code="$(curl --noproxy "*" -sS -o "$tmp_body" -w "%{http_code}" --max-time "$MAX_TIME_PER_REQ" \
        -X "$METHOD" "$URL" || true)"
    fi
  fi

  body="$(tr -d '\r' <"$tmp_body" | head -c 300 || true)"
  rm -f "$tmp_body"

  # Success on any 2xx/3xx.
  if [[ "$http_code" =~ ^2|^3 ]]; then
    log "${NAME} is ready (HTTP ${http_code})"
    exit 0
  fi

  # Emit a periodic hint so it never looks “silently stuck”.
  # Keep it low-noise: print only when status changes or every ~10 seconds.
  if [[ "$http_code" != "$last_code" || $(( (now - start_epoch) % 10 )) -eq 0 ]]; then
    log "${NAME} not ready yet (HTTP ${http_code:-n/a}) body=${body:-<empty>}"
  fi

  last_code="$http_code"
  last_body="$body"

  sleep "$INTERVAL"
done

