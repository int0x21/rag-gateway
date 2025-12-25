from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .config import load_config, AppConfig
from .models import ChatCompletionsRequest, IngestRequest, CrawlHTTP, CrawlGitHub
from .redact import redact_text
from .tantivy_index import TantivyBM25
from .qdrant_store import QdrantVectorStore
from .tei_client import TEIClient
from .vllm_client import VLLMClient
from .retrieval import RetrievalDeps, retrieve_evidence
from .ingest_service import ingest_documents, crawl_sources


CONFIG_PATH_DEFAULT = "/etc/rag-gateway/config.yaml"


def build_evidence_block(evidence) -> str:
    parts = ["EVIDENCE:"]
    for ch in evidence:
        parts.append(f"[{ch.chunk_id}] {ch.title} | {ch.source} | {ch.url_or_path}")
        parts.append(ch.text.strip())
        parts.append("")
    return "\n".join(parts).strip()


def last_user_text(req: ChatCompletionsRequest) -> str:
    for m in reversed(req.messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                return c
    return ""


def infer_mode(req: ChatCompletionsRequest) -> str:
    return (req.rag.mode if req.rag and req.rag.mode else "selection")


def infer_filters(req: ChatCompletionsRequest) -> Dict[str, str]:
    if req.rag and req.rag.filters:
        return {k: str(v) for k, v in req.rag.filters.items() if v is not None}
    return {}


def make_augmented_messages(cfg: AppConfig, req: ChatCompletionsRequest, evidence_block: str) -> list[dict]:
    system = (
        cfg.prompting.system_preamble.strip()
        + "\n\n"
        + cfg.prompting.citation_rule.strip()
        + "\n\n"
        + evidence_block
    )
    msgs = [{"role": "system", "content": system}]
    msgs.extend(req.messages)
    return msgs


def create_app(cfg: AppConfig) -> FastAPI:
    logging.basicConfig(level=getattr(logging, cfg.server.log_level.upper(), logging.INFO))
    app = FastAPI(title="RAG Gateway", version="0.1.0")

    bm25 = TantivyBM25(cfg.paths.tantivy_index_dir)
    tei = TEIClient(cfg.upstreams.tei_embed_url, cfg.upstreams.tei_rerank_url, cfg.models.embed_model, cfg.models.rerank_model)
    vllm = VLLMClient(cfg.upstreams.vllm_url)

    state: Dict[str, Any] = {"deps": None}

    @app.on_event("startup")
    async def _startup():
        probe = await tei.embed_one("dimension_probe")
        vector_size = len(probe)

        qdrant = QdrantVectorStore(
            url=cfg.upstreams.qdrant_url,
            collection=cfg.ingest.qdrant_collection,
            vector_size=vector_size,
        )
        qdrant.ensure_collection()

        state["deps"] = RetrievalDeps(bm25=bm25, vec=qdrant, tei=tei)

    def _deps() -> RetrievalDeps:
        d = state.get("deps")
        if d is None:
            raise RuntimeError("Service not initialized yet.")
        return d

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/ingest")
    async def ingest(req: IngestRequest):
        if not req.documents and not req.crawl:
            raise HTTPException(status_code=400, detail="Provide either documents or crawl spec")

        deps = _deps()

        if req.documents:
            return await ingest_documents(
                documents=req.documents,
                tei=deps.tei,
                bm25=deps.bm25,
                qdrant=deps.vec,
                default_vendor=req.default_vendor,
                default_product=req.default_product,
                default_version=req.default_version,
                default_source_type=req.default_source_type,
                max_chars=cfg.chunking.max_chars,
                overlap_chars=cfg.chunking.overlap_chars,
                batch_size=cfg.ingest.batch_size,
                incremental=cfg.ingest.incremental,
                skip_unchanged=cfg.ingest.skip_unchanged,
                dry_run=bool(req.dry_run),
            )

        http_specs = [CrawlHTTP(**h.model_dump()) for h in (req.crawl.http or [])] if req.crawl and req.crawl.http else []
        gh_specs = [CrawlGitHub(**g.model_dump()) for g in (req.crawl.github or [])] if req.crawl and req.crawl.github else []

        docs = await crawl_sources(
            http_specs=http_specs,
            github_specs=gh_specs,
            http_user_agent=cfg.ingest.http.user_agent,
            http_max_pages=cfg.ingest.http.max_pages,
            http_max_depth=cfg.ingest.http.max_depth,
            http_timeout_s=cfg.ingest.http.request_timeout_s,
            http_delay_s=cfg.ingest.http.politeness_delay_s,
            github_max_files=cfg.ingest.github.max_files,
            github_max_file_size_bytes=cfg.ingest.github.max_file_size_bytes,
        )

        for d in docs:
            d.vendor = d.vendor or req.default_vendor
            d.product = d.product or req.default_product
            d.version = d.version or req.default_version
            d.source_type = d.source_type or req.default_source_type or d.source_type

        return await ingest_documents(
            documents=docs,
            tei=deps.tei,
            bm25=deps.bm25,
            qdrant=deps.vec,
            default_vendor=req.default_vendor,
            default_product=req.default_product,
            default_version=req.default_version,
            default_source_type=req.default_source_type,
            max_chars=cfg.chunking.max_chars,
            overlap_chars=cfg.chunking.overlap_chars,
            batch_size=cfg.ingest.batch_size,
            incremental=cfg.ingest.incremental,
            skip_unchanged=cfg.ingest.skip_unchanged,
            dry_run=bool(req.dry_run),
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionsRequest):
        deps = _deps()

        mode = infer_mode(req)
        filters = infer_filters(req)

        evidence_top_k = cfg.retrieval.evidence_top_k
        if mode in cfg.retrieval.mode_overrides and "evidence_top_k" in cfg.retrieval.mode_overrides[mode]:
            evidence_top_k = int(cfg.retrieval.mode_overrides[mode]["evidence_top_k"])

        user_text = last_user_text(req)
        if cfg.safety.redact:
            user_text = redact_text(user_text, cfg.safety.redaction_patterns)

        retrieval_query = user_text.strip() or "help"

        rr = await retrieve_evidence(
            deps=deps,
            query=retrieval_query,
            qdrant_filters=filters if filters else None,
            bm25_top_n=cfg.retrieval.bm25_top_n,
            vec_top_n=cfg.retrieval.vec_top_n,
            rrf_k=cfg.retrieval.rrf_k,
            rerank_top_k=cfg.retrieval.rerank_top_k,
            evidence_top_k=evidence_top_k,
        )

        evidence_block = build_evidence_block(rr.evidence)
        augmented_messages = make_augmented_messages(cfg, req, evidence_block)

        upstream_payload: Dict[str, Any] = {
            "model": cfg.models.generator_model if req.model is None else req.model,
            "messages": augmented_messages,
            "stream": bool(req.stream),
        }

        if req.stream:
            async def _gen():
                async for b in vllm.stream_chat_completions(upstream_payload):
                    yield b
            return StreamingResponse(_gen(), media_type="application/json")

        out = await vllm.chat_completions(upstream_payload)
        return JSONResponse(out)

    return app


try:
    _cfg = load_config(CONFIG_PATH_DEFAULT)
    app = create_app(_cfg)
except Exception:
    app = FastAPI(title="RAG Gateway (unconfigured)")

