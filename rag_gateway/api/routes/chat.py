from __future__ import annotations

import time
import logging
import psutil
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..deps import ConfigDep, RetrievalDep, TEIDep, VLLMDep
from ...core.models import ChatCompletionsRequest
from ...core.text_processing import redact_text

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionsRequest,
    cfg: ConfigDep,
    retrieval: RetrievalDep,
    vllm: VLLMDep,
):
    logger = logging.getLogger(__name__)
    user_text = ""  # Initialize for error logging

    # Log raw request details for debugging
    logger.info(f"RAW_REQUEST: model={req.model}, messages_count={len(req.messages)}")
    for i, msg in enumerate(req.messages):
        content_preview = msg.get('content', '')[:200] + '...' if len(msg.get('content', '')) > 200 else msg.get('content', '')
        logger.info(f"RAW_MESSAGE_{i}: role={msg.get('role')}, content_preview='{content_preview}'")

    start_time = time.time()
    start_mem = psutil.Process().memory_info().rss / 1024 / 1024

    try:
        # Always use RAG with default settings
        evidence_top_k = cfg.retrieval.evidence_top_k

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

        logger.info(f"PRE_PROCESS: user_text_raw='{user_text[:500]}{'...' if len(user_text) > 500 else ''}'")

        user_text = user_text.strip()

        retrieval_query = user_text or "help"

        logger.info(f"QUERY_START: '{retrieval_query[:50]}...' | MEM: {int(start_mem)}MB")

        # Always perform RAG retrieval
        retrieval_start = time.time()
        result = await retrieval.retrieve(
            query=retrieval_query,
            filters=None,
            evidence_top_k=evidence_top_k,
        )
        retrieval_time = time.time() - retrieval_start
        evidence_count = len(result["evidence"])
        logger.info(f"RETRIEVAL: {evidence_count} docs in {retrieval_time:.2f}s | MEM: {int(psutil.Process().memory_info().rss / 1024 / 1024)}MB")
        evidence_block = build_evidence_block(result["evidence"])

        evidence_build_start = time.time()
        system = (
            cfg.prompting.system_preamble.strip()
            + "\n\n"
            + cfg.prompting.citation_rule.strip()
            + "\n\n"
            + evidence_block
        )
        msgs = [{"role": "system", "content": system}]
        msgs.extend(req.messages)
        evidence_build_time = time.time() - evidence_build_start
        logger.info(f"EVIDENCE_BUILD: {len(evidence_block)} chars in {evidence_build_time:.2f}s | MEM: {int(psutil.Process().memory_info().rss / 1024 / 1024)}MB")

        upstream_payload: Dict[str, Any] = {
            "model": req.model,
            "messages": msgs,
            "stream": bool(req.stream),
        }

        # Add supported OpenAI parameters
        if req.temperature is not None:
            upstream_payload["temperature"] = req.temperature
        if req.top_p is not None:
            upstream_payload["top_p"] = req.top_p
        if req.max_tokens is not None:
            upstream_payload["max_tokens"] = req.max_tokens
        if req.stop is not None:
            upstream_payload["stop"] = req.stop
        if req.presence_penalty is not None:
            upstream_payload["presence_penalty"] = req.presence_penalty
        if req.frequency_penalty is not None:
            upstream_payload["frequency_penalty"] = req.frequency_penalty
        # Add other supported parameters as VLLM adds support

        if req.stream:
            logger.info(f"LLM_STREAM: initiated | MEM: {int(psutil.Process().memory_info().rss / 1024 / 1024)}MB")
            async def _gen():
                async for b in vllm.stream_chat_completions(upstream_payload):
                    yield b
            return StreamingResponse(_gen(), media_type="text/event-stream")

        llm_start = time.time()
        out = await vllm.chat_completions(upstream_payload)
        llm_time = time.time() - llm_start
        total_time = time.time() - start_time
        end_mem = psutil.Process().memory_info().rss / 1024 / 1024

        completion_tokens = out.get("usage", {}).get("completion_tokens", 0)
        logger.info(f"LLM_COMPLETE: {completion_tokens} tokens in {llm_time:.2f}s | TOTAL: {total_time:.2f}s | MEM: {int(end_mem)}MB")

        return out

    except Exception as e:
        total_time = time.time() - start_time
        end_mem = psutil.Process().memory_info().rss / 1024 / 1024
        logger.error(f"ERROR_CONTEXT: raw_messages_count={len(req.messages)}, processed_query='{user_text[:200]}...', error={str(e)} | TOTAL_TIME: {total_time:.2f}s | MEM: {int(end_mem)}MB")
        # Return proper JSON error instead of plain text
        raise HTTPException(
            status_code=500,
            detail=f"RAG processing failed: {str(e)}"
        )


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
