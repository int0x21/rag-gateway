#!/usr/bin/env bash
set -euo pipefail

NAME=""
URL=""
METHOD="GET"
JSON_PAYLOAD=""
TIMEOUT=60
INTERVAL=2

log() {
  printf '[%s] wait-http: %s\n' "$(date --iso-8601=seconds)" "$*"
}

usage() {
  cat <<EOF
Usage: wait-http.sh --name <name> --url <url> [--method GET|POST] [--json '<json>'] [--timeout <sec>] [--interval <sec>]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --method) METHOD="$2"; shift 2;;
    --json) JSON_PAYLOAD="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$NAME" || -z "$URL" ]]; then
  usage
  exit 2
fi

log "Waiting for ${NAME} (${METHOD} ${URL}) timeout=${TIMEOUT}s interval=${INTERVAL}s"

deadline=$(( $(date +%s) + TIMEOUT ))
attempt=0

while (( $(date +%s) < deadline )); do
  attempt=$((attempt + 1))

  if [[ -n "$JSON_PAYLOAD" ]]; then
    # Capture HTTP status without printing body
    code="$(curl --noproxy "*" -sS -o /dev/null -w '%{http_code}' \
      --max-time 5 -H "Content-Type: application/json" -X "$METHOD" \
      "$URL" -d "$JSON_PAYLOAD" || true)"
  else
    code="$(curl --noproxy "*" -sS -o /dev/null -w '%{http_code}' \
      --max-time 5 -X "$METHOD" "$URL" || true)"
  fi

  if [[ "$code" =~ ^2[0-9]{2}$ ]]; then
    log "${NAME} is ready (HTTP ${code})"
    exit 0
  fi

  if (( attempt % 10 == 0 )); then
    remaining=$(( deadline - $(date +%s) ))
    log "Still waiting for ${NAME} (last HTTP ${code:-err}); remaining ${remaining}s"
  fi

  sleep "$INTERVAL"
done

log "ERROR: timeout waiting for ${NAME} (${METHOD} ${URL})"
exit 1

