#!/usr/bin/env bash
set -euo pipefail

# install.sh — deploy rag-gateway into:
#   /opt/llm/rag-gateway/     (application)
#   /etc/rag-gateway/         (config)
#   /etc/systemd/system/      (systemd units)
#   /opt/llm/models/          (predownloaded models)

APP_DIR="/opt/llm/rag-gateway"
CONF_DIR="/etc/rag-gateway"
SYSTEMD_DIR="/etc/systemd/system"
MODELS_DIR="/opt/llm/models"

# Supporting service directories (required by included unit files)
QDRANT_DIR="/opt/llm/qdrant"
TEI_DIR="/opt/llm/tei"
TEI_CACHE_DIR="/opt/llm/tei/cache"
VLLM_DIR="/opt/llm/vllm"
HF_CACHE_DIR="/opt/llm/hf"

# Where rag-gateway should store its state/indexes
VAR_DIR="${APP_DIR}/var"
TANTIVY_DIR="${VAR_DIR}/tantivy"
SCHED_DIR="${VAR_DIR}/scheduler"
SCHED_LOCK="${SCHED_DIR}/crawl.lock"

timestamp() { date +"%Y%m%d-%H%M%S"; }
log() { printf '%s\n' "[$(date -Is)] $*"; }
warn() { printf '%s\n' "[$(date -Is)] WARNING: $*" >&2; }
die() { printf '%s\n' "[$(date -Is)] ERROR: $*" >&2; exit 1; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "This script must be run as root."
  fi
}

detect_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  # Expect this script to live in the repo root
  if [[ -f "${script_dir}/pyproject.toml" && -d "${script_dir}/rag_gateway" && -d "${script_dir}/systemd" && -d "${script_dir}/etc" ]]; then
    echo "${script_dir}"
    return 0
  fi

  die "Could not detect repo root. Expected pyproject.toml + rag_gateway/ + etc/ + systemd/ next to install.sh."
}

ensure_group_user() {
  local name="$1"
  local home="${2:-/nonexistent}"
  local shell="${3:-/usr/sbin/nologin}"

  if ! getent group "${name}" >/dev/null 2>&1; then
    log "Creating group: ${name}"
    groupadd --system "${name}"
  fi

  if ! id -u "${name}" >/dev/null 2>&1; then
    log "Creating system user: ${name}"
    useradd --system \
      --gid "${name}" \
      --home-dir "${home}" \
      --shell "${shell}" \
      --no-create-home \
      "${name}"
  fi
}

ensure_base_dirs() {
  log "Creating base directories"
  install -d -m 0755 /opt/llm
  install -d -m 0755 "${APP_DIR}"
  install -d -m 0755 "${CONF_DIR}"
  install -d -m 0755 "${MODELS_DIR}"

  # upstream service dirs (as referenced by unit files)
  install -d -m 0755 "${QDRANT_DIR}" "${QDRANT_DIR}/config"
  install -d -m 0755 "${TEI_DIR}" "${TEI_CACHE_DIR}"
  install -d -m 0755 "${VLLM_DIR}"
  install -d -m 0755 "${HF_CACHE_DIR}"
}

deploy_app() {
  local repo_root="$1"

  log "Deploying application code to ${APP_DIR}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude ".venv/" \
      --exclude "var/" \
      --exclude "etc/" \
      --exclude "systemd/" \
      --exclude ".git/" \
      "${repo_root}/" "${APP_DIR}/"
  else
    warn "rsync not found; falling back to cp -a (no deletion of removed files)"
    cp -a "${repo_root}/." "${APP_DIR}/"
    rm -rf "${APP_DIR}/etc" "${APP_DIR}/systemd" "${APP_DIR}/.git" 2>/dev/null || true
  fi

  chown -R rag:rag "${APP_DIR}"
}

setup_gateway_venv() {
  log "Ensuring Python virtualenv exists at ${APP_DIR}/.venv"
  if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    python3 -m venv "${APP_DIR}/.venv"
    chown -R rag:rag "${APP_DIR}/.venv"
  fi

  log "Installing/updating gateway Python dependencies"
  sudo -u rag "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
  sudo -u rag "${APP_DIR}/.venv/bin/python" -m pip install -e "${APP_DIR}" >/dev/null
}

ensure_rag_state_dirs() {
  # IMPORTANT: create var/ AFTER pip install -e to avoid setuptools seeing it as a top-level package
  log "Creating rag-gateway runtime state directories"
  install -d -m 0755 "${VAR_DIR}" "${TANTIVY_DIR}" "${SCHED_DIR}"
  chown -R rag:rag "${VAR_DIR}"
}

backup_if_exists() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    local bak="${path}.$(timestamp).bak"
    cp -a "${path}" "${bak}"
    log "Backed up ${path} -> ${bak}"
  fi
}

deploy_configs() {
  local repo_root="$1"

  log "Deploying config files to ${CONF_DIR}"
  install -d -m 0755 "${CONF_DIR}"

  # config.yaml
  if [[ ! -f "${CONF_DIR}/config.yaml" ]]; then
    install -m 0640 -o root -g rag "${repo_root}/etc/config.yaml" "${CONF_DIR}/config.yaml"
  else
    backup_if_exists "${CONF_DIR}/config.yaml"
  fi

  # sources.yaml (typically user-curated; do not overwrite if it exists)
  if [[ ! -f "${CONF_DIR}/sources.yaml" ]]; then
    install -m 0640 -o root -g rag "${repo_root}/etc/sources.yaml" "${CONF_DIR}/sources.yaml"
  else
    log "Keeping existing ${CONF_DIR}/sources.yaml (not overwritten)"
  fi

  # Patch config.yaml paths to match the desired layout (safe, preserves other values)
  log "Patching ${CONF_DIR}/config.yaml paths for this filesystem layout"
  python3 - <<PY
import sys
from pathlib import Path

try:
    import yaml
except Exception as e:
    print(f"ERROR: PyYAML is required to patch config.yaml: {e}", file=sys.stderr)
    sys.exit(1)

p = Path("${CONF_DIR}/config.yaml")
raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

raw.setdefault("paths", {})
raw["paths"]["tantivy_index_dir"] = "${TANTIVY_DIR}"

raw.setdefault("scheduler", {})
raw["scheduler"]["state_dir"] = "${SCHED_DIR}"
raw["scheduler"]["lock_file"] = "${SCHED_LOCK}"
raw["scheduler"]["sources_file"] = "${CONF_DIR}/sources.yaml"

p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
PY

  chown root:rag "${CONF_DIR}/config.yaml"
  chmod 0640 "${CONF_DIR}/config.yaml"
}

deploy_systemd_units() {
  local repo_root="$1"

  log "Deploying systemd unit files to ${SYSTEMD_DIR}"
  install -d -m 0755 "${SYSTEMD_DIR}"

  local unit
  for unit in "${repo_root}/systemd/"*.service "${repo_root}/systemd/"*.timer; do
    [[ -e "${unit}" ]] || continue
    local dst="${SYSTEMD_DIR}/$(basename "${unit}")"
    if [[ -f "${dst}" ]]; then
      backup_if_exists "${dst}"
    fi
    install -m 0644 "${unit}" "${dst}"
  done

  # Patch unit files to reference /opt/llm/models for predownloaded models
  log "Patching unit files to use ${MODELS_DIR} for model locations"

  sed -i \
    -e 's|--model-id /opt/llm/tei/models/Qwen3-Embedding-8B|--model-id /opt/llm/models/embeddings/Qwen3-Embedding-8B|g' \
    "${SYSTEMD_DIR}/tei-embed.service" 2>/dev/null || true

  sed -i \
    -e 's|--model-id /opt/llm/tei/models/bge-reranker-large|--model-id /opt/llm/models/rerank/bge-reranker-large|g' \
    "${SYSTEMD_DIR}/tei-rerank.service" 2>/dev/null || true

  sed -i \
    -e 's|--model /opt/llm/vllm/models/DeepSeek-R1-Distill-Qwen-32B|--model /opt/llm/models/generator/DeepSeek-R1-Distill-Qwen-32B|g' \
    "${SYSTEMD_DIR}/vllm.service" 2>/dev/null || true

  systemctl daemon-reload
}

fix_permissions() {
  log "Setting ownership/permissions"

  # rag-gateway owns its app + state
  chown -R rag:rag "${APP_DIR}"
  chmod -R u+rwX,go+rX "${APP_DIR}"

  # config readable by rag group
  chown -R root:rag "${CONF_DIR}"
  chmod 0755 "${CONF_DIR}"
  chmod 0640 "${CONF_DIR}/config.yaml" 2>/dev/null || true
  chmod 0640 "${CONF_DIR}/sources.yaml" 2>/dev/null || true

  # service dirs
  chown -R qdrant:qdrant "${QDRANT_DIR}" || true
  chown -R tei:tei "${TEI_DIR}" || true
  chown -R vllm:vllm "${VLLM_DIR}" "${HF_CACHE_DIR}" || true

  # models: readable by service users (adjust to your policy as needed)
  chmod -R a+rX "${MODELS_DIR}"
}

enable_start_basics() {
  log "Enabling and starting rag-gateway + rag-crawl.timer"
  systemctl enable --now rag-gateway.service
  systemctl enable --now rag-crawl.timer
}

main() {
  need_root
  local repo_root
  repo_root="$(detect_repo_root)"

  log "Repo root detected: ${repo_root}"

  ensure_group_user rag
  ensure_group_user qdrant
  ensure_group_user tei
  ensure_group_user vllm

  ensure_base_dirs
  deploy_app "${repo_root}"
  setup_gateway_venv
  ensure_rag_state_dirs
  deploy_configs "${repo_root}"
  deploy_systemd_units "${repo_root}"
  fix_permissions
  enable_start_basics

  log "Install complete."
  log "Gateway config: ${CONF_DIR}/config.yaml"
  log "Gateway unit:   systemctl status rag-gateway.service"
  log "Crawl timer:    systemctl status rag-crawl.timer"
}

main "$@"

