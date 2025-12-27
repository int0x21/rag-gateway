from __future__ import annotations

import time
import logging
import psutil
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..deps import ConfigDep, RetrievalDep, VLLMDep
from ...core.models import ChatCompletionsRequest
from ...core.text_processing import redact_text

router = APIRouter()


def log_evidence_summary(evidence: List[Any], logger: logging.Logger) -> None:
    """Log evidence chunks with curl-friendly URLs for easy inspection."""
    if not evidence:
        logger.info("EVIDENCE_SUMMARY: No evidence chunks retrieved")
        return
    
    logger.info(f"EVIDENCE_SUMMARY: {len(evidence)} chunks retrieved")
    logger.info("=" * 80)
    
    for i, ch in enumerate(evidence):
        # Build a clean, copy-pasteable curl command for the source URL
        url = ch.url_or_path or "N/A"
        if url.startswith("http"):
            curl_cmd = f"curl -sL '{url}'"
        else:
            curl_cmd = f"# Local path: {url}"
        
        vendor_info = f"{ch.vendor or 'unknown'}/{ch.product or 'unknown'}"
        
        logger.info(
            f"EVIDENCE[{i}]: score={ch.score:.3f} | {vendor_info} | {ch.title[:60]}"
        )
        logger.info(f"  chunk_id: {ch.chunk_id}")
        logger.info(f"  source: {ch.source} | {curl_cmd}")
        logger.info(f"  text_preview: {ch.text[:150].replace(chr(10), ' ')}...")
    
    logger.info("=" * 80)


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionsRequest,
    cfg: ConfigDep,
    retrieval: RetrievalDep,
    vllm: VLLMDep,
):
    logger = logging.getLogger(__name__)
    user_text = ""  # Initialize for error logging

    # Log request summary (detailed message logging moved to DEBUG level)
    logger.info(f"REQUEST: model={req.model}, messages={len(req.messages)}")
    if logger.isEnabledFor(logging.DEBUG):
        for i, msg in enumerate(req.messages):
            content = msg.get('content', '')
            preview = content[:200] + '...' if len(content) > 200 else content
            logger.debug(f"MESSAGE[{i}]: role={msg.get('role')}, content='{preview}'")

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
        
        # Log detailed evidence summary for debugging hallucinations
        log_evidence_summary(result["evidence"], logger)
        
        evidence_block = build_evidence_block(result["evidence"])

        evidence_build_start = time.time()
        
        # Structure: Evidence first (so LLM knows what it has), then rules, then reinforcement
        # This ordering helps the LLM ground its responses in the available evidence
        system = (
            evidence_block
            + "\n\n"
            + cfg.prompting.system_preamble.strip()
            + "\n\n"
            + cfg.prompting.citation_rule.strip()
            + "\n\n"
            + "REMINDER: Only use commands and configurations shown in the EVIDENCE above. If it's not in the evidence, say you don't have that information."
        )
        msgs = [{"role": "system", "content": system}]
        msgs.extend(req.messages)
        evidence_build_time = time.time() - evidence_build_start
        logger.info(f"SYSTEM_PROMPT: {len(system)} chars built in {evidence_build_time:.2f}s | MEM: {int(psutil.Process().memory_info().rss / 1024 / 1024)}MB")

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


def truncate_title(title: str, max_len: int = 40) -> str:
    """Truncate title to max_len chars, adding ellipsis if needed."""
    if len(title) <= max_len:
        return title
    return title[:max_len - 3] + "..."


def sanitize_markdown_title(title: str) -> str:
    """Escape characters that would break markdown link syntax."""
    return title.replace("[", "\\[").replace("]", "\\]")


def build_evidence_block(evidence: List[Any]) -> str:
    """Build the evidence block that gets injected into the system prompt."""
    if not evidence:
        return "EVIDENCE:\nNo relevant documentation found for this query."
    
    parts = ["EVIDENCE:"]
    for ch in evidence:
        # Include vendor/product to help LLM distinguish between SONiC distributions
        vendor = ch.vendor or "unknown"
        product = ch.product or "unknown"
        version = ch.version or ""
        version_str = f" v{version}" if version else ""
        
        # Create markdown link citation: [Title](URL)
        safe_title = sanitize_markdown_title(truncate_title(ch.title))
        url = ch.url_or_path or ""
        
        parts.append(f"[{safe_title}]({url})")
        parts.append(f"Vendor/Product: {vendor}/{product}{version_str}")
        parts.append("---")
        parts.append(ch.text.strip())
        parts.append("---")
        parts.append("")
    
    return "\n".join(parts).strip()
