#!/usr/bin/env bash
set -euo pipefail

# uninstall.sh — remove rag-gateway installation while KEEPING predownloaded models
#
# Removes:
#   - /opt/llm/rag-gateway
#   - /etc/rag-gateway
#   - systemd units for this project (and any *.bak created by install.sh)
#   - runtime/support dirs created for services: /opt/llm/qdrant /opt/llm/tei /opt/llm/vllm /opt/llm/hf
#
# Keeps:
#   - /opt/llm/models

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

# Units included in this project (match your install.sh deploy list)
PROJECT_UNITS=(
  "rag-gateway.service"
  "rag-crawl.service"
  "rag-crawl.timer"
  "qdrant.service"
  "tei-embed.service"
  "tei-rerank.service"
  "vllm.service"
)

stop_disable_units() {
  log "Stopping/disabling systemd units (if present)"

  # Stop timer first to prevent it from triggering while tearing down
  if systemctl list-unit-files | awk '{print $1}' | grep -qx "rag-crawl.timer"; then
    systemctl stop "rag-crawl.timer" || true
    systemctl disable "rag-crawl.timer" || true
  fi

  # Stop/disable services
  for unit in "${PROJECT_UNITS[@]}"; do
    if systemctl list-unit-files | awk '{print $1}' | grep -qx "${unit}"; then
      systemctl stop "${unit}" || true
      systemctl disable "${unit}" || true
    fi
  done
}

remove_unit_files_and_baks() {
  log "Removing unit files and install-created backups from ${SYSTEMD_DIR}"

  for unit in "${PROJECT_UNITS[@]}"; do
    rm -f "${SYSTEMD_DIR}/${unit}" 2>/dev/null || true

    # Remove any backup files created by install.sh:
    # e.g. rag-gateway.service.20251225-111437.bak
    rm -f "${SYSTEMD_DIR}/${unit}."*.bak 2>/dev/null || true
  done

  systemctl daemon-reload
  systemctl reset-failed || true
}

remove_paths() {
  log "Removing application and config directories (keeping ${MODELS_DIR})"

  rm -rf "${APP_DIR}" || true
  rm -rf "${CONF_DIR}" || true

  # Remove supporting directories created for services, to ensure a clean slate
  rm -rf "${QDRANT_DIR}" || true
  rm -rf "${TEI_DIR}" || true
  rm -rf "${VLLM_DIR}" || true
  rm -rf "${HF_CACHE_DIR}" || true

  if [[ -d "${MODELS_DIR}" ]]; then
    log "Keeping predownloaded models directory: ${MODELS_DIR}"
  else
    warn "Models directory not found (nothing to keep): ${MODELS_DIR}"
  fi
}

print_next_steps() {
  log "Uninstall complete."
  log "Kept models: ${MODELS_DIR}"
  log "To reinstall from scratch:"
  log "  cd /opt/llm/src/rag-gateway && git pull && sudo ./install.sh"
}

main() {
  need_root
  stop_disable_units
  remove_unit_files_and_baks
  remove_paths
  print_next_steps
}

main "$@"

