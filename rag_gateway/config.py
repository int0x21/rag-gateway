from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml



@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    log_level: str


@dataclass(frozen=True)
class PathsConfig:
    tantivy_index_dir: str
    log_dir: str


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
class ChunkingConfig:
    max_chars: int
    overlap_chars: int


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    paths: PathsConfig
    upstreams: Upstreams
    models: ModelConfig
    retrieval: RetrievalConfig
    safety: SafetyConfig
    prompting: PromptingConfig
    chunking: ChunkingConfig


def load_config(path: Optional[str] = None) -> AppConfig:
    if path is None:
        path = "/etc/rag-gateway/api.yaml"
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    required_sections = ["server", "paths", "upstreams", "models", "retrieval", "safety", "prompting", "chunking"]
    for section in required_sections:
        if section not in raw:
            raise ValueError(f"Config missing required section: {section}")

    server = ServerConfig(**raw["server"])
    paths = PathsConfig(**raw["paths"])
    upstreams = Upstreams(**raw["upstreams"])
    models = ModelConfig(**raw["models"])
    retrieval = RetrievalConfig(**raw["retrieval"])
    safety = SafetyConfig(**raw["safety"])
    prompting = PromptingConfig(**raw["prompting"])
    chunking = ChunkingConfig(**raw["chunking"])

    return AppConfig(
        server=server,
        paths=paths,
        upstreams=upstreams,
        models=models,
        retrieval=retrieval,
        safety=safety,
        prompting=prompting,
        chunking=chunking,
    )
