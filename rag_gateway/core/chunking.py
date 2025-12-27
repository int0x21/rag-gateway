from __future__ import annotations

from typing import List

from .text_processing import normalize_whitespace


def chunk_text(text: str, max_chars: int, overlap_chars: int) -> List[str]:
    """
    Split text into chunks of at most max_chars, with paragraph-aware boundaries.
    
    Args:
        text: The text to chunk
        max_chars: Maximum characters per chunk (must be > 0)
        overlap_chars: Reserved for future overlap implementation (currently unused)
    
    Returns:
        List of text chunks
    
    Raises:
        ValueError: If max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive, got {max_chars}")
    if overlap_chars < 0:
        raise ValueError(f"overlap_chars must be non-negative, got {overlap_chars}")
    if overlap_chars >= max_chars:
        raise ValueError(f"overlap_chars ({overlap_chars}) must be less than max_chars ({max_chars})")
    
    t = normalize_whitespace(text)
    if not t:
        return []
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
