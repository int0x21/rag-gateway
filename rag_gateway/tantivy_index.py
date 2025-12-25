from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import tantivy


@dataclass
class TantivyHit:
    chunk_id: str
    score: float
    stored: Dict[str, Any]


class TantivyBM25:
    def __init__(self, index_dir: str):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        if (self.index_dir / "meta.json").exists():
            self.index = tantivy.Index.open(str(self.index_dir))
            self.schema = self.index.schema
        else:
            sb = tantivy.SchemaBuilder()
            sb.add_text_field("text", stored=False)
            sb.add_text_field("chunk_id", stored=True)
            sb.add_text_field("title", stored=True)
            sb.add_text_field("source", stored=True)
            sb.add_text_field("url_or_path", stored=True)
            sb.add_text_field("vendor", stored=True)
            sb.add_text_field("product", stored=True)
            sb.add_text_field("version", stored=True)
            self.schema = sb.build()
            self.index = tantivy.Index(self.schema, path=str(self.index_dir))

        self.searcher = self.index.searcher()

    def refresh_searcher(self) -> None:
        self.searcher = self.index.searcher()

    def upsert_chunks(self, chunks: List[Dict[str, Any]]) -> None:
        writer = self.index.writer()
        for ch in chunks:
            cid = ch["chunk_id"]
            writer.delete_documents("chunk_id", cid)

            doc = tantivy.Document()
            doc.add_text("text", ch["text"])
            for f in ["chunk_id", "title", "source", "url_or_path", "vendor", "product", "version"]:
                doc.add_text(f, ch.get(f, "") or "")
            writer.add_document(doc)
        writer.commit()
        self.searcher = self.index.searcher()

    def search(self, query: str, top_n: int) -> List[TantivyHit]:
        q = self.index.parse_query(query, ["text"])
        results = self.searcher.search(q, top_n)
        hits: List[TantivyHit] = []
        for score, addr in results.hits:
            doc = self.searcher.doc(addr)
            stored = doc.to_dict()
            chunk_id = (stored.get("chunk_id") or [""])[0]
            hits.append(TantivyHit(chunk_id=chunk_id, score=float(score), stored=stored))
        return hits

