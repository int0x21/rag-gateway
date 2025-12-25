from __future__ import annotations

from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..deps import ConfigDep, RetrievalDep, TEIDep, VLLMDep
from ..core.models import ChatCompletionsRequest
from ..core.text_processing import redact_text

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionsRequest,
    cfg: ConfigDep,
    retrieval: RetrievalDep,
    vllm: VLLMDep,
):
    mode = req.rag.mode if req.rag and req.rag.mode else "selection"
    filters = req.rag.filters if req.rag and req.rag.filters else {}

    evidence_top_k = cfg.retrieval.evidence_top_k
    if mode in cfg.retrieval.mode_overrides and "evidence_top_k" in cfg.retrieval.mode_overrides[mode]:
        evidence_top_k = int(cfg.retrieval.mode_overrides[mode]["evidence_top_k"])

    if cfg.safety.redact:
        for msg in req.messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                msg["content"] = redact_text(msg["content"], cfg.safety.redaction_patterns)

    user_text = ""
    for m in reversed(req.messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                user_text = c
                break

    retrieval_query = user_text.strip() or "help"

    result = await retrieval.retrieve(
        query=retrieval_query,
        filters=filters if filters else None,
        evidence_top_k=evidence_top_k,
    )

    evidence_block = build_evidence_block(result["evidence"])

    system = (
        cfg.prompting.system_preamble.strip()
        + "\n\n"
        + cfg.prompting.citation_rule.strip()
        + "\n\n"
        + evidence_block
    )
    msgs = [{"role": "system", "content": system}]
    msgs.extend(req.messages)

    upstream_payload: Dict[str, Any] = {
        "model": cfg.models.generator_model if req.model is None else req.model,
        "messages": msgs,
        "stream": bool(req.stream),
    }

    if req.stream:
        async def _gen():
            async for b in vllm.stream_chat_completions(upstream_payload):
                yield b
        return StreamingResponse(_gen(), media_type="text/event-stream")

    out = await vllm.chat_completions(upstream_payload)
    return out


async def get_retrieval(cfg, req: ChatCompletionsRequest, retrieval_service: Any) -> dict:
    mode = req.rag.mode if req.rag and req.rag.mode else "selection"
    filters = req.rag.filters if req.rag and req.rag.filters else {}

    evidence_top_k = cfg.retrieval.evidence_top_k
    if mode in cfg.retrieval.mode_overrides and "evidence_top_k" in cfg.retrieval.mode_overrides[mode]:
        evidence_top_k = int(cfg.retrieval.mode_overrides[mode]["evidence_top_k"])

    if cfg.safety.redact:
        for msg in req.messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                msg["content"] = redact_text(msg["content"], cfg.safety.redaction_patterns)

    user_text = ""
    for m in reversed(req.messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                user_text = c
                break

    retrieval_query = user_text.strip() or "help"

    result = await retrieval_service.retrieve(
        query=retrieval_query,
        filters=filters if filters else None,
        evidence_top_k=evidence_top_k,
    )

    evidence_block = build_evidence_block(result["evidence"])

    system = (
        cfg.prompting.system_preamble.strip()
        + "\n\n"
        + cfg.prompting.citation_rule.strip()
        + "\n\n"
        + evidence_block
    )
    msgs = [{"role": "system", "content": system}]
    msgs.extend(req.messages)

    return {"messages": msgs, "mode": mode}


def build_evidence_block(evidence) -> str:
    parts = ["EVIDENCE:"]
    for ch in evidence:
        parts.append(f"[{ch.chunk_id}] {ch.title} | {ch.source} | {ch.url_or_path}")
        parts.append(ch.text.strip())
        parts.append("")
    return "\n".join(parts).strip()
