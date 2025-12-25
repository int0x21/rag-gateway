from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import load_config, AppConfig
from .models import CrawlHTTP, CrawlGitHub
from .tantivy_index import TantivyBM25
from .tei_client import TEIClient
from .qdrant_store import QdrantVectorStore
from .ingest_service import ingest_documents, crawl_sources
from .ingest_pipeline import document_to_chunks


def _now() -> float:
    return time.time()


def _acquire_lock(lock_file: str) -> int:
    p = Path(lock_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        return fd
    except FileExistsError:
        raise RuntimeError(f"Lock already held: {lock_file}")


def _release_lock(fd: int, lock_file: str) -> None:
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(lock_file)
        except FileNotFoundError:
            pass


def _state_path(state_dir: str) -> Path:
    return Path(state_dir) / "last_run.json"


def _load_last_run(state_dir: str) -> Optional[float]:
    p = _state_path(state_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return float(data.get("last_run_ts"))
    except Exception:
        return None


def _save_last_run(state_dir: str, ts: float) -> None:
    p = _state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_run_ts": ts}, indent=2), encoding="utf-8")


def _should_run(interval_minutes: int, state_dir: str) -> bool:
    if interval_minutes <= 0:
        return False
    last = _load_last_run(state_dir)
    if last is None:
        return True
    return (_now() - last) >= (interval_minutes * 60)


def _read_sources(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Sources file not found: {path}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _effective_bool(val: Any, default: bool) -> bool:
    return default if val is None else bool(val)


def _tags(src: Dict[str, Any]) -> Dict[str, Optional[str]]:
    t = src.get("tags") or {}
    return {
        "vendor": t.get("vendor"),
        "product": t.get("product"),
        "version": t.get("version"),
        "source_type": t.get("source_type"),
    }


async def run_scheduled_crawl(config_path: str, force: bool = False, dry_run_override: Optional[bool] = None) -> Dict[str, Any]:
    cfg: AppConfig = load_config(config_path)

    if cfg.scheduler.interval_minutes == 0 and not force:
        return {"ran": False, "reason": "disabled (interval_minutes=0)"}
    if not force and not _should_run(cfg.scheduler.interval_minutes, cfg.scheduler.state_dir):
        return {"ran": False, "reason": "not due yet"}

    lock_fd = _acquire_lock(cfg.scheduler.lock_file)
    try:
        src_doc = _read_sources(cfg.scheduler.sources_file)
        defaults = src_doc.get("defaults") or {}
        default_enabled = bool(defaults.get("enabled", True))
        default_dry_run = bool(defaults.get("dry_run", False))
        preview_items = int(defaults.get("preview_items", 10))

        sources = src_doc.get("sources") or []
        if not sources:
            return {"ran": False, "reason": f"no sources in {cfg.scheduler.sources_file}"}

        bm25 = TantivyBM25(cfg.paths.tantivy_index_dir)

        tei = TEIClient(
            embed_base_url=cfg.upstreams.tei_embed_url,
            rerank_base_url=cfg.upstreams.tei_rerank_url,
            embed_model=cfg.models.embed_model,
            rerank_model=cfg.models.rerank_model,
        )
        probe = await tei.embed_one("dimension_probe")
        vector_size = len(probe)

        qdrant = QdrantVectorStore(
            url=cfg.upstreams.qdrant_url,
            collection=cfg.ingest.qdrant_collection,
            vector_size=vector_size,
        )
        qdrant.ensure_collection()

        totals = {"sources": 0, "documents": 0, "chunks": 0, "points": 0, "skipped": 0, "updated": 0}
        per_source: List[Dict[str, Any]] = []

        for src in sources:
            enabled = _effective_bool(src.get("enabled"), default_enabled)
            if not enabled:
                continue

            src_tags = _tags(src)
            src_dry = _effective_bool(src.get("dry_run"), default_dry_run)
            if dry_run_override is not None:
                src_dry = dry_run_override

            http_specs = src.get("http") or []
            if isinstance(http_specs, dict):
                http_specs = [http_specs]
            http_parsed = [CrawlHTTP(**h) for h in http_specs]

            gh_specs = src.get("github") or []
            if isinstance(gh_specs, dict):
                gh_specs = [gh_specs]
            gh_parsed = [CrawlGitHub(**g) for g in gh_specs]

            docs = await crawl_sources(
                http_specs=http_parsed,
                github_specs=gh_parsed,
                http_user_agent=cfg.ingest.http.user_agent,
                http_max_pages=cfg.ingest.http.max_pages,
                http_max_depth=cfg.ingest.http.max_depth,
                http_timeout_s=cfg.ingest.http.request_timeout_s,
                http_delay_s=cfg.ingest.http.politeness_delay_s,
                github_max_files=cfg.ingest.github.max_files,
                github_max_file_size_bytes=cfg.ingest.github.max_file_size_bytes,
            )

            for d in docs:
                d.vendor = d.vendor or src_tags["vendor"]
                d.product = d.product or src_tags["product"]
                d.version = d.version or src_tags["version"]
                d.source_type = d.source_type or (src_tags["source_type"] or d.source_type)

            if src_dry:
                chunk_count = 0
                for d in docs:
                    chunk_count += len(
                        document_to_chunks(
                            doc=d,
                            default_vendor=src_tags["vendor"],
                            default_product=src_tags["product"],
                            default_version=src_tags["version"],
                            default_source_type=src_tags["source_type"],
                            max_chars=cfg.chunking.max_chars,
                            overlap_chars=cfg.chunking.overlap_chars,
                        )
                    )
                preview = [d.url_or_path for d in docs[:preview_items]]
                per_source.append({"name": src.get("name", "unnamed"), "dry_run": True, "documents": len(docs), "chunks": chunk_count, "preview": preview})
                totals["sources"] += 1
                totals["documents"] += len(docs)
                totals["chunks"] += chunk_count
                continue

            res = await ingest_documents(
                documents=docs,
                tei=tei,
                bm25=bm25,
                qdrant=qdrant,
                default_vendor=src_tags["vendor"],
                default_product=src_tags["product"],
                default_version=src_tags["version"],
                default_source_type=src_tags["source_type"],
                max_chars=cfg.chunking.max_chars,
                overlap_chars=cfg.chunking.overlap_chars,
                batch_size=cfg.ingest.batch_size,
                incremental=cfg.ingest.incremental,
                skip_unchanged=cfg.ingest.skip_unchanged,
                dry_run=False,
            )

            per_source.append({"name": src.get("name", "unnamed"), "dry_run": False, **res})
            totals["sources"] += 1
            for k in ["documents", "chunks", "points", "skipped", "updated"]:
                totals[k] += int(res.get(k, 0))

        _save_last_run(cfg.scheduler.state_dir, _now())
        return {"ran": True, **totals, "per_source": per_source}

    finally:
        _release_lock(lock_fd, cfg.scheduler.lock_file)

