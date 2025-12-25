from __future__ import annotations

from typing import Annotated, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..api.deps import ConfigDep, TEIDep
from ..core.models import ChatCompletionsRequest


router = APIRouter()


@router.post("/v1/embeddings")
async def embeddings(
    payload: Dict,
    cfg: ConfigDep,
    tei: TEIDep,
):
    requested_model = payload.get("model")
    if requested_model and requested_model != cfg.models.embed_model:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported embedding model '{requested_model}'. Only '{cfg.models.embed_model}' is available.",
        )

    model = cfg.models.embed_model
    inp = payload.get("input")
    if inp is None:
        raise HTTPException(status_code=400, detail="Missing 'input'")

    if isinstance(inp, str):
        vectors = await tei.embed_many([inp])
        return {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": vectors[0]}],
            "model": model,
        }

    if isinstance(inp, list):
        vectors = await tei.embed_many([str(x) for x in inp])
        return {
            "object": "list",
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
            "model": model,
        }

    raise HTTPException(status_code=400, detail="'input' must be a string or list")
