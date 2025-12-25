from __future__ import annotations

import logging
import re
from typing import Iterable

from bs4 import BeautifulSoup


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


def normalize_whitespace(s: str) -> str:
    if not s or not isinstance(s, str):
        return ""
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def html_to_text(html: str) -> str:
    if not html or not isinstance(html, str):
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        for pre in soup.find_all(["pre", "code"]):
            code_text = pre.get_text()
            pre.replace_with(soup.new_string(f"\n\n```text\n{code_text}\n```\n\n"))
        text = soup.get_text(separator="\n")
        return normalize_whitespace(text)
    except Exception:
        return ""
