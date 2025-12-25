#!/usr/bin/env bash
set -euo pipefail

# rag-gateway installer
# - Deploys code into /opt/llm/rag-gateway
# - Deploys helper scripts into /opt/llm/rag-gateway/bin
# - Deploys config into /etc/rag-gateway
# - Deploys systemd units into /etc/systemd/system
# - Creates required system users/groups
# - Starts rag-stack.target and verifies services become active

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BASE_DIR="/opt/llm"
APP_DIR="${BASE_DIR}/rag-gateway"
BIN_DIR="${APP_DIR}/bin"
VLLM_VENV_DIR="${BASE_DIR}/vllm/.venv"
MODELS_DIR="${BASE_DIR}/models"

CONFIG_DIR="/etc/rag-gateway"
SYSTEMD_DIR="/etc/systemd/system"

STACK_TARGET="rag-stack.target"

log() {
  echo "[$(date --iso-8601=seconds)] $*"
}

die() {
  echo "[$(date --iso-8601=seconds)] ERROR: $*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "This script must be run as root."
  fi
}

detect_repo_root() {
  # Basic sanity check: must contain systemd and scripts directories
  if [[ ! -d "${REPO_ROOT}/systemd" || ! -d "${REPO_ROOT}/scripts" ]]; then
    die "Repo root does not look correct: ${REPO_ROOT} (missing systemd/ or scripts/)"
  fi
  log "Repo root detected: ${REPO_ROOT}"
}

ensure_dirs() {
  log "Creating base directories"
  mkdir -p "${BASE_DIR}" "${APP_DIR}" "${BIN_DIR}" "${MODELS_DIR}" "${CONFIG_DIR}"
}

check_models_present() {
  if compgen -G "${MODELS_DIR}/*" > /dev/null; then
    log "Models present under ${MODELS_DIR}"
  else
    die "No models found under ${MODELS_DIR}. Run scripts/download-models.sh first."
  fi
}

ensure_group_user() {
  local g="$1"
  local u="$2"

  if ! getent group "${g}" >/dev/null; then
    groupadd --system "${g}"
  fi

  if ! getent passwd "${u}" >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin --gid "${g}" "${u}"
  fi
}

ensure_stack_users() {
  # These must match User=/Group= in the unit files
  ensure_group_user "qdrant" "qdrant"
  ensure_group_user "tei"    "tei"
  ensure_group_user "vllm"   "vllm"
  ensure_group_user "rag"    "rag"
}

install_vllm() {
  log "Installing vLLM into ${VLLM_VENV_DIR}"
  mkdir -p "$(dirname "${VLLM_VENV_DIR}")"

  if [[ ! -x "${VLLM_VENV_DIR}/bin/python" ]]; then
    python3 -m venv "${VLLM_VENV_DIR}"
  fi

  "${VLLM_VENV_DIR}/bin/pip" install -U pip wheel setuptools >/dev/null
  "${VLLM_VENV_DIR}/bin/pip" install -U vllm >/dev/null

  log "vLLM installed successfully."
}

deploy_app() {
  log "Deploying application code to ${APP_DIR}"

  # Compatibility: support both old layout (./app/) and new layout (repo root)
  local src_dir=""
  if [[ -d "${REPO_ROOT}/app" ]]; then
    src_dir="${REPO_ROOT}/app"
  else
    src_dir="${REPO_ROOT}"
  fi

  rsync -a --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude 'systemd' \
    --exclude 'scripts' \
    --exclude 'install.sh' \
    --exclude 'uninstall.sh' \
    "${src_dir}/" "${APP_DIR}/"
}

deploy_scripts() {
  log "Deploying runtime helper scripts to ${BIN_DIR}"
  rsync -a --delete "${REPO_ROOT}/scripts/" "${BIN_DIR}/"
  chmod +x "${BIN_DIR}/"*.sh || true
}

ensure_gateway_venv() {
  log "Ensuring Python virtualenv exists at ${APP_DIR}/.venv"
  if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    python3 -m venv "${APP_DIR}/.venv"
  fi
}

install_gateway_deps() {
  log "Installing/updating gateway Python dependencies"
  "${APP_DIR}/.venv/bin/pip" install -U pip wheel setuptools >/dev/null

  if [[ -f "${APP_DIR}/pyproject.toml" ]]; then
    "${APP_DIR}/.venv/bin/pip" install -U "${APP_DIR}" >/dev/null
  elif [[ -f "${APP_DIR}/requirements.txt" ]]; then
    "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" >/dev/null
  else
    die "No pyproject.toml or requirements.txt found in ${APP_DIR}"
  fi
}

create_runtime_dirs() {
  log "Creating rag-gateway runtime state directories"
  mkdir -p /var/lib/rag-gateway /var/log/rag-gateway
}

deploy_config() {
  log "Deploying config files to ${CONFIG_DIR}"
  if [[ -d "${REPO_ROOT}/config" ]]; then
    rsync -a --delete "${REPO_ROOT}/config/" "${CONFIG_DIR}/"
  fi

  if [[ -f "${CONFIG_DIR}/config.yaml" ]]; then
    log "Patching /etc/rag-gateway/config.yaml paths for this filesystem layout"
    sed -i \
      -e "s|^models_dir:.*|models_dir: \"${MODELS_DIR}\"|g" \
      -e "s|^app_dir:.*|app_dir: \"${APP_DIR}\"|g" \
      "${CONFIG_DIR}/config.yaml" || true
  fi
}

deploy_systemd_units() {
  log "Deploying systemd units to ${SYSTEMD_DIR}"

  for f in "${REPO_ROOT}/systemd/"*.service "${REPO_ROOT}/systemd/"*.timer "${REPO_ROOT}/systemd/"*.target; do
    [[ -e "${f}" ]] || continue
    local base
    base="$(basename "${f}")"
    if [[ -f "${SYSTEMD_DIR}/${base}" ]]; then
      local ts
      ts="$(date +%Y%m%d-%H%M%S)"
      cp -a "${SYSTEMD_DIR}/${base}" "${SYSTEMD_DIR}/${base}.${ts}.bak"
      log "Backed up ${SYSTEMD_DIR}/${base} -> ${SYSTEMD_DIR}/${base}.${ts}.bak"
    fi
    install -m 0644 "${f}" "${SYSTEMD_DIR}/${base}"
  done
}

deploy_dropins() {
  log "Deploying systemd drop-in overrides"
  if [[ -d "${REPO_ROOT}/systemd/overrides" ]]; then
    rsync -a "${REPO_ROOT}/systemd/overrides/" "${SYSTEMD_DIR}/"
  fi
}

patch_unit_model_paths() {
  log "Patching unit files to use ${MODELS_DIR} for model locations"
  # If any unit references a placeholder, patch it here. Safe no-op if nothing matches.
  for unit in vllm.service tei-embed.service tei-rerank.service; do
    [[ -f "${SYSTEMD_DIR}/${unit}" ]] || continue
    sed -i "s|/opt/llm/models|${MODELS_DIR}|g" "${SYSTEMD_DIR}/${unit}" || true
  done
}

set_permissions() {
  log "Setting ownership/permissions"

  chown -R rag:rag /var/lib/rag-gateway /var/log/rag-gateway || true
  chown -R rag:rag "${APP_DIR}" || true

  # Allow service users to read models
  chown -R root:root "${MODELS_DIR}" || true
  chmod -R a+rX "${MODELS_DIR}" || true
}

daemon_reload() {
  systemctl daemon-reload
}

enable_target() {
  log "Enabling ${STACK_TARGET}"
  systemctl enable "${STACK_TARGET}"
}

start_target_nonblocking() {
  log "Starting ${STACK_TARGET} (non-blocking)"
  systemctl start "${STACK_TARGET}" --no-block
  systemctl list-jobs || true
}

unit_state() {
  local unit="$1"
  local a s
  a="$(systemctl show -p ActiveState --value "${unit}" 2>/dev/null || echo "unknown")"
  s="$(systemctl show -p SubState --value "${unit}" 2>/dev/null || echo "unknown")"
  echo "${a} ${s}"
}

fail_fast_if_crashing() {
  local unit="$1"
  local a s
  read -r a s < <(unit_state "${unit}")

  # If systemd reports auto-restart, it is failing repeatedly.
  if [[ "${a}" == "activating" && "${s}" == "auto-restart" ]]; then
    echo
    log "ERROR: ${unit} is in auto-restart (crashing). Showing status and recent logs:"
    systemctl status "${unit}" --no-pager || true
    echo
    journalctl -u "${unit}" -b --no-pager -n 80 || true
    echo
    return 1
  fi

  return 0
}

wait_for_unit_active() {
  local unit="$1"
  local timeout="$2"
  local interval=2
  local start now elapsed

  start="$(date +%s)"
  while true; do
    if ! fail_fast_if_crashing "${unit}"; then
      return 1
    fi

    local a s
    read -r a s < <(unit_state "${unit}")

    if [[ "${a}" == "active" ]]; then
      log "OK: ${unit} is active"
      return 0
    fi

    now="$(date +%s)"
    elapsed=$((now - start))
    if (( elapsed >= timeout )); then
      echo
      log "WARNING: NOT ACTIVE after ${timeout}s: ${unit}"
      systemctl status "${unit}" --no-pager || true
      echo
      journalctl -u "${unit}" -b --no-pager -n 80 || true
      echo
      return 1
    fi

    log "Waiting for ${unit} (${a} ${s})... ${elapsed}/${timeout}s"
    sleep "${interval}"
  done
}

verify_stack() {
  log "Verifying stack units are active"

  # These are expected to be quick
  wait_for_unit_active qdrant.service 60
  wait_for_unit_active tei-embed.service 120
  wait_for_unit_active tei-rerank.service 120
  wait_for_unit_active vllm.service 180

  # Gateway is critical; fail fast if it is crashing
  wait_for_unit_active rag-gateway.service 180

  # Timer depends on gateway; only check after gateway is confirmed up
  wait_for_unit_active rag-crawl.timer 60
}

main() {
  require_root
  detect_repo_root

  ensure_dirs
  check_models_present

  # Create service users BEFORE starting anything (prevents 217/USER loops)
  ensure_stack_users

  install_vllm
  deploy_app
  deploy_scripts
  ensure_gateway_venv
  install_gateway_deps
  create_runtime_dirs
  deploy_config
  deploy_systemd_units
  deploy_dropins
  patch_unit_model_paths
  set_permissions
  daemon_reload

  enable_target
  start_target_nonblocking

  if ! verify_stack; then
    die "Stack verification failed."
  fi

  log "Install complete."
}

main "$@"

