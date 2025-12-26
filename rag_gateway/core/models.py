from __future__ import annotations

from dataclasses import dataclass
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple


class RagParams(BaseModel):
    mode: Optional[str] = Field(default=None, description="selection|file|diff|doc_search")
    filters: Optional[Dict[str, Any]] = None


class ChatCompletionsRequest(BaseModel):
    model: Optional[str] = None
    messages: List[Dict[str, Any]]
    stream: Optional[bool] = False
    safety: Optional[Dict[str, Any]] = None


class EvidenceChunk(BaseModel):
    chunk_id: str
    title: str
    source: str
    url_or_path: str
    vendor: Optional[str] = None
    product: Optional[str] = None
    version: Optional[str] = None
    text: str
    score: float = 0.0


class RetrievalResult(BaseModel):
    evidence: List[EvidenceChunk]


class IngestDocument(BaseModel):
    doc_id: Optional[str] = None
    title: str
    source_type: str = "official_docs"
    url_or_path: str
    vendor: Optional[str] = None
    product: Optional[str] = None
    version: Optional[str] = None
    date_published: Optional[str] = None
    text: str


class CrawlHTTP(BaseModel):
    start_urls: List[str]
    allowed_domains: List[str]
    allowed_url_prefixes: Optional[List[str]] = None
    exclude_url_patterns: Optional[List[str]] = None
    max_pages: Optional[int] = None
    max_depth: Optional[int] = None


class CrawlGitHub(BaseModel):
    repo: str
    ref: Optional[str] = None
    include_paths: Optional[List[str]] = None
    exclude_paths: Optional[List[str]] = None
    max_files: Optional[int] = None


class CrawlSpec(BaseModel):
    http: Optional[List[CrawlHTTP]] = None
    github: Optional[List[CrawlGitHub]] = None


class IngestRequest(BaseModel):
    documents: Optional[List[IngestDocument]] = None
    crawl: Optional[CrawlSpec] = None

    default_vendor: Optional[str] = None
    default_product: Optional[str] = None
    default_version: Optional[str] = None
    default_source_type: Optional[str] = None

    dry_run: Optional[bool] = False


@dataclass
class ChunkRecord:
    chunk_id: str
    chunk_hash: str
    doc_id: str
    title: str
    source_type: str
    url_or_path: str
    vendor: Optional[str]
    product: Optional[str]
    version: Optional[str]
    text: str


@dataclass
class TantivyHit:
    chunk_id: str
    score: float
    stored: Dict[str, Any]


@dataclass
class QdrantHit:
    chunk_id: str
    score: float
    payload: Dict[str, Any]
