from __future__ import annotations

import logging
import re
from typing import Iterable


def redact_text(text: str, patterns: Iterable[str]) -> str:
    redacted = text
    for pat in patterns:
        try:
            rx = re.compile(pat)
            def _sub(m: re.Match) -> str:
                if m.lastindex and m.lastindex >= 2:
                    return f"{m.group(1)}<REDACTED>"
                return "<REDACTED>"
            redacted = rx.sub(_sub, redacted)
        except re.error as e:
            logging.warning(f"Invalid redaction pattern '{pat}': {e}")
            continue
    return redacted

