#!/usr/bin/env bash
set -euo pipefail

# wait-http.sh
#
# Polls an HTTP endpoint until it returns a successful status code (2xx/3xx).
# Optionally validates the response body using an extended regular expression.
#
# Designed for systemd ExecStartPre= usage.

name=""
url=""
method="GET"
timeout=60
json=""
expect_regex=""

usage() {
  cat <<'USAGE'
Usage:
  wait-http.sh --name NAME --url URL [--method GET|POST] [--timeout SECONDS]
               [--json JSON_STRING] [--expect ERE_REGEX]

Notes:
  - Success = HTTP 2xx/3xx AND (if --expect is provided) body matches regex.
  - --expect uses grep -E (extended regex).
USAGE
}

log() {
  echo "[$(date --iso-8601=seconds)] wait-http: $*"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) name="$2"; shift 2;;
    --url) url="$2"; shift 2;;
    --method) method="$2"; shift 2;;
    --timeout) timeout="$2"; shift 2;;
    --json) json="$2"; shift 2;;
    --expect) expect_regex="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "${name}" || -z "${url}" ]]; then
  usage
  exit 2
fi

start_ts="$(date +%s)"

log "Waiting for ${name} (${method} ${url}) timeout=${timeout}s"

while true; do
  now_ts="$(date +%s)"
  elapsed=$(( now_ts - start_ts ))
  if (( elapsed >= timeout )); then
    log "ERROR: timeout waiting for ${name} after ${timeout}s"
    exit 1
  fi

  tmp="$(mktemp)"
  http_code=""
  curl_rc=0

  if [[ "${method}" == "POST" ]]; then
    if [[ -n "${json}" ]]; then
      http_code="$(
        curl --noproxy "*" -sS --show-error --max-time 3 \
          -H "Content-Type: application/json" \
          -X POST \
          -o "${tmp}" \
          -w "%{http_code}" \
          "${url}" \
          -d "${json}" 2>/dev/null
      )" || curl_rc=$?
    else
      http_code="$(
        curl --noproxy "*" -sS --show-error --max-time 3 \
          -X POST \
          -o "${tmp}" \
          -w "%{http_code}" \
          "${url}" 2>/dev/null
      )" || curl_rc=$?
    fi
  else
    http_code="$(
      curl --noproxy "*" -sS --show-error --max-time 3 \
        -X GET \
        -o "${tmp}" \
        -w "%{http_code}" \
        "${url}" 2>/dev/null
    )" || curl_rc=$?
  fi

  body=""
  if [[ -f "${tmp}" ]]; then
    body="$(cat "${tmp}" 2>/dev/null || true)"
    rm -f "${tmp}" || true
  fi

  if (( curl_rc != 0 )); then
    sleep 1
    continue
  fi

  # Accept 2xx/3xx
  if [[ "${http_code}" =~ ^2[0-9][0-9]$ || "${http_code}" =~ ^3[0-9][0-9]$ ]]; then
    if [[ -n "${expect_regex}" ]]; then
      if echo "${body}" | grep -Eq "${expect_regex}"; then
        log "${name} is ready (http=${http_code}, expect matched)"
        exit 0
      fi

      # helpful but not noisy: show a short hint
      log "${name} responded http=${http_code} but did not match --expect; retrying"
      sleep 1
      continue
    fi

    log "${name} is ready (http=${http_code})"
    exit 0
  fi

  sleep 1
done

