#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/llm/rag-gateway"
CONF_DIR="/etc/rag-gateway"
SYSTEMD_DIR="/etc/systemd/system"
MODELS_DIR="/opt/llm/models"

QDRANT_DIR="/opt/llm/qdrant"
TEI_DIR="/opt/llm/tei"
VLLM_DIR="/opt/llm/vllm"
HF_CACHE_DIR="/opt/llm/hf"

log() { printf '%s\n' "[$(date -Is)] $*"; }
warn() { printf '%s\n' "[$(date -Is)] WARNING: $*" >&2; }
die() { printf '%s\n' "[$(date -Is)] ERROR: $*" >&2; exit 1; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "This script must be run as root."
  fi
}

PROJECT_UNITS=(
  "rag-stack.target"
  "rag-gateway.service"
  "rag-crawl.service"
  "rag-crawl.timer"
  "qdrant.service"
  "tei-embed.service"
  "tei-rerank.service"
  "vllm.service"
)

stop_disable_units() {
  log "Stopping/disabling units (if present)"

  # Stop stack first (it will stop PartOf units too)
  systemctl stop rag-stack.target 2>/dev/null || true
  systemctl disable rag-stack.target 2>/dev/null || true

  # Stop timer explicitly
  systemctl stop rag-crawl.timer 2>/dev/null || true
  systemctl disable rag-crawl.timer 2>/dev/null || true

  # Stop/disable services
  for unit in "${PROJECT_UNITS[@]}"; do
    systemctl stop "${unit}" 2>/dev/null || true
    systemctl disable "${unit}" 2>/dev/null || true
  done
}

remove_units_dropins_and_baks() {
  log "Removing unit files, drop-ins, and .bak backups"

  for unit in "${PROJECT_UNITS[@]}"; do
    # unit file
    rm -f "${SYSTEMD_DIR}/${unit}" 2>/dev/null || true

    # backups created by install.sh: <unit>.<timestamp>.bak
    rm -f "${SYSTEMD_DIR}/${unit}."*.bak 2>/dev/null || true

    # drop-in directory: <unit>.d/
    rm -rf "${SYSTEMD_DIR}/${unit}.d" 2>/dev/null || true
  done

  # Also remove any wants symlinks that may linger
  rm -f "${SYSTEMD_DIR}/multi-user.target.wants/rag-stack.target" 2>/dev/null || true
  rm -f "${SYSTEMD_DIR}/multi-user.target.wants/rag-gateway.service" 2>/dev/null || true
  rm -f "${SYSTEMD_DIR}/timers.target.wants/rag-crawl.timer" 2>/dev/null || true

  systemctl daemon-reload

  # Clear failed state entries (this is what removes "not-found failed failed")
  for unit in "${PROJECT_UNITS[@]}"; do
    systemctl reset-failed "${unit}" 2>/dev/null || true
  done
  systemctl reset-failed 2>/dev/null || true
}

remove_paths_keep_models() {
  log "Removing application/config/support dirs (keeping ${MODELS_DIR})"

  rm -rf "${APP_DIR}" || true
  rm -rf "${CONF_DIR}" || true

  rm -rf "${QDRANT_DIR}" || true
  rm -rf "${TEI_DIR}" || true
  rm -rf "${VLLM_DIR}" || true
  rm -rf "${HF_CACHE_DIR}" || true

  if [[ -d "${MODELS_DIR}" ]]; then
    log "Kept models directory: ${MODELS_DIR}"
  else
    warn "Models directory not found: ${MODELS_DIR}"
  fi
}

main() {
  need_root
  stop_disable_units
  remove_units_dropins_and_baks
  remove_paths_keep_models

  log "Uninstall complete."
  log "To confirm there are no failed units:"
  log "  systemctl --failed"
}

main "$@"

