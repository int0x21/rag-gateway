from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ...core.models import IngestDocument
from ...core.text_processing import html_to_text, normalize_whitespace


def should_skip_url(url: str, allowed_domains: Set[str], allowed_prefixes: Optional[List[str]], exclude_patterns: Optional[List[str]]) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return True
    if u.scheme not in ("http", "https"):
        return True
    if u.netloc and u.netloc not in allowed_domains:
        return True
    if allowed_prefixes and not any(url.startswith(p) for p in allowed_prefixes):
        return True
    if exclude_patterns:
        for pat in exclude_patterns:
            if re.search(pat, url):
                return True
    return False


async def crawl_http_docs(
    spec: IngestDocument,
    user_agent: str,
    max_pages: int,
    max_depth: int,
    timeout_s: int,
    delay_s: float,
) -> List[IngestDocument]:
    allowed_domains = set(spec.allowed_domains)
    allowed_prefixes = spec.allowed_url_prefixes
    exclude_patterns = spec.exclude_url_patterns

    max_pages = spec.max_pages or max_pages
    max_depth = spec.max_depth or max_depth

    seen: Set[str] = set()
    out: List[IngestDocument] = []
    queue: List[Tuple[str, int]] = [(u, 0) for u in spec.start_urls]

    async with httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": user_agent}, follow_redirects=True) as client:
        while queue and len(out) < max_pages:
            url, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            if should_skip_url(url, allowed_domains, allowed_prefixes, exclude_patterns):
                continue

            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                ctype = r.headers.get("content-type", "")
                if "text/html" not in ctype:
                    continue

                text = html_to_text(r.text)
                if len(text) < 200:
                    continue

                title = url
                m = re.search(r"<title>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
                if m:
                    title = normalize_whitespace(m.group(1))[:200] or url

                out.append(
                    IngestDocument(
                        title=title,
                        source_type="official_docs",
                        url_or_path=url,
                        text=text,
                    )
                )

                if depth < max_depth:
                    soup = BeautifulSoup(r.text, "lxml")
                    for a in soup.find_all("a", href=True):
                        nxt = urljoin(url, a["href"]).split("#", 1)[0]
                        if nxt and nxt not in seen:
                            queue.append((nxt, depth + 1))

            except Exception as e:
                logging.error(f"Failed to crawl URL {url}: {e}", exc_info=True)
                pass

            if delay_s > 0:
                await asyncio.sleep(delay_s)

    return out
