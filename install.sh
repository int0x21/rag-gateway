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
DATA_DIR="${VAR_DIR}/data"
CACHE_DIR="${VAR_DIR}/cache"
LOG_DIR="${VAR_DIR}/logs"

# system-wide runtime dirs for services (owned by service users)
RUNTIME_STATE_DIR="/var/lib/rag-gateway"
RUNTIME_LOG_DIR="/var/log/rag-gateway"

# Virtualenv paths
APP_VENV="${APP_DIR}/.venv"
VLLM_VENV="${VLLM_DIR}/.venv"

# Repo root (autodetected)
REPO_ROOT=""

# Verification tuning
VERIFY_QDRANT_TIMEOUT="${VERIFY_QDRANT_TIMEOUT:-180}"
VERIFY_TEI_TIMEOUT="${VERIFY_TEI_TIMEOUT:-180}"
VERIFY_VLLM_TIMEOUT="${VERIFY_VLLM_TIMEOUT:-300}"
VERIFY_GATEWAY_TIMEOUT="${VERIFY_GATEWAY_TIMEOUT:-120}"
VERIFY_TIMER_TIMEOUT="${VERIFY_TIMER_TIMEOUT:-60}"

log() {
  # ISO 8601 with timezone, matches your prior logs
  echo "[$(date --iso-8601=seconds)] $*"
}

die() {
  log "ERROR: $*"
  exit 1
}

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    die "This script must be run as root."
  fi
}

detect_repo_root() {
  # Prefer the directory containing this script
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

  # If it’s a git repo, use git top-level
  if command -v git >/dev/null 2>&1 && git -C "${script_dir}" rev-parse --show-toplevel >/dev/null 2>&1; then
    REPO_ROOT="$(git -C "${script_dir}" rev-parse --show-toplevel)"
  else
    REPO_ROOT="${script_dir}"
  fi

  log "Repo root detected: ${REPO_ROOT}"
}

ensure_group_user() {
  local name="$1"
  local home="${2:-/nonexistent}"
  local shell="${3:-/usr/sbin/nologin}"

  if ! getent group "${name}" >/dev/null 2>&1; then
    log "Creating system group: ${name}"
    groupadd --system "${name}"
  else
    log "System group exists: ${name}"
  fi

  if ! id -u "${name}" >/dev/null 2>&1; then
    log "Creating system user: ${name}"
    useradd --system --gid "${name}" --home-dir "${home}" --shell "${shell}" --no-create-home "${name}"
  else
    log "System user exists: ${name}"
  fi

  # Hard validation (prevents silent 217/USER loops later)
  if ! id -u "${name}" >/dev/null 2>&1; then
    die "Required system user '${name}' is still missing after creation attempt."
  fi
}

create_base_directories() {
  log "Creating base directories"

  mkdir -p "${MODELS_DIR}" \
           "${QDRANT_DIR}" \
           "${TEI_DIR}" \
           "${TEI_CACHE_DIR}" \
           "${VLLM_DIR}" \
           "${HF_CACHE_DIR}" \
           "${APP_DIR}" \
           "${APP_BIN_DIR}" \
           "${VAR_DIR}" \
           "${TANTIVY_DIR}" \
           "${DATA_DIR}" \
           "${CACHE_DIR}" \
           "${LOG_DIR}" \
           "${RUNTIME_STATE_DIR}" \
           "${RUNTIME_LOG_DIR}"

  # Ensure service users/groups exist (must match systemd unit User=)
  ensure_group_user "qdrant" "/nonexistent" "/usr/sbin/nologin"
  ensure_group_user "tei"    "/nonexistent" "/usr/sbin/nologin"
  ensure_group_user "vllm"   "/nonexistent" "/usr/sbin/nologin"
  ensure_group_user "rag"    "/nonexistent" "/usr/sbin/nologin"
}

ensure_models_present() {
  if [[ -d "${MODELS_DIR}" ]] && find "${MODELS_DIR}" -mindepth 2 -maxdepth 3 -type f >/dev/null 2>&1; then
    log "Models present under ${MODELS_DIR}"
    return 0
  fi

  log "No models detected under ${MODELS_DIR}; attempting to download via scripts/download-models.sh"
  if [[ -x "${REPO_ROOT}/scripts/download-models.sh" ]]; then
    "${REPO_ROOT}/scripts/download-models.sh"
  else
    die "scripts/download-models.sh is missing or not executable."
  fi
}

install_vllm() {
  log "Installing vLLM into ${VLLM_VENV}"
  mkdir -p "${VLLM_DIR}"

  if [[ ! -d "${VLLM_VENV}" ]]; then
    python3 -m venv "${VLLM_VENV}"
  fi

  "${VLLM_VENV}/bin/pip" install --upgrade pip wheel setuptools >/dev/null

  # This assumes you want vLLM installed in this venv; dependencies come from your repo lock/requirements.
  if [[ -f "${REPO_ROOT}/requirements-vllm.txt" ]]; then
    "${VLLM_VENV}/bin/pip" install -r "${REPO_ROOT}/requirements-vllm.txt"
  else
    # fallback: install vllm directly (pinning recommended in requirements-vllm.txt)
    "${VLLM_VENV}/bin/pip" install "vllm"
  fi

  log "vLLM installed successfully."
}

deploy_application_code() {
  log "Deploying application code to ${APP_DIR}"
  rsync -a --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '.venv' \
    --exclude '.mypy_cache' \
    --exclude '.pytest_cache' \
    --exclude 'var' \
    "${REPO_ROOT}/app/" "${APP_DIR}/app/"
}

deploy_runtime_scripts() {
  log "Deploying runtime helper scripts to ${APP_BIN_DIR}"
  rsync -a --delete "${REPO_ROOT}/scripts/" "${APP_BIN_DIR}/"
  chmod -R a+rx "${APP_BIN_DIR}"
}

ensure_gateway_venv() {
  log "Ensuring Python virtualenv exists at ${APP_VENV}"
  if [[ ! -d "${APP_VENV}" ]]; then
    python3 -m venv "${APP_VENV}"
  fi
  "${APP_VENV}/bin/pip" install --upgrade pip wheel setuptools >/dev/null
}

install_gateway_dependencies() {
  log "Installing/updating gateway Python dependencies"
  if [[ -f "${REPO_ROOT}/requirements.txt" ]]; then
    "${APP_VENV}/bin/pip" install -r "${REPO_ROOT}/requirements.txt"
  else
    die "requirements.txt not found in repo root."
  fi
}

create_runtime_state_dirs() {
  log "Creating rag-gateway runtime state directories"
  mkdir -p "${RUNTIME_STATE_DIR}" "${RUNTIME_LOG_DIR}"
}

deploy_config_files() {
  log "Deploying config files to ${CONF_DIR}"
  mkdir -p "${CONF_DIR}"
  rsync -a --delete "${REPO_ROOT}/etc/" "${CONF_DIR}/"
}

patch_config_paths() {
  local cfg="${CONF_DIR}/config.yaml"
  if [[ ! -f "${cfg}" ]]; then
    die "Missing ${cfg} after config deploy."
  fi

  log "Patching ${cfg} paths for this filesystem layout"

  # Keep this conservative: replace only known defaults.
  sed -i \
    -e "s|^  base_dir: .*|  base_dir: \"${MODELS_DIR}\"|g" \
    -e "s|^  tantivy_dir: .*|  tantivy_dir: \"${TANTIVY_DIR}\"|g" \
    -e "s|^  data_dir: .*|  data_dir: \"${DATA_DIR}\"|g" \
    -e "s|^  cache_dir: .*|  cache_dir: \"${CACHE_DIR}\"|g" \
    -e "s|^  log_dir: .*|  log_dir: \"${LOG_DIR}\"|g" \
    "${cfg}" || true
}

deploy_systemd_units() {
  log "Deploying systemd units to ${SYSTEMD_DIR}"
  mkdir -p "${SYSTEMD_DIR}"

  # Backup existing if present, then overwrite
  for f in "${REPO_ROOT}/systemd/"*.service "${REPO_ROOT}/systemd/"*.target "${REPO_ROOT}/systemd/"*.timer; do
    [[ -f "$f" ]] || continue
    local base
    base="$(basename "$f")"
    if [[ -f "${SYSTEMD_DIR}/${base}" ]]; then
      local bak="${SYSTEMD_DIR}/${base}.$(date +%Y%m%d-%H%M%S).bak"
      cp -a "${SYSTEMD_DIR}/${base}" "${bak}"
      log "Backed up ${SYSTEMD_DIR}/${base} -> ${bak}"
    fi
    install -m 0644 "$f" "${SYSTEMD_DIR}/${base}"
  done

  log "Deploying systemd drop-in overrides"
  if [[ -d "${REPO_ROOT}/systemd/overrides" ]]; then
    rsync -a "${REPO_ROOT}/systemd/overrides/" "${SYSTEMD_DIR}/"
  fi
}

patch_unit_model_paths() {
  log "Patching unit files to use ${MODELS_DIR} for model locations"
  # Replace common defaults with current MODELS_DIR in unit files.
  # Safe even if no matches.
  sed -i \
    -e "s|/opt/llm/models|${MODELS_DIR}|g" \
    "${SYSTEMD_DIR}/vllm.service" \
    "${SYSTEMD_DIR}/tei-embed.service" \
    "${SYSTEMD_DIR}/tei-rerank.service" \
    2>/dev/null || true
}

set_ownership_permissions() {
  log "Setting ownership/permissions"

  # App code readable by service users; runtime dirs owned by rag
  chown -R root:root "${APP_DIR}"
  chown -R rag:rag "${VAR_DIR}" "${RUNTIME_STATE_DIR}" "${RUNTIME_LOG_DIR}"

  # Caches owned by relevant service users
  chown -R tei:tei "${TEI_CACHE_DIR}" || true
  chown -R vllm:vllm "${HF_CACHE_DIR}" || true

  # Models: readable by services (root-owned is fine, but allow group/world read)
  chmod -R a+rX "${MODELS_DIR}" || true

  # Ensure scripts are executable
  chmod -R a+rx "${APP_BIN_DIR}"
}

systemd_reload() {
  systemctl daemon-reload
}

enable_stack_target() {
  log "Enabling rag-stack.target"
  systemctl enable rag-stack.target >/dev/null
}

start_stack_nonblocking() {
  log "Starting rag-stack.target (non-blocking)"
  systemctl start --no-block rag-stack.target
  systemctl list-jobs || true
}

is_unit_active() {
  local unit="$1"
  systemctl is-active --quiet "${unit}"
}

wait_unit_active_verbose() {
  local unit="$1"
  local timeout="$2"

  local elapsed=0
  local step=5

  while (( elapsed < timeout )); do
    local active sub result nrestarts main_status main_code
    active="$(systemctl show -p ActiveState --value "${unit}" 2>/dev/null || echo "unknown")"
    sub="$(systemctl show -p SubState --value "${unit}" 2>/dev/null || echo "unknown")"
    result="$(systemctl show -p Result --value "${unit}" 2>/dev/null || echo "unknown")"
    nrestarts="$(systemctl show -p NRestarts --value "${unit}" 2>/dev/null || echo "0")"
    main_status="$(systemctl show -p ExecMainStatus --value "${unit}" 2>/dev/null || echo "0")"
    main_code="$(systemctl show -p ExecMainCode --value "${unit}" 2>/dev/null || echo "0")"

    if [[ "${active}" == "active" ]]; then
      log "OK: ${unit} is active"
      return 0
    fi

    # Fail fast on obvious restart loops instead of waiting full timeout.
    # Common case: status=217/USER or similar misconfiguration -> endless auto-restart.
    if [[ "${sub}" == "auto-restart" ]] && [[ "${nrestarts}" =~ ^[0-9]+$ ]] && (( nrestarts >= 3 )); then
      log "WARNING: ${unit} is in auto-restart (restarts=${nrestarts}, result=${result}, exec_code=${main_code}, exec_status=${main_status})"
      systemctl status "${unit}" --no-pager || true
      journalctl -u "${unit}" -b --no-pager -n 80 || true
      return 1
    fi

    if [[ "${active}" == "failed" ]]; then
      log "WARNING: ${unit} is failed (result=${result}, exec_code=${main_code}, exec_status=${main_status})"
      systemctl status "${unit}" --no-pager || true
      journalctl -u "${unit}" -b --no-pager -n 80 || true
      return 1
    fi

    log "Waiting for ${unit} (${active} ${sub})... ${elapsed}/${timeout}s"
    sleep "${step}"
    elapsed=$(( elapsed + step ))
  done

  log "WARNING: NOT ACTIVE after ${timeout}s: ${unit}"
  systemctl status "${unit}" --no-pager || true
  journalctl -u "${unit}" -b --no-pager -n 80 || true
  return 1
}

verify_stack_active() {
  log "Verifying stack units are active"

  # Core services should already be active quickly
  local core_units=(qdrant.service tei-embed.service tei-rerank.service vllm.service)
  for u in "${core_units[@]}"; do
    if is_unit_active "${u}"; then
      log "OK: ${u} is active"
    else
      log "WARNING: ${u} is not active yet; waiting briefly"
      wait_unit_active_verbose "${u}" 60 || return 1
    fi
  done

  # Gateway depends on readiness probes; allow longer but fail fast on restart loops.
  wait_unit_active_verbose rag-gateway.service "${VERIFY_GATEWAY_TIMEOUT}" || return 1

  # Timer should become active(waiting) once gateway is up.
  wait_unit_active_verbose rag-crawl.timer "${VERIFY_TIMER_TIMEOUT}" || return 1

  return 0
}

show_failed_units() {
  log "WARNING: Showing failed units (if any):"
  systemctl --failed --no-pager || true
}

main() {
  need_root
  detect_repo_root

  create_base_directories
  ensure_models_present

  install_vllm

  deploy_application_code
  deploy_runtime_scripts

  ensure_gateway_venv
  install_gateway_dependencies

  create_runtime_state_dirs
  deploy_config_files
  patch_config_paths

  deploy_systemd_units
  patch_unit_model_paths
  set_ownership_permissions

  systemd_reload
  enable_stack_target
  start_stack_nonblocking

  if verify_stack_active; then
    log "SUCCESS: Stack is up."
    exit 0
  fi

  show_failed_units
  die "Stack verification failed."
}

main "$@"

