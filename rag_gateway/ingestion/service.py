from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from qdrant_client.http import models as qm

from ..storage.tei_client import TEIClient
from ..storage.tantivy_index import TantivyBM25
from ..storage.qdrant_store import QdrantVectorStore
from ..core.models import IngestDocument, CrawlHTTP, CrawlGitHub
from ..ingestion.pipeline import document_to_chunks
from ..ingestion.crawlers.http_crawler import crawl_http_docs
from ..ingestion.crawlers.github_crawler import crawl_github_repo


async def batched_embed_many(
    tei_client,
    texts: List[str],
    batch_size: int = 10,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    backoff: float = 2.0,
    max_concurrent: int = 4,
    timeout: int = 120,
    min_batch_size: int = 1,
    config: Optional[Dict] = None
) -> List[List[float]]:
    """
    Embed texts in batches with parallel processing, adaptive sizing, and failure recovery.

    Args:
        tei_client: TEI client instance
        texts: List of texts to embed
        batch_size: Initial batch size
        max_retries: Max retry attempts per batch
        retry_delay: Initial delay between retries
        backoff: Exponential backoff multiplier
        max_concurrent: Max parallel batches
        timeout: Timeout per batch
        min_batch_size: Minimum batch size for recovery
        config: Configuration dict for logging

    Returns:
        List of embedding vectors (preserves input order)
    """
    import asyncio
    import json
    from pathlib import Path
    from datetime import datetime

    logger = logging.getLogger(__name__)
    log_level = config.get("logging", {}).get("level", "INFO") if config else "INFO"
    embed_progress = config.get("logging", {}).get("embed_progress", True) if config else True
    embed_log_failures = config.get("tei", {}).get("embed_log_failures", True) if config else True

    # Set up failure logging
    failed_batches = []
    failure_log_file = None
    if embed_log_failures and config:
        log_dir = Path(config.get("tei", {}).get("embed_failure_log_dir", "/opt/llm/rag-gateway/var/log"))
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        failure_log_file = log_dir / f"embedding_failures_{timestamp}.jsonl"

    total_batches = (len(texts) + batch_size - 1) // batch_size
    if embed_progress:
        logger.info(f"Starting batched embedding: {len(texts)} texts, {total_batches} batches, batch_size={batch_size}")

    # Create batches with indices to preserve order
    batches = []
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i + batch_size]
        batch_idx = i // batch_size
        batches.append((batch_idx, batch_texts))

    # Semaphore for concurrency control
    semaphore = asyncio.Semaphore(max_concurrent)

    async def process_batch_adaptive(batch_idx: int, batch_texts: List[str], current_batch_size: int) -> Tuple[int, Optional[List[List[float]]]]:
        """Process a batch with adaptive sizing on failure."""
        async with semaphore:
            for attempt in range(max_retries + 1):
                try:
                    if embed_progress and attempt > 0:
                        logger.info(f"Processing batch {batch_idx + 1}/{total_batches} (attempt {attempt + 1})")

                    embed_task = asyncio.create_task(tei_client.embed_many(batch_texts))
                    vectors = await asyncio.wait_for(embed_task, timeout=timeout)

                    if embed_progress and logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"Completed batch {batch_idx + 1}/{total_batches}")
                    return batch_idx, vectors

                except asyncio.TimeoutError:
                    logger.warning(f"Batch {batch_idx + 1} timed out (attempt {attempt + 1})")
                except Exception as e:
                    error_str = str(e)
                    if "413" in error_str and current_batch_size > min_batch_size:
                        # Payload too large - break down into smaller batches
                        logger.warning(f"Batch {batch_idx + 1} too large (size {current_batch_size}), splitting...")

                        half_size = max(current_batch_size // 2, min_batch_size)
                        mid_point = len(batch_texts) // 2

                        # Recursively process smaller batches
                        left_result = await process_batch_adaptive(batch_idx, batch_texts[:mid_point], half_size)
                        right_result = await process_batch_adaptive(batch_idx, batch_texts[mid_point:], half_size)

                        # Combine results (both should be successful if we get here)
                        if left_result[1] is not None and right_result[1] is not None:
                            combined_vectors = left_result[1] + right_result[1]
                            return batch_idx, combined_vectors
                        else:
                            # One of the sub-batches failed, propagate failure
                            return batch_idx, None
                    else:
                        logger.warning(f"Batch {batch_idx + 1} failed (attempt {attempt + 1}): {e}")

                if attempt < max_retries:
                    delay = retry_delay * (backoff ** attempt)
                    logger.info(f"Retrying batch {batch_idx + 1} in {delay:.1f}s")
                    await asyncio.wait_for(asyncio.sleep(delay), timeout=delay + 1)

            # All retries exhausted - log failure
            failure_record = {
                "timestamp": datetime.now().isoformat(),
                "batch_index": batch_idx,
                "batch_size": len(batch_texts),
                "error_type": "PermanentFailure",
                "error_message": f"Failed after {max_retries + 1} attempts",
                "document_count": len(batch_texts),
                "sample_texts": batch_texts[:3] if len(batch_texts) <= 3 else batch_texts[:3] + ["..."],
                "text_lengths": [len(text) for text in batch_texts]
            }

            failed_batches.append(failure_record)

            if failure_log_file and embed_log_failures:
                try:
                    with open(failure_log_file, 'a') as f:
                        json.dump(failure_record, f)
                        f.write('\n')
                except Exception as log_error:
                    logger.error(f"Failed to write failure log: {log_error}")

            logger.error(f"Batch {batch_idx + 1} permanently failed after all attempts")
            return batch_idx, None  # Signal permanent failure

    # Process all batches in parallel
    tasks = [process_batch_adaptive(idx, texts, batch_size) for idx, texts in batches]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    ordered_results: List[Optional[List[List[float]]]] = [None] * len(batches)
    successful_batches = 0

    for result in raw_results:
        if isinstance(result, Exception):
            logger.error(f"Unexpected error in batch processing: {result}")
            continue

        batch_idx, vectors = result
        if vectors is not None:
            ordered_results[batch_idx] = vectors
            successful_batches += 1
        else:
            ordered_results[batch_idx] = None  # Failed batch

    # Flatten results while preserving order
    final_vectors = []
    failed_count = 0
    for batch_idx, vectors in enumerate(ordered_results):
        if vectors is None:
            failed_count += 1
            logger.warning(f"Skipping failed batch {batch_idx + 1}")
        else:
            final_vectors.extend(vectors)

    # Summary logging
    if embed_progress:
        if failed_batches:
            logger.warning(f"Batched embedding completed: {len(final_vectors)} vectors from {successful_batches}/{total_batches} batches")
            logger.warning(f"{len(failed_batches)} batches permanently failed - details logged to {failure_log_file}")
        else:
            logger.info(f"Batched embedding completed: {len(final_vectors)} vectors from {total_batches} batches")

    return final_vectors


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
    embed_batch_size: int = 10,
    embed_max_concurrent: int = 4,
    embed_config: Optional[Dict] = None,
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
        vectors = await batched_embed_many(
            tei_client=tei,
            texts=[c.text for c in batch],
            batch_size=embed_batch_size,
            max_concurrent=embed_max_concurrent,
            config=embed_config,
        )

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
    http_max_concurrent: int,
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
                    max_concurrent=http_max_concurrent,
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
