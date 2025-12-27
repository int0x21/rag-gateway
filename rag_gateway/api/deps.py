from __future__ import annotations

from fastapi import Depends
from typing import Annotated, Optional

from ..config import AppConfig
from ..core.retrieval import RetrievalService
from ..storage.qdrant_store import QdrantVectorStore
from ..storage.tantivy_index import TantivyBM25
from ..storage.tei_client import TEIClient
from ..storage.vllm_client import VLLMClient

CONFIG_PATH_DEFAULT = "/etc/rag-gateway/api.yaml"


_config: Optional[AppConfig] = None
_bm25: Optional[TantivyBM25] = None
_qdrant: Optional[QdrantVectorStore] = None
_tei: Optional[TEIClient] = None
_vllm: Optional[VLLMClient] = None
_retrieval: Optional[RetrievalService] = None


def set_config(cfg: AppConfig) -> None:
    global _config
    _config = cfg


async def initialize_storage() -> None:
    global _bm25, _qdrant, _tei, _vllm, _retrieval

    if _config is None:
        raise RuntimeError("Config not set. Call set_config() first.")

    _bm25 = TantivyBM25(_config.paths.tantivy_index_dir)
    _tei = TEIClient(
        embed_base_url=_config.upstreams.tei_embed_url,
        rerank_base_url=_config.upstreams.tei_rerank_url,
        embed_model=_config.models.embed_model,
        rerank_model=_config.models.rerank_model,
    )
    _vllm = VLLMClient(_config.upstreams.vllm_url)

    probe = await _tei.embed_one("dimension_probe")
    vector_size = len(probe)

    _qdrant = QdrantVectorStore(
        url=_config.upstreams.qdrant_url,
        collection="chunks_v1",
        vector_size=vector_size,
    )
    _qdrant.ensure_collection()

    from ..core.retrieval import RetrievalDeps
    _retrieval = RetrievalService(
        bm25=_bm25,
        qdrant=_qdrant,
        tei=_tei,
        config=_config.retrieval,
    )


async def get_config() -> AppConfig:
    if _config is None:
        raise RuntimeError("Config not loaded")
    return _config


async def get_bm25() -> TantivyBM25:
    if _bm25 is None:
        raise RuntimeError("BM25 not initialized")
    return _bm25


async def get_qdrant() -> QdrantVectorStore:
    if _qdrant is None:
        raise RuntimeError("Qdrant not initialized")
    return _qdrant


async def get_tei() -> TEIClient:
    if _tei is None:
        raise RuntimeError("TEI not initialized")
    return _tei


async def get_retrieval() -> RetrievalService:
    if _retrieval is None:
        raise RuntimeError("Retrieval service not initialized")
    return _retrieval


async def get_vllm() -> VLLMClient:
    if _vllm is None:
        raise RuntimeError("vLLM client not initialized")
    return _vllm


ConfigDep = Annotated[AppConfig, Depends(get_config)]
BM25Dep = Annotated[TantivyBM25, Depends(get_bm25)]
QdrantDep = Annotated[QdrantVectorStore, Depends(get_qdrant)]
TEIDep = Annotated[TEIClient, Depends(get_tei)]
VLLMDep = Annotated[VLLMClient, Depends(get_vllm)]
RetrievalDep = Annotated[RetrievalService, Depends(get_retrieval)]
