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

logger = logging.getLogger(__name__)


async def crawl_urls_parallel(
    urls: List[str],
    max_concurrent: int = 8,
    max_total: int = 20,
    timeout: float = 30.0,
    user_agent: str = "rag-gateway/0.1",
    continue_on_failure: bool = True
) -> List[Tuple[str, Optional[str]]]:
    """
    Crawl multiple URLs concurrently with controlled parallelism.

    Args:
        urls: List of URLs to crawl
        max_concurrent: Maximum concurrent requests (default: 8)
        max_total: Absolute maximum concurrent requests (default: 20)
        timeout: Request timeout in seconds
        user_agent: HTTP User-Agent header
        continue_on_failure: Continue processing despite individual failures

    Returns:
        List of (url, content) tuples, None content for failed requests
    """
    # Validate and clamp concurrency limits
    max_concurrent = min(max_concurrent, max_total)

    semaphore = asyncio.Semaphore(max_concurrent)
    results = []

    async def fetch_single_url(url: str) -> Tuple[str, Optional[str]]:
        async with semaphore:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout),
                    headers={"User-Agent": user_agent},
                    follow_redirects=True
                ) as client:
                    logger.debug(f"Fetching: {url}")
                    response = await client.get(url)
                    response.raise_for_status()

                    # Process content
                    text = html_to_text(response.text)
                    logger.debug(f"Completed: {url} ({len(text)} chars)")
                    return url, text

            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                if not continue_on_failure:
                    raise
                return url, None

    # Execute all requests concurrently
    logger.info(f"Starting parallel fetch of {len(urls)} URLs (max_concurrent={max_concurrent})")
    tasks = [fetch_single_url(url) for url in urls]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    for result in raw_results:
        if isinstance(result, Exception):
            if not continue_on_failure:
                raise result
            logger.error(f"Unexpected error in URL fetching: {result}")
        else:
            results.append(result)

    successful = sum(1 for _, content in results if content is not None)
    failed = len(results) - successful

    logger.info(f"Parallel fetch complete: {successful} successful, {failed} failed")
    return results


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
    max_concurrent: int = 8,
) -> List[IngestDocument]:
    """
    Crawl HTTP documentation with parallel fetching.

    Uses a two-phase approach:
    1. Collect URLs using BFS up to max_pages/max_depth
    2. Fetch all URLs in parallel
    """
    allowed_domains = set(spec.allowed_domains or [])
    allowed_prefixes = spec.allowed_url_prefixes
    exclude_patterns = spec.exclude_url_patterns

    max_pages = spec.max_pages or max_pages
    max_depth = spec.max_depth or max_depth

    # Phase 1: Collect URLs using BFS (sequential, but fast)
    logger.info(f"Phase 1: Collecting URLs (max_pages={max_pages}, max_depth={max_depth})")

    seen: Set[str] = set()
    urls_to_fetch: List[str] = []
    queue: List[Tuple[str, int]] = [(u, 0) for u in spec.start_urls]

    async with httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": user_agent}, follow_redirects=True) as client:
        while queue and len(urls_to_fetch) < max_pages:
            url, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            if should_skip_url(url, allowed_domains, allowed_prefixes, exclude_patterns):
                continue

            urls_to_fetch.append(url)

            # Extract links for next level (if not at max depth)
            if depth < max_depth:
                try:
                    r = await client.get(url)
                    if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                        soup = BeautifulSoup(r.text, "lxml")
                        for a in soup.find_all("a", href=True):
                            nxt = urljoin(url, a["href"]).split("#", 1)[0]
                            if nxt and nxt not in seen and len(urls_to_fetch) + len(queue) < max_pages:
                                queue.append((nxt, depth + 1))
                except Exception as e:
                    logger.debug(f"Failed to extract links from {url}: {e}")
                    continue

            if delay_s > 0:
                await asyncio.sleep(delay_s)

    logger.info(f"Collected {len(urls_to_fetch)} URLs for parallel fetching")

    # Phase 2: Fetch all URLs in parallel
    logger.info(f"Phase 2: Fetching URLs in parallel (max_concurrent={max_concurrent})")

    fetch_results = await crawl_urls_parallel(
        urls=urls_to_fetch,
        max_concurrent=max_concurrent,
        timeout=timeout_s,
        user_agent=user_agent,
        continue_on_failure=True
    )

    # Phase 3: Process results into documents
    out: List[IngestDocument] = []

    for url, content in fetch_results:
        if content and len(content) >= 200:
            # Extract title
            title = url
            try:
                # Try to find title in the original URL fetch (this is approximate)
                # In a more sophisticated implementation, we'd store the response object
                title = url  # Default fallback
            except:
                pass

            out.append(
                IngestDocument(
                    title=title,
                    source_type="official_docs",
                    url_or_path=url,
                    text=content,
                )
            )

    logger.info(f"Successfully processed {len(out)} documents from {len(fetch_results)} fetched URLs")
    return out
