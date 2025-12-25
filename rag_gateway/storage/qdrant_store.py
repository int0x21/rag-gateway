from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm


@dataclass
class QdrantHit:
    chunk_id: str
    score: float
    payload: Dict[str, Any]


class QdrantVectorStore:
    def __init__(self, url: str, collection: str, vector_size: int, distance: qm.Distance = qm.Distance.COSINE):
        self.client = QdrantClient(url=url)
        self.collection = collection
        self.vector_size = vector_size
        self.distance = distance

    def ensure_collection(self) -> None:
        existing = [c.name for c in self.client.get_collections().collections]
        if self.collection not in existing:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qm.VectorParams(size=self.vector_size, distance=self.distance),
            )
            for field in ["vendor", "product", "version", "source_type", "doc_id", "chunk_id"]:
                try:
                    self.client.create_payload_index(
                        collection_name=self.collection,
                        field_name=field,
                        field_schema=qm.PayloadSchemaType.KEYWORD,
                    )
                except Exception as e:
                    logging.warning(f"Failed to create payload index for {field}: {e}")
                    pass

    def get_payloads(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        res = self.client.retrieve(
            collection_name=self.collection,
            ids=ids,
            with_payload=True,
            with_vectors=False,
        )
        out: Dict[str, Dict[str, Any]] = {}
        for p in res:
            out[str(p.id)] = p.payload or {}
        return out

    def upsert_points(self, points: List[qm.PointStruct]) -> None:
        self.client.upsert(collection_name=self.collection, points=points)

    def search(
        self,
        vector: List[float],
        top_n: int,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[QdrantHit]:
        qfilter = None
        if filters:
            must = []
            for k, v in filters.items():
                if v is None:
                    continue
                must.append(qm.FieldCondition(key=k, match=qm.MatchValue(value=v)))
            if must:
                qfilter = qm.Filter(must=must)

        res = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_n,
            filter=qfilter,
            with_payload=True,
        )
        hits: List[QdrantHit] = []
        for r in res:
            payload = r.payload or {}
            chunk_id = payload.get("chunk_id", "")
            hits.append(QdrantHit(chunk_id=str(chunk_id), score=float(r.score), payload=payload))
        return hits

