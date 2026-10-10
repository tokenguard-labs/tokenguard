"""Stage 1: repository ingestion and exclusion filtering."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

from .config import CODE_EXT, EXCLUDE_DIRS, EXCLUDE_EXT, ContextConfig

DATA_EXT = {".json", ".yaml", ".yml", ".toml", ".md", ".mdx", ".txt", ".ini", ".cfg"}


@dataclass
class FileInfo:
    path: str
    rel_path: str
    size: int
    mtime: float
    text: str
    line_count: int


def _matches_any(rel_path: str, globs) -> bool:
    return any(fnmatch.fnmatch(rel_path, g) for g in globs)


def _load_gitignore(repo_path: str) -> list:
    """Very small .gitignore parser (approx; no nested files / no ** semantics)."""
    path = os.path.join(repo_path, ".gitignore")
    patterns: list = []
    if not os.path.isfile(path):
        return patterns
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.rstrip("\n").rstrip("\r")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                neg = line.startswith("!")
                if neg:
                    line = line[1:]
                dir_only = line.endswith("/")
                line = line.rstrip("/")
                anchored = line.startswith("/")
                line = line.lstrip("/")
                if line:
                    patterns.append((line, neg, dir_only, anchored))
    except OSError:
        return []
    return patterns


def _is_ignored(rel_path: str, patterns: list) -> bool:
    if not patterns:
        return False
    parts = rel_path.split("/")
    ignored = False
    for pat, neg, dir_only, anchored in patterns:
        matched = False
        if anchored or "/" in pat:
            matched = fnmatch.fnmatch(rel_path, pat) or (
                dir_only and (rel_path == pat or rel_path.startswith(pat + "/"))
            )
        else:
            if fnmatch.fnmatch(parts[-1], pat):
                matched = True
            elif any(fnmatch.fnmatch(p, pat) for p in parts):
                matched = True
            if dir_only and any(seg == pat for seg in parts[:-1]):
                matched = True
        if matched:
            ignored = not neg
    return ignored


def _is_code(rel_path: str, cfg: ContextConfig) -> bool:
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in EXCLUDE_EXT:
        return False
    if ext in CODE_EXT:
        return True
    if cfg.include_data and ext in DATA_EXT:
        return True
    return False


def walk_repo(repo_path: str, cfg: ContextConfig) -> list:
    """Return all eligible code files under ``repo_path``.

    Deterministic: sorted by relative path.
    """
    files: list = []
    if not os.path.isdir(repo_path):
        raise NotADirectoryError(f"repo_path is not a directory: {repo_path}")

    ignore_patterns = _load_gitignore(repo_path) if cfg.respect_gitignore else []

    for root, dirs, names in os.walk(repo_path):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDE_DIRS and not d.startswith(".")
        )
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, repo_path).replace(os.sep, "/")

            if not _is_code(rel, cfg):
                continue
            if cfg.include_globs and not _matches_any(rel, cfg.include_globs):
                continue
            if cfg.exclude_globs and _matches_any(rel, cfg.exclude_globs):
                continue
            if _is_ignored(rel, ignore_patterns):
                continue

            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size > cfg.max_file_bytes:
                continue

            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue

            if "\x00" in text:
                continue

            files.append(
                FileInfo(
                    path=full,
                    rel_path=rel,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    text=text,
                    line_count=text.count("\n") + 1,
                )
            )

    files.sort(key=lambda f: f.rel_path)
    return files
