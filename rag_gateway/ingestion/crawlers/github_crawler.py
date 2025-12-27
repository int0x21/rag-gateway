from __future__ import annotations

import os
import subprocess
import tempfile
from typing import List, Optional

from ...core.models import IngestDocument
from ...core.text_processing import normalize_whitespace


TEXT_EXTS = {".md", ".markdown", ".rst", ".txt"}
CODE_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".conf", ".cfg", ".hcl"}
DOC_EXTS = TEXT_EXTS | CODE_EXTS


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


def crawl_github_repo(spec: IngestDocument, max_files: int, max_file_size_bytes: int) -> List[IngestDocument]:
    max_files = spec.max_files or max_files

    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = os.path.join(tmp, "repo")

        cmd = ["git", "clone", "--depth", "1"]
        actual_ref = spec.ref  # Track which ref we actually use
        
        if spec.ref:
            cmd += ["--branch", spec.ref, spec.repo, repo_dir]
            subprocess.check_call(cmd)
        else:
            # Try main branch first, then master
            try:
                subprocess.check_call(cmd + ["--branch", "main", spec.repo, repo_dir])
                actual_ref = "main"
            except subprocess.CalledProcessError:
                try:
                    subprocess.check_call(cmd + ["--branch", "master", spec.repo, repo_dir])
                    actual_ref = "master"
                except subprocess.CalledProcessError:
                    # Fall back to default branch
                    subprocess.check_call(cmd + [spec.repo, repo_dir])
                    actual_ref = "HEAD"  # Default branch

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
                    
                    # Construct proper GitHub blob URL for direct access
                    # Format: https://github.com/{owner}/{repo}/blob/{ref}/{path}
                    github_url = f"{spec.repo}/blob/{actual_ref}/{rel}"
                    
                    docs.append(
                        IngestDocument(
                            title=f"{os.path.basename(spec.repo)}:{rel}",
                            source_type="code",
                            url_or_path=github_url,
                            text=text,
                        )
                    )
                    count += 1
                    if count >= max_files:
                        return docs
                except Exception:
                    continue

        return docs
