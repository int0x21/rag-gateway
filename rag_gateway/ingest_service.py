from __future__ import annotations

from typing import Dict, List, Optional

from qdrant_client.http import models as qm

from .tei_client import TEIClient
from .tantivy_index import TantivyBM25
from .qdrant_store import QdrantVectorStore
from .models import IngestDocument, CrawlHTTP, CrawlGitHub
from .ingest_pipeline import (
    crawl_http_docs,
    crawl_github_repo,
    document_to_chunks,
)


async def ingest_documents(
    *,
    documents: List[IngestDocument],
    tei: TEIClient,
    bm25: TantivyBM25,
    qdrant: QdrantVectorStore,
    default_vendor: Optional[str],
    default_product: Optional[str],
    default_version: Optional[str],
    default_source_type: Optional[str],
    max_chars: int,
    overlap_chars: int,
    batch_size: int = 64,
    incremental: bool = True,
    skip_unchanged: bool = True,
    dry_run: bool = False,
) -> Dict[str, int]:
    chunks = []
    for doc in documents:
        chunks.extend(
            document_to_chunks(
                doc=doc,
                default_vendor=default_vendor,
                default_product=default_product,
                default_version=default_version,
                default_source_type=default_source_type,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
        )

    if not chunks:
        return {"documents": len(documents), "chunks": 0, "points": 0, "skipped": 0, "updated": 0}

    if dry_run:
        return {"documents": len(documents), "chunks": len(chunks), "points": 0, "skipped": 0, "updated": 0}

    skipped = 0
    updated = 0
    to_process = []

    if incremental:
        for i in range(0, len(chunks), 256):
            batch_ids = [c.chunk_id for c in chunks[i : i + 256]]
            existing = qdrant.get_payloads(batch_ids)
            for c in chunks[i : i + 256]:
                if skip_unchanged:
                    payload = existing.get(c.chunk_id)
                    if payload and payload.get("chunk_hash") == c.chunk_hash:
                        skipped += 1
                        continue
                to_process.append(c)
    else:
        to_process = chunks

    if not to_process:
        return {"documents": len(documents), "chunks": len(chunks), "points": 0, "skipped": skipped, "updated": 0}

    total_points = 0
    for i in range(0, len(to_process), batch_size):
        batch = to_process[i : i + batch_size]
        vectors = await tei.embed_many([c.text for c in batch])

        points: List[qm.PointStruct] = []
        tantivy_docs = []

        for c, v in zip(batch, vectors):
            payload = {
                "chunk_id": c.chunk_id,
                "chunk_hash": c.chunk_hash,
                "doc_id": c.doc_id,
                "title": c.title,
                "source": c.source_type,
                "source_type": c.source_type,
                "url_or_path": c.url_or_path,
                "vendor": c.vendor or "",
                "product": c.product or "",
                "version": c.version or "",
                "text": c.text,
            }
            points.append(qm.PointStruct(id=c.chunk_id, vector=v, payload=payload))
            tantivy_docs.append(
                {
                    "chunk_id": c.chunk_id,
                    "title": c.title,
                    "source": c.source_type,
                    "url_or_path": c.url_or_path,
                    "vendor": c.vendor or "",
                    "product": c.product or "",
                    "version": c.version or "",
                    "text": c.text,
                }
            )

        qdrant.upsert_points(points)
        bm25.upsert_chunks(tantivy_docs)

        total_points += len(points)
        updated += len(points)

    return {
        "documents": len(documents),
        "chunks": len(chunks),
        "points": total_points,
        "skipped": skipped,
        "updated": updated,
    }


async def crawl_sources(
    *,
    http_specs: Optional[List[CrawlHTTP]],
    github_specs: Optional[List[CrawlGitHub]],
    http_user_agent: str,
    http_max_pages: int,
    http_max_depth: int,
    http_timeout_s: int,
    http_delay_s: float,
    github_max_files: int,
    github_max_file_size_bytes: int,
) -> List[IngestDocument]:
    docs: List[IngestDocument] = []

    if http_specs:
        for h in http_specs:
            docs.extend(
                await crawl_http_docs(
                    spec=h,
                    user_agent=http_user_agent,
                    max_pages=http_max_pages,
                    max_depth=http_max_depth,
                    timeout_s=http_timeout_s,
                    delay_s=http_delay_s,
                )
            )

    if github_specs:
        for g in github_specs:
            docs.extend(
                crawl_github_repo(
                    spec=g,
                    max_files=github_max_files,
                    max_file_size_bytes=github_max_file_size_bytes,
                )
            )

    return docs

