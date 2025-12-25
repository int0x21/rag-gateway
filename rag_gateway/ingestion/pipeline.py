from __future__ import annotations

import hashlib
from typing import List, Optional

from ..core.models import ChunkRecord, IngestDocument
from ..core.chunking import chunk_text
from ..core.text_processing import normalize_whitespace


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def document_to_chunks(
    doc: IngestDocument,
    default_vendor: Optional[str],
    default_product: Optional[str],
    default_version: Optional[str],
    default_source_type: Optional[str],
    max_chars: int,
    overlap_chars: int,
) -> List[ChunkRecord]:
    doc_id = doc.doc_id or sha256_hex(f"{doc.url_or_path}|{doc.title}")[:24]

    vendor = doc.vendor or default_vendor
    product = doc.product or default_product
    version = doc.version or default_version
    source_type = doc.source_type or (default_source_type or "official_docs")

    chunks = chunk_text(doc.text, max_chars=max_chars, overlap_chars=overlap_chars)

    out: List[ChunkRecord] = []
    for i, ch in enumerate(chunks):
        norm = normalize_whitespace(ch)
        chash = sha256_hex(norm)
        cid = sha256_hex(f"{doc_id}|{i}|{chash}")[:32]
        out.append(
            ChunkRecord(
                chunk_id=cid,
                chunk_hash=chash,
                doc_id=doc_id,
                title=doc.title,
                source_type=source_type,
                url_or_path=doc.url_or_path,
                vendor=vendor,
                product=product,
                version=version,
                text=norm,
            )
        )
    return out
