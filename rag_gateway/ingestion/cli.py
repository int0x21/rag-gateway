#!/usr/bin/env python3
"""
RAG Gateway Ingestion CLI

Crawls and ingests documentation for RAG knowledge base.

Usage:
  # Crawl all sources from config
  rag-gateway-crawl --api-config /etc/rag-gateway/api.yaml \
                    --ingest-config /etc/rag-gateway/ingest.yaml \
                    --sources /etc/rag-gateway/sources.yaml

  # Crawl specific sources by name
  rag-gateway-crawl --api-config /etc/rag-gateway/api.yaml \
                    --ingest-config /etc/rag-gateway/ingest.yaml \
                    --sources /etc/rag-gateway/sources.yaml \
                    --source sonic-core ceph-docs

  # Force run (ignore last run timestamp)
  rag-gateway-crawl --api-config /etc/rag-gateway/api.yaml \
                    --ingest-config /etc/rag-gateway/ingest.yaml \
                    --sources /etc/rag-gateway/sources.yaml \
                    --force

  # Dry run (show what would be ingested)
  rag-gateway-crawl --api-config /etc/rag-gateway/api.yaml \
                    --ingest-config /etc/rag-gateway/ingest.yaml \
                    --sources /etc/rag-gateway/sources.yaml \
                    --dry-run
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from ..config import load_config
from ..storage.qdrant_store import QdrantVectorStore
from ..storage.tantivy_index import TantivyBM25
from ..storage.tei_client import TEIClient
from ..ingestion.service import ingest_documents, crawl_sources
from ..ingestion.pipeline import document_to_chunks


_SHUTDOWN_REQUESTED = False
_FORCE_SHUTDOWN = False


def setup_logging(log_dir: str, verbose: bool) -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ]
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Logging to: {log_file}")
    return logger


def signal_handler(signum, frame):
    global _SHUTDOWN_REQUESTED, _FORCE_SHUTDOWN
    if _SHUTDOWN_REQUESTED:
        # Second Ctrl+C - force immediate exit
        print("\n\nForced shutdown requested. Exiting immediately...", file=sys.stderr)
        _FORCE_SHUTDOWN = True
        # Use os._exit for immediate termination from signal handler
        import os
        os._exit(128 + signum)
    else:
        # First Ctrl+C - graceful shutdown
        _SHUTDOWN_REQUESTED = True
        print("\n\nShutdown requested. Finishing current source... (Ctrl+C again to force exit)", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(
        description="RAG Gateway - Crawl and ingest documentation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    p.add_argument("--api-config", default="/etc/rag-gateway/api.yaml",
                   help="Path to api.yaml config (default: /etc/rag-gateway/api.yaml)")
    p.add_argument("--ingest-config", default="/etc/rag-gateway/ingest.yaml",
                   help="Path to ingest.yaml config (default: /etc/rag-gateway/ingest.yaml)")
    p.add_argument("--sources", default="/etc/rag-gateway/sources.yaml",
                   help="Path to sources.yaml (default: /etc/rag-gateway/sources.yaml)")
    p.add_argument("--source", action="append", dest="sources_filter",
                  help="Specific source names to crawl (can be specified multiple times)")
    p.add_argument("--force", action="store_true",
                  help="Force run (ignore lock file)")
    p.add_argument("--dry-run", action="store_true",
                  help="Dry run (show what would be ingested, don't actually store)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose output (DEBUG level)")
    p.add_argument("--reset", action="store_true",
                   help="Reset all ingested data (clear Qdrant and Tantivy)")
    p.add_argument("--reset-qdrant", action="store_true",
                   help="Reset only Qdrant vector data")
    p.add_argument("--reset-tantivy", action="store_true",
                   help="Reset only Tantivy search index")

    args = p.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Validate config files exist
    import os
    import sys
    for config_path, config_name in [
        (args.api_config, "API config"),
        (args.ingest_config, "Ingest config"),
        (args.sources, "Sources config")
    ]:
        if not os.path.isfile(config_path):
            print(f"ERROR: {config_name} file not found: {config_path}", file=sys.stderr)
            print("ERROR: Please ensure RAG Gateway is properly installed with 'sudo ./install.sh'", file=sys.stderr)
            print("ERROR: Or specify custom paths with --api-config, --ingest-config, --sources", file=sys.stderr)
            sys.exit(1)

    api_cfg = load_config(args.api_config)
    log_dir = getattr(api_cfg.paths, 'log_dir', '/opt/llm/rag/log')

    logger = setup_logging(log_dir, args.verbose)

    # Handle reset operations
    if args.reset or args.reset_qdrant or args.reset_tantivy:
        logger.info("Starting data reset operation")
        result = asyncio.run(run_reset(
            api_cfg=api_cfg,
            reset_qdrant=args.reset or args.reset_qdrant,
            reset_tantivy=args.reset or args.reset_tantivy,
            logger=logger,
        ))
        print(json.dumps(result, indent=2))
        return

    try:
        result = asyncio.run(run_crawl(
            api_cfg=api_cfg,
            ingest_config_path=args.ingest_config,
            sources_path=args.sources,
            sources_filter=args.sources_filter,
            force=args.force,
            dry_run=args.dry_run,
            verbose=args.verbose,
            logger=logger,
        ))

        print(json.dumps(result, indent=2))

        if not result.get("ran"):
            sys.exit(1)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(json.dumps({
            "ran": False,
            "reason": f"Fatal error: {str(e)}",
            "error": str(e),
        }, indent=2), file=sys.stderr)
        sys.exit(1)


async def run_reset(api_cfg, reset_qdrant: bool, reset_tantivy: bool, logger) -> Dict:
    import os
    """Reset ingested data"""
    from ..storage.qdrant_store import QdrantVectorStore
    from ..storage.tantivy_index import TantivyBM25

    result = {"reset_qdrant": False, "reset_tantivy": False, "errors": []}

    try:
        if reset_qdrant:
            logger.info("Resetting Qdrant vector store")
            qdrant = QdrantVectorStore(
                url=api_cfg.upstreams.qdrant_url,
                collection="chunks_v1",  # Use the known collection name
                vector_size=4096,  # Should match TEI embed dimensions
            )
            # Note: Qdrant client doesn't have a simple reset method
            # We would need to delete and recreate the collection
            # For now, we'll just log that manual reset is needed
            logger.warning("Qdrant reset requires manual collection deletion")
            result["qdrant_message"] = "Manual reset required: delete and recreate collection"

        if reset_tantivy:
            logger.info("Resetting Tantivy search index")
            tantivy_path = getattr(api_cfg.paths, 'tantivy_index_dir', '/opt/llm/rag-gateway/var/tantivy')
            import shutil
            if os.path.exists(tantivy_path):
                shutil.rmtree(tantivy_path)
                os.makedirs(tantivy_path, exist_ok=True)
                result["reset_tantivy"] = True
                logger.info("Tantivy index reset complete")
            else:
                result["tantivy_message"] = "Tantivy directory not found"

    except Exception as e:
        result["errors"].append(str(e))
        logger.error(f"Reset failed: {e}")

    return result


async def run_crawl(
    api_cfg,
    ingest_config_path: str,
    sources_path: str,
    sources_filter: List[str],
    force: bool,
    dry_run: bool,
    verbose: bool,
    logger: logging.Logger,
) -> Dict[str, Any]:
    global _SHUTDOWN_REQUESTED, _FORCE_SHUTDOWN

    import yaml
    ingest_cfg = yaml.safe_load(Path(ingest_config_path).read_text(encoding="utf-8")) or {}

    sources_doc = yaml.safe_load(Path(sources_path).read_text(encoding="utf-8")) or {}
    defaults = sources_doc.get("defaults", {})
    all_sources = sources_doc.get("sources", [])

    if sources_filter:
        sources = [s for s in all_sources if s.get("name") in sources_filter]
        if not sources:
            return {"ran": False, "reason": f"No sources found matching: {sources_filter}"}
        logger.info(f"Filtered to {len(sources)} sources: {sources_filter}")
    else:
        sources = all_sources

    if not sources:
        return {"ran": False, "reason": "No sources defined"}

    logger.info(f"Starting crawl of {len(sources)} sources (dry_run={dry_run})")

    var_dir = str(Path(api_cfg.paths.tantivy_index_dir).parent)
    lock_file = os.path.join(var_dir, "crawl.lock")
    lock_fd = _acquire_lock(str(lock_file))
    logger.info(f"Acquired lock: {lock_file}")

    try:
        bm25 = TantivyBM25(api_cfg.paths.tantivy_index_dir)
        tei = TEIClient(
            embed_base_url=api_cfg.upstreams.tei_embed_url,
            rerank_base_url=api_cfg.upstreams.tei_rerank_url,
            embed_model=api_cfg.models.embed_model,
            rerank_model=api_cfg.models.rerank_model,
        )
        logger.info("Probing TEI for vector dimensions...")
        probe = await tei.embed_one("dimension_probe")
        vector_size = len(probe)
        logger.info(f"Vector dimensions: {vector_size}")

        qdrant = QdrantVectorStore(
            url=api_cfg.upstreams.qdrant_url,
            collection=ingest_cfg.get("qdrant_collection", "chunks_v1"),
            vector_size=vector_size,
        )
        qdrant.ensure_collection()
        logger.info(f"Qdrant collection ready: {qdrant.collection}")

        totals = {"sources": 0, "documents": 0, "chunks": 0, "points": 0, "skipped": 0, "updated": 0}
        errors: List[Dict[str, Any]] = []
        per_source: List[Dict[str, Any]] = []

        for idx, src in enumerate(sources, 1):
            src_name = src.get("name", f"unnamed_{idx}")
            logger.info(f"[{idx}/{len(sources)}] Processing source: {src_name}")

            if _SHUTDOWN_REQUESTED:
                logger.warning("Shutdown requested, stopping after current source")
                break

            # Check for forced shutdown
            if _FORCE_SHUTDOWN:
                logger.error("Forced shutdown - exiting immediately")
                sys.exit(128 + signal.SIGINT)

            try:
                src_tags = _parse_tags(src, defaults)
                src_dry = src.get("dry_run", defaults.get("dry_run", dry_run))
                if dry_run:
                    src_dry = True

                http_specs = _parse_http_specs(src)
                gh_specs = _parse_github_specs(src)

                logger.info(f"  Crawling {len(http_specs)} HTTP specs, {len(gh_specs)} GitHub specs...")

                # Check for forced shutdown before starting async operations
                if _FORCE_SHUTDOWN:
                    logger.error("Forced shutdown - cancelling async operations")
                    break

                docs = await crawl_sources(
                    http_specs=http_specs,
                    github_specs=gh_specs,
                    http_user_agent=ingest_cfg.get("http", {}).get("user_agent", "rag-gateway/0.1"),
                    http_max_pages=ingest_cfg.get("http", {}).get("max_pages", 2000),
                    http_max_depth=ingest_cfg.get("http", {}).get("max_depth", 4),
                    http_timeout_s=ingest_cfg.get("http", {}).get("request_timeout_s", 30),
                    http_delay_s=ingest_cfg.get("http", {}).get("politeness_delay_s", 0.2),
                    github_max_files=ingest_cfg.get("github", {}).get("max_files", 5000),
                    github_max_file_size_bytes=ingest_cfg.get("github", {}).get("max_file_size_bytes", 2000000),
                )
                logger.info(f"  Crawled {len(docs)} documents")

                for d in docs:
                    d.vendor = d.vendor or src_tags.get("vendor")
                    d.product = d.product or src_tags.get("product")
                    d.version = d.version or src_tags.get("version")
                    d.source_type = d.source_type or (src_tags.get("source_type") or d.source_type)

                if src_dry:
                    chunk_count = 0
                    for d in docs:
                        chunk_count += len(
                            document_to_chunks(
                                doc=d,
                                default_vendor=src_tags.get("vendor"),
                                default_product=src_tags.get("product"),
                                default_version=src_tags.get("version"),
                                default_source_type=src_tags.get("source_type"),
                                max_chars=ingest_cfg.get("chunking", {}).get("max_chars", 8000),
                                overlap_chars=ingest_cfg.get("chunking", {}).get("overlap_chars", 800),
                            )
                        )
                    preview = [d.url_or_path for d in docs[:ingest_cfg.get("preview_items", 10)]]
                    per_source.append({
                        "name": src_name,
                        "dry_run": True,
                        "documents": len(docs),
                        "chunks": chunk_count,
                        "preview": preview,
                    })
                    logger.info(f"  Dry run: {len(docs)} docs, {chunk_count} chunks")
                    totals["sources"] += 1
                    totals["documents"] += len(docs)
                    totals["chunks"] += chunk_count
                    continue

                logger.info(f"  Ingesting {len(docs)} documents...")
                res = await ingest_documents(
                    documents=docs,
                    tei=tei,
                    bm25=bm25,
                    qdrant=qdrant,
                    default_vendor=src_tags.get("vendor"),
                    default_product=src_tags.get("product"),
                    default_version=src_tags.get("version"),
                    default_source_type=src_tags.get("source_type"),
                    max_chars=ingest_cfg.get("chunking", {}).get("max_chars", 8000),
                    overlap_chars=ingest_cfg.get("chunking", {}).get("overlap_chars", 800),
                    batch_size=ingest_cfg.get("batch_size", 64),
                    incremental=ingest_cfg.get("incremental", True),
                    skip_unchanged=ingest_cfg.get("skip_unchanged", True),
                    dry_run=False,
                )
                per_source.append({"name": src_name, "dry_run": False, **res})
                logger.info(f"  Ingested: {res['chunks']} chunks, {res['updated']} updated, {res['skipped']} skipped")

                totals["sources"] += 1
                for k in ["documents", "chunks", "points", "skipped", "updated"]:
                    totals[k] += int(res.get(k, 0))

            except Exception as e:
                logger.error(f"  Error processing source {src_name}: {e}", exc_info=True)
                errors.append({
                    "source": src_name,
                    "error": str(e),
                    "type": type(e).__name__,
                })
                per_source.append({
                    "name": src_name,
                    "error": str(e),
                    "type": type(e).__name__,
                })

        result = {"ran": True, **totals, "per_source": per_source}
        if errors:
            result["errors"] = errors
            logger.warning(f"Completed with {len(errors)} errors")

        logger.info(f"Crawl complete: {totals['sources']} sources, {totals['documents']} docs, {totals['points']} points")
        return result

    finally:
        _release_lock(lock_fd, str(lock_file))
        logger.info("Released lock")


def _acquire_lock(lock_file: str) -> int:
    p = Path(lock_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        return fd
    except FileExistsError:
        try:
            existing_pid = p.read_text().strip()
            if existing_pid and _is_process_running(int(existing_pid)):
                raise RuntimeError(f"Lock already held by process {existing_pid}: {lock_file}")
            else:
                os.unlink(p)
                return _acquire_lock(lock_file)
        except Exception:
            raise RuntimeError(f"Lock already held: {lock_file}")


def _release_lock(fd: int, lock_file: str) -> None:
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(lock_file)
        except FileNotFoundError:
            pass


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _parse_tags(src: Dict, defaults: Dict) -> Dict[str, Optional[str]]:
    src_tags = src.get("tags") or {}
    return {
        "vendor": src_tags.get("vendor") or defaults.get("tags", {}).get("vendor"),
        "product": src_tags.get("product") or defaults.get("tags", {}).get("product"),
        "version": src_tags.get("version") or defaults.get("tags", {}).get("version"),
        "source_type": src_tags.get("source_type") or defaults.get("tags", {}).get("source_type"),
    }


def _parse_http_specs(src: Dict) -> List:
    from ..core.models import CrawlHTTP
    http_specs = src.get("http") or []
    if isinstance(http_specs, dict):
        http_specs = [http_specs]
    return [CrawlHTTP(**h) for h in http_specs]


def _parse_github_specs(src: Dict) -> List:
    from ..core.models import CrawlGitHub
    gh_specs = src.get("github") or []
    if isinstance(gh_specs, dict):
        gh_specs = [gh_specs]
    return [CrawlGitHub(**g) for g in gh_specs]


if __name__ == "__main__":
    main()
