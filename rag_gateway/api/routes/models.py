from __future__ import annotations

from fastapi import APIRouter

from ..deps import ConfigDep


router = APIRouter()


@router.get("/v1/models")
async def list_models(cfg: ConfigDep):
    return {
        "object": "list",
        "data": [
            {"id": cfg.models.generator_model, "object": "model", "owned_by": "local"},
            {"id": cfg.models.embed_model, "object": "model", "owned_by": "local"},
            {"id": cfg.models.rerank_model, "object": "model", "owned_by": "local"},
        ],
    }
