from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..config import load_config, AppConfig
from ..core.models import ChatCompletionsRequest
from .deps import set_config, initialize_storage, ConfigDep, TEIDep
from ..storage.vllm_client import VLLMClient


CONFIG_PATH_DEFAULT = "/etc/rag-gateway/api.yaml"


def create_app(cfg: AppConfig) -> FastAPI:
    logging.basicConfig(level=getattr(logging, cfg.server.log_level.upper(), logging.INFO))

    # Set up file logging for performance metrics
    log_dir = Path(cfg.paths.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_dir / "rag_performance.log")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    logging.getLogger().addHandler(file_handler)

    app = FastAPI(title="RAG Gateway", version="0.2.0")

    set_config(cfg)

    @app.on_event("startup")
    async def _startup():
        await initialize_storage()
        logging.info("RAG Gateway initialized")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {"id": cfg.models.generator_model, "object": "model", "owned_by": "local"},
                {"id": cfg.models.embed_model, "object": "model", "owned_by": "local"},
                {"id": cfg.models.rerank_model, "object": "model", "owned_by": "local"},
            ],
        }

    from .routes.embeddings import router as embeddings_router
    from .routes.chat import router as chat_router
    app.include_router(embeddings_router)
    app.include_router(chat_router)

    return app


logging.basicConfig(level=logging.INFO)
_cfg_path = os.environ.get("RAG_GATEWAY_CONFIG", CONFIG_PATH_DEFAULT)

try:
    _cfg = load_config(_cfg_path)
    app = create_app(_cfg)
except Exception:
    logging.exception("Failed to initialize RAG Gateway using config: %s", _cfg_path)
    raise
