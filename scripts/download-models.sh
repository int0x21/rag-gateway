#!/usr/bin/env bash
set -euo pipefail

# downloads HF models into /opt/llm/models in the layout expected by your systemd units:
#   /opt/llm/models/generator/DeepSeek-R1-Distill-Qwen-32B
#   /opt/llm/models/embeddings/Qwen3-Embedding-8B
#   /opt/llm/models/rerank/bge-reranker-large
#
# Auth (optional):
#   export HF_TOKEN="hf_..."
#
# Notes:
# - Uses huggingface_hub snapshot_download (HTTP). No git-lfs required.
# - Creates a dedicated venv under /opt/llm/model-tools/.venv to avoid polluting system python.
# - Safe to re-run; snapshot_download resumes whenever possible and skips unchanged files.

MODELS_DIR="/opt/llm/models"
TOOLS_DIR="/opt/llm/model-tools"
VENV_DIR="${TOOLS_DIR}/.venv"

# Hugging Face repos (authoritative IDs)
HF_GENERATOR_REPO="deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
HF_EMBED_REPO="Qwen/Qwen3-Embedding-8B"
HF_RERANK_REPO="BAAI/bge-reranker-large"

GENERATOR_NAME="DeepSeek-R1-Distill-Qwen-32B"
EMBED_NAME="Qwen3-Embedding-8B"
RERANK_NAME="bge-reranker-large"

# Local target directories
GENERATOR_DIR="${MODELS_DIR}/generator/${GENERATOR_NAME}"
EMBED_DIR="${MODELS_DIR}/embeddings/${EMBED_NAME}"
RERANK_DIR="${MODELS_DIR}/rerank/${RERANK_NAME}"

log() { printf '%s\n' "[$(date -Is)] $*"; }
warn() { printf '%s\n' "[$(date -Is)] WARNING: $*" >&2; }
die() { printf '%s\n' "[$(date -Is)] ERROR: $*" >&2; exit 1; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "Run as root (writes to ${MODELS_DIR})."
  fi
}

ensure_prereqs() {
  log "Ensuring prerequisites"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"

  install -d -m 0755 /opt/llm
  install -d -m 0755 "${MODELS_DIR}"
  install -d -m 0755 "${TOOLS_DIR}"

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "Creating venv: ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
  fi

  log "Installing python deps in venv (huggingface_hub)"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip wheel >/dev/null
  "${VENV_DIR}/bin/python" -m pip install --upgrade "huggingface_hub>=0.23.0" >/dev/null
}

download_one() {
  local repo_id="$1"
  local dest_dir="$2"

  log "Downloading ${repo_id} -> ${dest_dir}"
  install -d -m 0755 "${dest_dir}"

  "${VENV_DIR}/bin/python" - <<PY
import os
from huggingface_hub import snapshot_download

repo_id = "${repo_id}"
local_dir = "${dest_dir}"
token = os.environ.get("HF_TOKEN") or None

# NOTE: do not pass deprecated args like resume_download/local_dir_use_symlinks.
snapshot_download(
    repo_id=repo_id,
    local_dir=local_dir,
    token=token,
    max_workers=8,
)
print(f"OK: {repo_id} -> {local_dir}")
PY

  chmod -R a+rX "${dest_dir}"
}

print_summary() {
  log "Download complete."
  log "Generator:  ${GENERATOR_DIR}"
  log "Embedding:   ${EMBED_DIR}"
  log "Reranker:    ${RERANK_DIR}"
  log "Tip: if a model is gated, export HF_TOKEN before running."
}

main() {
  need_root
  ensure_prereqs

  download_one "${HF_GENERATOR_REPO}" "${GENERATOR_DIR}"
  download_one "${HF_EMBED_REPO}" "${EMBED_DIR}"
  download_one "${HF_RERANK_REPO}" "${RERANK_DIR}"

  print_summary
}

main "$@"

