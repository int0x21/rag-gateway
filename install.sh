#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/llm/rag-gateway"
CONF_DIR="/etc/rag-gateway"
SYSTEMD_DIR="/etc/systemd/system"
MODELS_DIR="/opt/llm/models"

QDRANT_DIR="/opt/llm/qdrant"
TEI_DIR="/opt/llm/tei"
TEI_CACHE_DIR="/opt/llm/tei/cache"
VLLM_DIR="/opt/llm/vllm"
HF_CACHE_DIR="/opt/llm/hf"

APP_BIN_DIR="${APP_DIR}/bin"

VAR_DIR="${APP_DIR}/var"
TANTIVY_DIR="${VAR_DIR}/tantivy"
SCHED_DIR="${VAR_DIR}/scheduler"
SCHED_LOCK="${SCHED_DIR}/crawl.lock"

GENERATOR_DIR="${MODELS_DIR}/generator/DeepSeek-R1-Distill-Qwen-32B"
EMBED_DIR="${MODELS_DIR}/embeddings/Qwen3-Embedding-8B"
RERANK_DIR="${MODELS_DIR}/rerank/bge-reranker-large"

STACK_TARGET="rag-stack.target"
REQUIRED_UNITS=(
  "qdrant.service"
  "tei-embed.service"
  "tei-rerank.service"
  "vllm.service"
  "rag-gateway.service"
  "rag-crawl.timer"
)

# Tunable timeouts (seconds)
: "${VERIFY_QDRANT_TIMEOUT:=120}"
: "${VERIFY_TEI_TIMEOUT:=600}"
: "${VERIFY_VLLM_TIMEOUT:=900}"
: "${VERIFY_GATEWAY_TIMEOUT:=600}"
: "${VERIFY_TIMER_TIMEOUT:=60}"

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
    useradd --system --gid "${name}" --home-dir "${home}" --shell "${shell}" --no-create-home "${name}"
  fi
}

ensure_base_dirs() {
  log "Creating base directories"
  install -d -m 0755 /opt/llm
  install -d -m 0755 "${APP_DIR}" "${APP_BIN_DIR}"
  install -d -m 0755 "${CONF_DIR}"
  install -d -m 0755 "${MODELS_DIR}"

  install -d -m 0755 "${QDRANT_DIR}" "${QDRANT_DIR}/config"
  install -d -m 0755 "${TEI_DIR}" "${TEI_CACHE_DIR}"
  install -d -m 0755 "${VLLM_DIR}"
  install -d -m 0755 "${HF_CACHE_DIR}"
}

ensure_models_present() {
  local repo_root="$1"

  if [[ "${SKIP_MODEL_DOWNLOAD:-0}" == "1" ]]; then
    warn "SKIP_MODEL_DOWNLOAD=1 set; skipping model presence checks/downloads."
    return 0
  fi

  local missing=0
  [[ -d "${GENERATOR_DIR}" ]] || missing=1
  [[ -d "${EMBED_DIR}" ]] || missing=1
  [[ -d "${RERANK_DIR}" ]] || missing=1

  if [[ "${missing}" -eq 0 ]]; then
    log "Models present under ${MODELS_DIR}"
    return 0
  fi

  log "One or more required model directories are missing; downloading models now."
  local dl="${repo_root}/scripts/download-models.sh"
  [[ -x "${dl}" ]] || die "Missing or non-executable: ${dl} (run: chmod +x scripts/download-models.sh)"
  "${dl}"

  [[ -d "${GENERATOR_DIR}" ]] || die "Generator model still missing: ${GENERATOR_DIR}"
  [[ -d "${EMBED_DIR}" ]] || die "Embedding model still missing: ${EMBED_DIR}"
  [[ -d "${RERANK_DIR}" ]] || die "Rerank model still missing: ${RERANK_DIR}"
  log "Model download complete and verified."
}

detect_python_for_vllm() {
  local cand
  for cand in python3.11 python3.10 python3; do
    if command -v "${cand}" >/dev/null 2>&1; then
      echo "${cand}"
      return 0
    fi
  done
  return 1
}

ensure_vllm_installed() {
  if [[ "${SKIP_VLLM_INSTALL:-0}" == "1" ]]; then
    warn "SKIP_VLLM_INSTALL=1 set; not installing vLLM. vllm.service may not start."
    return 0
  fi

  if [[ -x "${VLLM_DIR}/.venv/bin/vllm" ]]; then
    log "vLLM already installed: ${VLLM_DIR}/.venv/bin/vllm"
    return 0
  fi

  log "Installing vLLM into ${VLLM_DIR}/.venv"
  install -d -m 0755 "${VLLM_DIR}" "${HF_CACHE_DIR}"
  chown -R vllm:vllm "${VLLM_DIR}" "${HF_CACHE_DIR}"

  local py
  py="$(detect_python_for_vllm)" || die "No python3 interpreter found."

  sudo -u vllm "${py}" -m venv "${VLLM_DIR}/.venv"
  sudo -u vllm "${VLLM_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
  sudo -u vllm "${VLLM_DIR}/.venv/bin/pip" install --upgrade "vllm" >/dev/null

  [[ -x "${VLLM_DIR}/.venv/bin/vllm" ]] || die "vLLM install completed but CLI missing."
  log "vLLM installed successfully."
}

deploy_app() {
  local repo_root="$1"
  log "Deploying application code to ${APP_DIR}"

  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude ".venv/" \
      --exclude "var/" \
      --exclude "bin/" \
      --exclude "etc/" \
      --exclude "systemd/" \
      --exclude "scripts/" \
      --exclude ".git/" \
      "${repo_root}/" "${APP_DIR}/"
  else
    warn "rsync not found; falling back to cp -a (no deletion of removed files)"
    cp -a "${repo_root}/." "${APP_DIR}/"
    rm -rf "${APP_DIR}/etc" "${APP_DIR}/systemd" "${APP_DIR}/scripts" "${APP_DIR}/.git" 2>/dev/null || true
  fi

  chown -R rag:rag "${APP_DIR}"
}

deploy_runtime_helpers() {
  local repo_root="$1"
  log "Deploying runtime helper scripts to ${APP_BIN_DIR}"
  install -d -m 0755 "${APP_BIN_DIR}"
  install -m 0755 "${repo_root}/scripts/wait-http.sh" "${APP_BIN_DIR}/wait-http.sh"
  chown root:root "${APP_BIN_DIR}/wait-http.sh"
  chmod 0755 "${APP_BIN_DIR}/wait-http.sh"
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

  if [[ ! -f "${CONF_DIR}/config.yaml" ]]; then
    install -m 0640 -o root -g rag "${repo_root}/etc/config.yaml" "${CONF_DIR}/config.yaml"
  else
    backup_if_exists "${CONF_DIR}/config.yaml"
  fi

  if [[ ! -f "${CONF_DIR}/sources.yaml" ]]; then
    install -m 0640 -o root -g rag "${repo_root}/etc/sources.yaml" "${CONF_DIR}/sources.yaml"
  else
    log "Keeping existing ${CONF_DIR}/sources.yaml (not overwritten)"
  fi

  log "Patching ${CONF_DIR}/config.yaml paths for this filesystem layout"
  python3 - <<PY
from pathlib import Path
import yaml

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

  log "Deploying systemd units to ${SYSTEMD_DIR}"
  install -d -m 0755 "${SYSTEMD_DIR}"

  local unit
  for unit in "${repo_root}/systemd/"*.service "${repo_root}/systemd/"*.timer "${repo_root}/systemd/"*.target; do
    [[ -e "${unit}" ]] || continue
    local dst="${SYSTEMD_DIR}/$(basename "${unit}")"
    if [[ -f "${dst}" ]]; then
      backup_if_exists "${dst}"
    fi
    install -m 0644 "${unit}" "${dst}"
  done

  if [[ -d "${repo_root}/systemd/overrides" ]]; then
    log "Deploying systemd drop-in overrides"
    (cd "${repo_root}/systemd/overrides" && find . -type f -name "*.conf" -print0) | \
      while IFS= read -r -d '' f; do
        local rel="${f#./}"
        local dst_dir="${SYSTEMD_DIR}/$(dirname "${rel}")"
        install -d -m 0755 "${dst_dir}"
        install -m 0644 "${repo_root}/systemd/overrides/${rel}" "${SYSTEMD_DIR}/${rel}"
      done
  fi

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

  chown -R rag:rag "${APP_DIR}"
  chmod -R u+rwX,go+rX "${APP_DIR}"

  chown -R root:rag "${CONF_DIR}"
  chmod 0755 "${CONF_DIR}"
  chmod 0640 "${CONF_DIR}/config.yaml" 2>/dev/null || true
  chmod 0640 "${CONF_DIR}/sources.yaml" 2>/dev/null || true

  chown -R qdrant:qdrant "${QDRANT_DIR}" || true
  chown -R tei:tei "${TEI_DIR}" || true
  chown -R vllm:vllm "${VLLM_DIR}" "${HF_CACHE_DIR}" || true

  chmod -R a+rX "${MODELS_DIR}"
}

enable_start_stack() {
  log "Enabling ${STACK_TARGET}"
  systemctl daemon-reload
  systemctl enable "${STACK_TARGET}"

  log "Starting ${STACK_TARGET} (non-blocking)"
  systemctl start --no-block "${STACK_TARGET}"

  # Optional: show any queued systemd jobs so the operator sees progress immediately
  systemctl list-jobs --no-pager || true
}

wait_unit_active_verbose() {
  local unit="$1"
  local timeout="$2"
  local deadline=$(( $(date +%s) + timeout ))
  local i=0

  while (( $(date +%s) < deadline )); do
    if systemctl is-active --quiet "${unit}"; then
      return 0
    fi

    # Print a progress line every 10 seconds to avoid "hang" perception
    if (( i % 10 == 0 )); then
      local st
      st="$(systemctl show -p ActiveState -p SubState --value "${unit}" 2>/dev/null | tr '\n' ' ')"
      log "Waiting for ${unit} (${st:-unknown})... ${i}/${timeout}s"
    fi
    i=$((i+1))
    sleep 1
  done
  return 1
}

unit_timeout_for() {
  case "$1" in
    qdrant.service) echo "${VERIFY_QDRANT_TIMEOUT}" ;;
    tei-embed.service|tei-rerank.service) echo "${VERIFY_TEI_TIMEOUT}" ;;
    vllm.service) echo "${VERIFY_VLLM_TIMEOUT}" ;;
    rag-gateway.service) echo "${VERIFY_GATEWAY_TIMEOUT}" ;;
    rag-crawl.timer) echo "${VERIFY_TIMER_TIMEOUT}" ;;
    *) echo 120 ;;
  esac
}

verify_stack_active() {
  log "Verifying stack units are active"
  local failed=0

  for u in "${REQUIRED_UNITS[@]}"; do
    local load_state
    load_state="$(systemctl show -p LoadState --value "${u}" 2>/dev/null || echo "unknown")"
    if [[ "${load_state}" != "loaded" ]]; then
      warn "NOT LOADED: ${u} (LoadState=${load_state})"
      failed=1
      continue
    fi

    local t
    t="$(unit_timeout_for "${u}")"

    if wait_unit_active_verbose "${u}" "${t}"; then
      log "OK: ${u} is active"
      continue
    fi

    warn "NOT ACTIVE after ${t}s: ${u}"
    failed=1

    systemctl status "${u}" -l --no-pager || true
    journalctl -u "${u}" -b --no-pager -n 200 || true
  done

  if [[ "${failed}" -ne 0 ]]; then
    warn "Showing failed units (if any):"
    systemctl --failed || true
    die "Stack verification failed."
  fi

  log "All required units are active."
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
  ensure_models_present "${repo_root}"
  ensure_vllm_installed

  deploy_app "${repo_root}"
  deploy_runtime_helpers "${repo_root}"
  setup_gateway_venv
  ensure_rag_state_dirs
  deploy_configs "${repo_root}"
  deploy_systemd_units "${repo_root}"
  fix_permissions

  enable_start_stack
  verify_stack_active

  log "Install complete."
  log "Tip: tune verification timeouts via env vars, e.g.: VERIFY_VLLM_TIMEOUT=300 ./install.sh"
}

main "$@"

