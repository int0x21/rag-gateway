#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/llm/rag-gateway"
CONF_DIR="/etc/rag-gateway"
SYSTEMD_DIR="/etc/systemd/system"
MODELS_DIR="/opt/llm/models"

QDRANT_DIR="/opt/llm/qdrant"
TEI_DIR="/opt/llm/tei"
VLLM_DIR="/opt/llm/vllm"
VLLM_VENV_DIR="/opt/llm/vllm/.venv"

HF_CACHE_DIR="/opt/llm/hf"
MODEL_TOOLS_DIR="/opt/llm/model-tools"

log()  { printf '%s\n' "[$(date -Is)] $*"; }
warn() { printf '%s\n' "[$(date -Is)] WARNING: $*" >&2; }
die()  { printf '%s\n' "[$(date -Is)] ERROR: $*" >&2; exit 1; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "This script must be run as root."
  fi
}

PROJECT_UNITS=(
  "rag-stack.target"
  "rag-gateway.service"
  "qdrant.service"
  "tei-embed.service"
  "tei-rerank.service"
  "vllm.service"
)


stop_disable_units() {
  log "Stopping/disabling units (if present)"
  systemctl stop rag-stack.target 2>/dev/null || true
  systemctl disable rag-stack.target 2>/dev/null || true

  for unit in "${PROJECT_UNITS[@]}"; do
    systemctl stop "${unit}" 2>/dev/null || true
    systemctl disable "${unit}" 2>/dev/null || true
  done
}


remove_units_dropins_and_baks() {
  log "Removing unit files, drop-ins, and .bak backups"

  for unit in "${PROJECT_UNITS[@]}"; do
    rm -f "${SYSTEMD_DIR}/${unit}" 2>/dev/null || true
    rm -f "${SYSTEMD_DIR}/${unit}."*.bak 2>/dev/null || true
    rm -rf "${SYSTEMD_DIR}/${unit}.d" 2>/dev/null || true
  done

  # Remove common wants symlinks if they exist
  rm -f "${SYSTEMD_DIR}/multi-user.target.wants/rag-stack.target" 2>/dev/null || true
  rm -f "${SYSTEMD_DIR}/timers.target.wants/rag-crawl.timer" 2>/dev/null || true

  systemctl daemon-reload

  # Clear failed state entries
  for unit in "${PROJECT_UNITS[@]}"; do
    systemctl reset-failed "${unit}" 2>/dev/null || true
  done
  systemctl reset-failed 2>/dev/null || true
}

remove_vllm_dir_but_keep_venv() {
  # User requirement: keep /opt/llm/vllm/.venv to avoid long reinstall times.
  if [[ -d "${VLLM_DIR}" ]]; then
    if [[ -d "${VLLM_VENV_DIR}" ]]; then
      log "Removing ${VLLM_DIR} contents except ${VLLM_VENV_DIR}"
      # Delete everything directly under VLLM_DIR except .venv
      find "${VLLM_DIR}" -mindepth 1 -maxdepth 1 \
        ! -name ".venv" \
        -exec rm -rf {} + 2>/dev/null || true
      log "Kept vLLM virtualenv: ${VLLM_VENV_DIR}"
    else
      log "Removing ${VLLM_DIR} (no .venv present to preserve)"
      rm -rf "${VLLM_DIR}" || true
    fi
  fi
}

remove_paths_keep_models() {
  local purge_crawl="$1"
  local purge_models="$2"

  log "Removing application/config/support dirs"

  rm -rf "${APP_DIR}" || true
  rm -rf "${CONF_DIR}" || true

  if [[ "${purge_crawl}" == "true" ]]; then
    rm -rf "${QDRANT_DIR}" || true
    log "Removed Qdrant data directory: ${QDRANT_DIR}"
  else
    if [[ -d "${QDRANT_DIR}" ]]; then
      log "Preserved Qdrant data directory: ${QDRANT_DIR}"
    fi
  fi

  rm -rf "${TEI_DIR}" || true
  remove_vllm_dir_but_keep_venv

  rm -rf "${HF_CACHE_DIR}" || true
  rm -rf "${MODEL_TOOLS_DIR}" || true

  if [[ "${purge_models}" == "true" ]]; then
    rm -rf "${MODELS_DIR}" || true
    log "Removed models directory: ${MODELS_DIR}"
  else
    if [[ -d "${MODELS_DIR}" ]]; then
      log "Preserved models directory: ${MODELS_DIR}"
    else
      warn "Models directory not found: ${MODELS_DIR}"
    fi
  fi
}

remove_cli_tool() {
  rm -f "/usr/local/bin/rag-gateway-crawl"
  log "Removed CLI tool"
}

main() {
  PURGE_CRAWL=false
  PURGE_MODELS=false

  while [[ $# -gt 0 ]]; do
    case $1 in
      --purge-crawl)
        PURGE_CRAWL=true
        shift
        ;;
      --purge-models)
        PURGE_MODELS=true
        shift
        ;;
      --help)
        echo "Usage: $0 [--purge-crawl] [--purge-models]"
        echo "  --purge-crawl: Remove Qdrant database and crawled data"
        echo "  --purge-models: Remove downloaded models"
        echo "  Default: Preserve both crawl data and models"
        exit 0
        ;;
      *)
        die "Unknown option: $1"
        ;;
    esac
  done

  need_root
  stop_disable_units
  remove_units_dropins_and_baks
  remove_paths_keep_models "${PURGE_CRAWL}" "${PURGE_MODELS}"
  remove_cli_tool

  log "Uninstall complete."
  if [[ "${PURGE_CRAWL}" == "false" ]]; then
    log "Note: Crawled data in ${QDRANT_DIR} was preserved."
  fi
  if [[ "${PURGE_MODELS}" == "false" ]]; then
    log "Note: Models in ${MODELS_DIR} were preserved."
  fi
  if [[ "${PURGE_CRAWL}" == "false" || "${PURGE_MODELS}" == "false" ]]; then
    log "Use '$0 --purge-crawl --purge-models' for complete removal."
  fi
  log "To confirm there are no failed units:"
  log "  systemctl --failed"
}


main "$@"

