from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import IngestDocument, CrawlHTTP, CrawlGitHub


TEXT_EXTS = {".md", ".markdown", ".rst", ".txt"}
CODE_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".conf", ".cfg", ".hcl"}
DOC_EXTS = TEXT_EXTS | CODE_EXTS


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def normalize_whitespace(s: str) -> str:
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    for pre in soup.find_all(["pre", "code"]):
        pre.string = f"\n\n```text\n{pre.get_text()}\n```\n\n"
    text = soup.get_text(separator="\n")
    return normalize_whitespace(text)


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
    spec: CrawlHTTP,
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


def _is_included(path: str, include_paths: Optional[List[str]], exclude_paths: Optional[List[str]]) -> bool:
    p = path.replace("\\", "/")
    if exclude_paths:
        for ex in exclude_paths:
            exn = ex.rstrip("/")
            if p == exn or p.startswith(exn + "/"):
                return False
    if include_paths:
        for inc in include_paths:
            incn = inc.rstrip("/")
            if p == incn or p.startswith(incn + "/"):
                return True
        return False
    return True


def _looks_like_doc(path: str) -> bool:
    base = os.path.basename(path).lower()
    _, ext = os.path.splitext(base)
    return ext in DOC_EXTS or base in {"readme.md", "readme.rst", "readme.txt"}


def crawl_github_repo(spec: CrawlGitHub, max_files: int, max_file_size_bytes: int) -> List[IngestDocument]:
    max_files = spec.max_files or max_files

    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = os.path.join(tmp, "repo")

        cmd = ["git", "clone", "--depth", "1"]
        if spec.ref:
            cmd += ["--branch", spec.ref]
        cmd += [spec.repo, repo_dir]

        subprocess.check_call(cmd)

        docs: List[IngestDocument] = []
        count = 0

        for root, _, files in os.walk(repo_dir):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), repo_dir).replace("\\", "/")
                if not _is_included(rel, spec.include_paths, spec.exclude_paths):
                    continue
                if not _looks_like_doc(rel):
                    continue

                path = os.path.join(root, fn)
                try:
                    sz = os.path.getsize(path)
                    if sz > max_file_size_bytes:
                        continue
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        text = normalize_whitespace(f.read())
                    if len(text) < 200:
                        continue
                    docs.append(
                        IngestDocument(
                            title=f"{os.path.basename(spec.repo)}:{rel}",
                            source_type="code",
                            url_or_path=f"{spec.repo}::{rel}",
                            text=text,
                        )
                    )
                    count += 1
                    if count >= max_files:
                        return docs
                except Exception:
                    continue

        return docs


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


@dataclass
class ChunkRecord:
    chunk_id: str
    chunk_hash: str
    doc_id: str
    title: str
    source_type: str
    url_or_path: str
    vendor: Optional[str]
    product: Optional[str]
    version: Optional[str]
    text: str


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

