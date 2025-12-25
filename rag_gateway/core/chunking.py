from __future__ import annotations

from typing import List

from .text_processing import normalize_whitespace


def chunk_text(text: str, max_chars: int, overlap_chars: int) -> List[str]:
    t = normalize_whitespace(text)
    if len(t) <= max_chars:
        return [t]

    paragraphs = [p.strip() for p in t.split('\n\n') if p.strip()]
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 <= max_chars:
            current_chunk = f"{current_chunk}\n\n{para}".strip() if current_chunk else para
        else:
            if current_chunk:
                chunks.append(current_chunk)
            if len(para) > max_chars:
                sentences = [s.strip() for s in para.replace('. ', '.|').split('|') if s.strip()]
                current_chunk = ""
                for sent in sentences:
                    if len(current_chunk) + len(sent) + 1 <= max_chars:
                        current_chunk = f"{current_chunk} {sent}".strip() if current_chunk else sent
                    else:
                        if current_chunk:
                            chunks.append(current_chunk)
                        current_chunk = sent
            else:
                current_chunk = para

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [t[:max_chars]]
