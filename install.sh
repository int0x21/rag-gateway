#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# rag-gateway installer
# - Ensures system users
# - Deploys app + helper scripts + config + systemd units
# - Installs vLLM into /opt/llm/vllm/.venv
# - Starts rag-stack.target
# - Verifies units are active (bounded wait)
# ==============================================================================

ts() { date -Is; }
log() { echo "[$(ts)] $*"; }
warn() { echo "[$(ts)] WARNING: $*" >&2; }
die() { echo "[$(ts)] ERROR: $*" >&2; exit 1; }

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "This installer must be run as root."
  fi
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

require_cmds() {
  local missing=()
  for c in rsync curl python3 systemctl useradd groupadd install; do
    have_cmd "$c" || missing+=("$c")
  done
  if ((${#missing[@]} > 0)); then
    die "Missing required commands: ${missing[*]}"
  fi
}

detect_repo_root() {
  # Prefer git root if available; otherwise script directory (resolved).
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

  if have_cmd git && git -C "$here" rev-parse --show-toplevel >/dev/null 2>&1; then
    git -C "$here" rev-parse --show-toplevel
  else
    echo "$here"
  fi
}

# --- Global paths (filesystem layout) -----------------------------------------
LLM_ROOT="/opt/llm"
APP_DIR="${LLM_ROOT}/rag-gateway"
APP_BIN="${APP_DIR}/bin"
APP_STATE="${APP_DIR}/state"
APP_VENV="${APP_DIR}/.venv"

VLLM_DIR="${LLM_ROOT}/vllm"
VLLM_VENV="${VLLM_DIR}/.venv"

MODELS_DIR="${LLM_ROOT}/models"
CONFIG_DIR="/etc/rag-gateway"
SYSTEMD_DIR="/etc/systemd/system"

# --- Users --------------------------------------------------------------------
ensure_user() {
  local user="$1"
  local home="${2:-/nonexistent}"
  local shell="${3:-/usr/sbin/nologin}"

  if id -u "$user" >/dev/null 2>&1; then
    return 0
  fi

  # Ensure a matching group exists (system account style)
  if ! getent group "$user" >/dev/null 2>&1; then
    groupadd -r "$user" || true
  fi

  useradd -r \
    -g "$user" \
    -d "$home" \
    -s "$shell" \
    "$user"
}

create_users() {
  log "Ensuring system users exist"
  ensure_user qdrant
  ensure_user tei
  ensure_user vllm
  ensure_user rag
}

# --- Directories --------------------------------------------------------------
create_base_dirs() {
  log "Creating base directories"
  install -d -m 0755 "$LLM_ROOT" "$VLLM_DIR" "$APP_DIR" "$APP_BIN" "$APP_STATE"
}

verify_models_present() {
  if [[ -d "$MODELS_DIR" ]] && find "$MODELS_DIR" -mindepth 1 -maxdepth 3 -type d | head -n 1 >/dev/null 2>&1; then
    log "Models present under $MODELS_DIR"
  else
    warn "No models detected under $MODELS_DIR (installer will continue, but services may fail)"
  fi
}

# --- vLLM install -------------------------------------------------------------
install_vllm() {
  log "Installing vLLM into ${VLLM_VENV}"

  # Create venv if missing
  if [[ ! -x "${VLLM_VENV}/bin/python" ]]; then
    python3 -m venv "${VLLM_VENV}"
  fi

  "${VLLM_VENV}/bin/python" -m pip install -U pip wheel setuptools >/dev/null

  # Install vllm (this can take time)
  "${VLLM_VENV}/bin/python" -m pip install -U vllm

  log "vLLM installed successfully."
}

# --- App deploy ---------------------------------------------------------------
deploy_app_code() {
  local repo_root="$1"

  log "Deploying application code to ${APP_DIR}"

  # NOTE: Repo layout does NOT contain "app/". App lives at repo root (rag_gateway/, pyproject.toml, etc).
  # Exclude installer artifacts and local envs.
  rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude 'systemd/' \
    --exclude 'systemd-dropins/' \
    --exclude 'scripts/' \
    --exclude '*.tar.gz' \
    --exclude 'install.sh' \
    --exclude 'uninstall.sh' \
    "${repo_root}/" \
    "${APP_DIR}/"
}

deploy_runtime_helpers() {
  local repo_root="$1"

  log "Deploying runtime helper scripts to ${APP_BIN}"
  install -d -m 0755 "${APP_BIN}"

  if [[ -d "${repo_root}/scripts" ]]; then
    rsync -a --delete "${repo_root}/scripts/" "${APP_BIN}/"
    chmod -R 0755 "${APP_BIN}"
  else
    warn "No scripts/ directory found in repo; skipping helper deploy"
  fi
}

ensure_app_venv() {
  log "Ensuring Python virtualenv exists at ${APP_VENV}"
  if [[ ! -x "${APP_VENV}/bin/python" ]]; then
    python3 -m venv "${APP_VENV}"
  fi
  "${APP_VENV}/bin/python" -m pip install -U pip wheel setuptools >/dev/null
}

install_gateway_deps() {
  log "Installing/updating gateway Python dependencies"
  if [[ -f "${APP_DIR}/pyproject.toml" ]]; then
    "${APP_VENV}/bin/python" -m pip install -U "${APP_DIR}"
  elif [[ -f "${APP_DIR}/requirements.txt" ]]; then
    "${APP_VENV}/bin/python" -m pip install -U -r "${APP_DIR}/requirements.txt"
  else
    die "No pyproject.toml or requirements.txt found under ${APP_DIR}"
  fi
}

create_runtime_state_dirs() {
  log "Creating rag-gateway runtime state directories"
  install -d -m 0755 "${APP_STATE}"
  install -d -m 0755 "${APP_STATE}/logs" "${APP_STATE}/data" || true
}

deploy_config() {
  local repo_root="$1"
  log "Deploying config files to ${CONFIG_DIR}"
  install -d -m 0755 "${CONFIG_DIR}"

  if [[ -d "${repo_root}/config" ]]; then
    rsync -a --delete "${repo_root}/config/" "${CONFIG_DIR}/"
  elif [[ -f "${repo_root}/config.yaml" ]]; then
    install -m 0644 "${repo_root}/config.yaml" "${CONFIG_DIR}/config.yaml"
  else
    warn "No config directory or config.yaml found in repo; leaving ${CONFIG_DIR} as-is"
  fi

  if [[ -f "${CONFIG_DIR}/config.yaml" ]]; then
    log "Patching ${CONFIG_DIR}/config.yaml paths for this filesystem layout"
    # Safe, best-effort path patching (only if keys exist)
    sed -i \
      -e "s|/opt/llm/models|${MODELS_DIR}|g" \
      -e "s|/etc/rag-gateway|${CONFIG_DIR}|g" \
      "${CONFIG_DIR}/config.yaml" || true
  fi
}

deploy_systemd_units() {
  local repo_root="$1"
  log "Deploying systemd units to ${SYSTEMD_DIR}"

  if [[ ! -d "${repo_root}/systemd" ]]; then
    die "Expected ${repo_root}/systemd directory not found"
  fi

  local f
  for f in "${repo_root}/systemd/"*.service "${repo_root}/systemd/"*.target "${repo_root}/systemd/"*.timer; do
    [[ -e "$f" ]] || continue
    local base
    base="$(basename "$f")"

    if [[ -f "${SYSTEMD_DIR}/${base}" ]]; then
      local bak="${SYSTEMD_DIR}/${base}.$(date +%Y%m%d-%H%M%S).bak"
      cp -a "${SYSTEMD_DIR}/${base}" "$bak"
      log "Backed up ${SYSTEMD_DIR}/${base} -> ${bak}"
    fi

    install -m 0644 "$f" "${SYSTEMD_DIR}/${base}"
  done

  systemctl daemon-reload
}

deploy_systemd_dropins() {
  local repo_root="$1"

  if [[ -d "${repo_root}/systemd-dropins" ]]; then
    log "Deploying systemd drop-in overrides"
    rsync -a --delete "${repo_root}/systemd-dropins/" "${SYSTEMD_DIR}/"
    systemctl daemon-reload
  else
    log "No systemd drop-in overrides found (systemd-dropins/); skipping"
  fi
}

patch_unit_files_for_models() {
  # Patch installed unit files to use /opt/llm/models instead of older baked-in paths.
  log "Patching unit files to use ${MODELS_DIR} for model locations"

  local units=(
    "${SYSTEMD_DIR}/vllm.service"
    "${SYSTEMD_DIR}/tei-embed.service"
    "${SYSTEMD_DIR}/tei-rerank.service"
  )

  local u
  for u in "${units[@]}"; do
    [[ -f "$u" ]] || continue
    sed -i \
      -e "s|/opt/llm/models|${MODELS_DIR}|g" \
      -e "s|/opt/llm/vllm/models|${MODELS_DIR}|g" \
      -e "s|/opt/llm/tei/models|${MODELS_DIR}|g" \
      "$u" || true
  done

  systemctl daemon-reload
}

fix_permissions() {
  log "Setting ownership/permissions"
  # App runtime
  chown -R rag:rag "${APP_DIR}" || true
  chmod -R u=rwX,g=rX,o=rX "${APP_DIR}" || true

  # vLLM runtime
  chown -R vllm:vllm "${VLLM_DIR}" || true
  chmod -R u=rwX,g=rX,o=rX "${VLLM_DIR}" || true

  # Config
  chown -R rag:rag "${CONFIG_DIR}" || true
  chmod -R u=rwX,g=rX,o=rX "${CONFIG_DIR}" || true

  # Models should typically remain root-owned or shared read-only; do not chown recursively here.
}

enable_start_stack() {
  log "Enabling rag-stack.target"
  systemctl enable rag-stack.target >/dev/null

  log "Starting rag-stack.target (non-blocking)"
  systemctl start rag-stack.target --no-block

  systemctl list-jobs --no-pager || true
}

wait_for_unit_active() {
  local unit="$1"
  local timeout="$2"
  local waited=0

  while (( waited < timeout )); do
    local state sub
    state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null || echo "unknown")"
    sub="$(systemctl show "$unit" -p SubState --value 2>/dev/null || echo "unknown")"

    if [[ "$state" == "active" ]]; then
      log "OK: ${unit} is active"
      return 0
    fi

    log "Waiting for ${unit} (${state} ${sub} )... ${waited}/${timeout}s"
    sleep 10
    waited=$((waited + 10))
  done

  warn "NOT ACTIVE after ${timeout}s: ${unit}"
  systemctl status "$unit" --no-pager || true
  return 1
}

verify_stack() {
  log "Verifying stack units are active"

  local ok=0
  systemctl is-active --quiet qdrant.service && log "OK: qdrant.service is active" || ok=1
  systemctl is-active --quiet tei-embed.service && log "OK: tei-embed.service is active" || ok=1
  systemctl is-active --quiet tei-rerank.service && log "OK: tei-rerank.service is active" || ok=1
  systemctl is-active --quiet vllm.service && log "OK: vllm.service is active" || ok=1

  # rag-gateway can take time; wait bounded.
  wait_for_unit_active rag-gateway.service 600 || ok=1

  # rag-crawl.timer is optional but expected by your target; keep bounded.
  wait_for_unit_active rag-crawl.timer 60 || ok=1

  if (( ok != 0 )); then
    warn "Showing failed units (if any):"
    systemctl --failed --no-pager || true
    die "Stack verification failed."
  fi

  log "Stack verification OK."
}

main() {
  require_root
  require_cmds

  local repo_root
  repo_root="$(detect_repo_root)"
  log "Repo root detected: ${repo_root}"

  create_users
  create_base_dirs
  verify_models_present

  install_vllm

  deploy_app_code "$repo_root"
  deploy_runtime_helpers "$repo_root"

  ensure_app_venv
  install_gateway_deps

  create_runtime_state_dirs
  deploy_config "$repo_root"

  deploy_systemd_units "$repo_root"
  deploy_systemd_dropins "$repo_root"
  patch_unit_files_for_models
  fix_permissions

  enable_start_stack
  verify_stack
}

main "$@"

