from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import EvidenceChunk, RetrievalResult
from .tantivy_index import TantivyBM25
from .qdrant_store import QdrantVectorStore
from .tei_client import TEIClient


def rrf_fuse(ranked_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


@dataclass
class RetrievalDeps:
    bm25: TantivyBM25
    vec: QdrantVectorStore
    tei: TEIClient


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
        title = payload.get("title") or ((stored.get("title") or [""])[0] if isinstance(stored.get("title"), list) else "")
        source = payload.get("source") or ((stored.get("source") or [""])[0] if isinstance(stored.get("source"), list) else "")
        url_or_path = payload.get("url_or_path") or ((stored.get("url_or_path") or [""])[0] if isinstance(stored.get("url_or_path"), list) else "")

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

    texts = [c.text for c in cand_chunks]
    rerank_json = await deps.tei.rerank(query=query, texts=texts)
    pairs = _normalize_rerank(rerank_json, n=len(cand_chunks))

    if not pairs:
        top = sorted(cand_chunks, key=lambda c: c.score, reverse=True)[:evidence_top_k]
        return RetrievalResult(evidence=top)

    reranked = sorted([(cand_chunks[i], s) for i, s in pairs], key=lambda x: x[1], reverse=True)
    top = [c for c, _ in reranked[:evidence_top_k]]
    return RetrievalResult(evidence=top)

