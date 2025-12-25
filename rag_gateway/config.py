from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    log_level: str


@dataclass(frozen=True)
class PathsConfig:
    tantivy_index_dir: str


@dataclass(frozen=True)
class Upstreams:
    qdrant_url: str
    tei_embed_url: str
    tei_rerank_url: str
    vllm_url: str


@dataclass(frozen=True)
class ModelConfig:
    generator_model: str
    embed_model: str
    rerank_model: str


@dataclass(frozen=True)
class RetrievalConfig:
    bm25_top_n: int
    vec_top_n: int
    rerank_top_k: int
    evidence_top_k: int
    rrf_k: int
    mode_overrides: Dict[str, Dict[str, Any]]


@dataclass(frozen=True)
class SafetyConfig:
    redact: bool
    redaction_patterns: list[str]


@dataclass(frozen=True)
class PromptingConfig:
    system_preamble: str
    citation_rule: str


@dataclass(frozen=True)
class IngestHTTPConfig:
    user_agent: str
    max_pages: int
    max_depth: int
    request_timeout_s: int
    politeness_delay_s: float


@dataclass(frozen=True)
class IngestGitHubConfig:
    max_files: int
    max_file_size_bytes: int


@dataclass(frozen=True)
class IngestConfig:
    qdrant_collection: str
    batch_size: int
    incremental: bool
    skip_unchanged: bool
    http: IngestHTTPConfig
    github: IngestGitHubConfig


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int
    overlap_chars: int


@dataclass(frozen=True)
class SchedulerConfig:
    interval_minutes: int  # 0 disables
    state_dir: str
    lock_file: str
    sources_file: str


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    paths: PathsConfig
    upstreams: Upstreams
    models: ModelConfig
    retrieval: RetrievalConfig
    safety: SafetyConfig
    prompting: PromptingConfig
    ingest: IngestConfig
    chunking: ChunkingConfig
    scheduler: SchedulerConfig


def load_config(path: str) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    server = ServerConfig(**raw["server"])
    paths = PathsConfig(**raw["paths"])
    upstreams = Upstreams(**raw["upstreams"])
    models = ModelConfig(**raw["models"])
    retrieval = RetrievalConfig(**raw["retrieval"])
    safety = SafetyConfig(**raw["safety"])
    prompting = PromptingConfig(**raw["prompting"])

    ingest_http = IngestHTTPConfig(**raw["ingest"]["http"])
    ingest_gh = IngestGitHubConfig(**raw["ingest"]["github"])
    ingest = IngestConfig(
        qdrant_collection=raw["ingest"]["qdrant_collection"],
        batch_size=int(raw["ingest"]["batch_size"]),
        incremental=bool(raw["ingest"]["incremental"]),
        skip_unchanged=bool(raw["ingest"]["skip_unchanged"]),
        http=ingest_http,
        github=ingest_gh,
    )

    chunking = ChunkingConfig(**raw["chunking"])
    scheduler = SchedulerConfig(**raw["scheduler"])

    return AppConfig(
        server=server,
        paths=paths,
        upstreams=upstreams,
        models=models,
        retrieval=retrieval,
        safety=safety,
        prompting=prompting,
        ingest=ingest,
        chunking=chunking,
        scheduler=scheduler,
    )
