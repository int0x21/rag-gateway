from __future__ import annotations

from typing import List
import httpx


class TEIClient:
    def __init__(self, embed_base_url: str, rerank_base_url: str, embed_model: str, rerank_model: str):
        self.embed_base_url = embed_base_url.rstrip("/")
        self.rerank_base_url = rerank_base_url.rstrip("/")
        self.embed_model = embed_model
        self.rerank_model = rerank_model

    async def embed_many(self, texts: List[str]) -> List[List[float]]:
        url = f"{self.embed_base_url}/v1/embeddings"
        payload = {"model": self.embed_model, "input": texts}
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()["data"]
            data_sorted = sorted(data, key=lambda x: x.get("index", 0))
            return [d["embedding"] for d in data_sorted]

    async def embed_one(self, text: str) -> List[float]:
        return (await self.embed_many([text]))[0]

    async def rerank(self, query: str, texts: List[str]) -> dict:
        url = f"{self.rerank_base_url}/rerank"
        payload = {"model": self.rerank_model, "query": query, "texts": texts, "raw_scores": False}
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

