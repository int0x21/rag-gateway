from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .models import EvidenceChunk, RetrievalResult
from ..storage.tantivy_index import TantivyBM25
from ..storage.qdrant_store import QdrantVectorStore
from ..storage.tei_client import TEIClient


def rrf_fuse(ranked_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


class RetrievalService:
    def __init__(self, config, bm25: TantivyBM25, qdrant: QdrantVectorStore, tei: TEIClient):
        self.config = config
        self.bm25 = bm25
        self.qdrant = qdrant
        self.tei = tei

    async def retrieve(self, query: str, filters: Optional[Dict[str, str]] = None, evidence_top_k: Optional[int] = None) -> Dict[str, Any]:
        bm25_top_n = self.config.bm25_top_n
        vec_top_n = self.config.vec_top_n
        rrf_k = self.config.rrf_k
        rerank_top_k = self.config.rerank_top_k

        if evidence_top_k is None:
            evidence_top_k = self.config.evidence_top_k

        deps = RetrievalDeps(bm25=self.bm25, vec=self.qdrant, tei=self.tei)

        result = await retrieve_evidence(
            deps=deps,
            query=query,
            qdrant_filters=filters,
            bm25_top_n=bm25_top_n,
            vec_top_n=vec_top_n,
            rrf_k=rrf_k,
            rerank_top_k=rerank_top_k,
            evidence_top_k=int(evidence_top_k),
        )

        return {
            "evidence": result.evidence,
        }


@dataclass
class RetrievalDeps:
    bm25: TantivyBM25
    vec: QdrantVectorStore
    tei: TEIClient


def _safe_extract(payload_or_stored: Any, key: str) -> str:
    if isinstance(payload_or_stored, dict):
        val = payload_or_stored.get(key)
        if isinstance(val, str):
            return val
    elif isinstance(payload_or_stored, list) and len(payload_or_stored) > 0:
        return str(payload_or_stored[0])
    return ""


def _normalize_rerank(rerank_json: dict, n: int) -> List[Tuple[int, float]]:
    if isinstance(rerank_json, dict):
        items = rerank_json.get("results") or rerank_json.get("data") or []
    else:
        items = rerank_json or []

    out: List[Tuple[int, float]] = []
    for it in items:
        try:
            idx = int(it.get("index", 0))
            score = float(it.get("relevance_score", it.get("score", 0.0)))
            if 0 <= idx < n:
                out.append((idx, score))
        except Exception:
            continue
    return out


async def retrieve_evidence(
    deps: RetrievalDeps,
    query: str,
    qdrant_filters: Optional[Dict[str, str]],
    bm25_top_n: int,
    vec_top_n: int,
    rrf_k: int,
    rerank_top_k: int,
    evidence_top_k: int,
) -> RetrievalResult:
    bm25_hits = deps.bm25.search(query, top_n=bm25_top_n)
    bm25_rank = [h.chunk_id for h in bm25_hits if h.chunk_id]

    qvec = await deps.tei.embed_one(query)
    vec_hits = deps.vec.search(vector=qvec, top_n=vec_top_n, filters=qdrant_filters)
    vec_rank = [h.chunk_id for h in vec_hits if h.chunk_id]

    fused = rrf_fuse([bm25_rank, vec_rank], k=rrf_k)
    candidates = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:rerank_top_k]

    payload_by_id = {h.chunk_id: h.payload for h in vec_hits}
    stored_by_id = {h.chunk_id: h.stored for h in bm25_hits}

    cand_chunks: List[EvidenceChunk] = []
    for chunk_id, fused_score in candidates:
        payload = payload_by_id.get(chunk_id, {})
        stored = stored_by_id.get(chunk_id, {})

        text = payload.get("text") or ""
        title = _safe_extract(payload, "title") or _safe_extract(stored, "title")
        source = _safe_extract(payload, "source") or _safe_extract(stored, "source")
        url_or_path = _safe_extract(payload, "url_or_path") or _safe_extract(stored, "url_or_path")

        cand_chunks.append(
            EvidenceChunk(
                chunk_id=chunk_id,
                title=title,
                source=source,
                url_or_path=url_or_path,
                vendor=payload.get("vendor"),
                product=payload.get("product"),
                version=payload.get("version"),
                text=text,
                score=float(fused_score),
            )
        )

    texts = [c.text[:2000] for c in cand_chunks]
    batch_size = 10
    if len(texts) <= batch_size:
        rerank_json = await deps.tei.rerank(query=query, texts=texts)
    else:
        batches = [texts[i:i+batch_size] for i in range(0, len(texts), batch_size)]
        rerank_jsons = await asyncio.gather(*[deps.tei.rerank(query=query, texts=batch) for batch in batches])
        combined_results = []
        for batch_idx, rj in enumerate(rerank_jsons):
            start_idx = batch_idx * batch_size
            for item in rj.get("results", []):
                item["index"] += start_idx
                combined_results.append(item)
        rerank_json = {"results": combined_results}
    pairs = _normalize_rerank(rerank_json, n=len(cand_chunks))

    if not pairs:
        top = sorted(cand_chunks, key=lambda c: c.score, reverse=True)[:evidence_top_k]
        return RetrievalResult(evidence=top)

    reranked = sorted([(cand_chunks[i], s) for i, s in pairs], key=lambda x: x[1], reverse=True)
    top = [c for c, _ in reranked[:evidence_top_k]]
    return RetrievalResult(evidence=top)
