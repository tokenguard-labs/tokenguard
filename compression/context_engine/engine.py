"""ContextEngine: orchestrates the context pipeline.

    (query, repo_path, budget) -> ContextResult

Two modes, selected by ``cfg.chunking``:

- ``"file"``   : P2-lite whole-file selection (safe fallback).
- ``"symbol"`` : symbol-level chunks + import-graph one-hop expansion.

Pure and deterministic; never calls an LLM.
"""

from __future__ import annotations

import time

from .chunker import chunk_repo, compress_chunk
from .config import ContextConfig
from .imports import build_import_graph, neighbors
from .models import (
    Chunk,
    ContextRequest,
    ContextResult,
    ContextStats,
    TokenStats,
)
from .redact import redact as _redact
from .selector import rank_chunks, select as select_files
from .tokens import TokenCounter, truncate_to_tokens
from .walker import walk_repo

_FILE_HEADER = "// file: "


class ContextEngine:
    def __init__(self, counter: TokenCounter | None = None) -> None:
        self.counter = counter or TokenCounter()

    # -- public -----------------------------------------------------------
    def build(self, req: ContextRequest) -> ContextResult:
        started = time.perf_counter()

        cfg = ContextConfig(
            top_k=req.top_k,
            max_tokens=req.max_tokens,
            include_globs=list(req.include_globs or []),
            exclude_globs=list(req.exclude_globs or []),
            redact_secrets=req.redact_secrets,
            chunking=req.chunking,
            expand_deps=req.expand_deps,
        )

        files = walk_repo(req.repo_path, cfg)
        by_path = {f.rel_path: f for f in files}
        original_tokens = sum(
            self.counter.count_file(f)
            + self.counter.count(f"{_FILE_HEADER}{f.rel_path}\n")
            for f in files
        )

        if cfg.chunking == "file":
            items, considered = self._select_file_mode(req.query, files, cfg)
            items = self._order(items)
        else:
            # Symbol mode already returns items in coverage-first priority order.
            items, considered = self._select_symbol_mode(req.query, files, cfg)

        context_text, included, selected_files = self._assemble(
            items, by_path, cfg.max_tokens
        )

        if cfg.strip_comments:
            context_text = self._strip_comments(context_text)

        redactions: list = []
        if cfg.redact_secrets:
            context_text, redactions = _redact(context_text, self.counter)

        context_text = self._enforce_cap(context_text, cfg.max_tokens)

        selected_tokens = self.counter.count(context_text)
        saved = max(0, original_tokens - selected_tokens)
        saved_pct = round(100.0 * saved / original_tokens, 1) if original_tokens else 0.0
        duration_ms = int((time.perf_counter() - started) * 1000)

        return ContextResult(
            context_text=context_text,
            selected_files=selected_files,
            chunks=included,
            tokens=TokenStats(
                original=original_tokens,
                selected=selected_tokens,
                saved=saved,
                saved_pct=saved_pct,
                counter=self.counter.label,
            ),
            redactions=redactions,
            stats=ContextStats(
                files_scanned=len(files),
                files_considered=considered,
                files_selected=len(selected_files),
                duration_ms=duration_ms,
            ),
        )

    # -- selection modes --------------------------------------------------
    def _select_file_mode(self, query: str, files: list, cfg: ContextConfig):
        chunks, considered = select_files(query, files, cfg)
        by_path = {f.rel_path: f for f in files}
        items = []
        for c in chunks:
            info = by_path.get(c.file)
            if info is not None:
                items.append((c, info.text))
        return items, considered

    def _select_symbol_mode(self, query: str, files: list, cfg: ContextConfig):
        """Budget-aware allocator: (1) guarantee file coverage, (2) deepen, (3) compress.

        The earlier greedy added a header + best chunk per file, so a couple of
        large functions could exhaust the budget and starve other relevant files.
        Here every candidate is priced with the token counter, one representative
        chunk is reserved per file (best that fits, else a signature), and only
        then are extra chunks added by score density within the per-file cap.
        """
        chunks = chunk_repo(files, cfg)
        chunks_by_file: dict = {}
        for c in chunks:
            chunks_by_file.setdefault(c.file, []).append(c)

        # rank_chunks -> [ ((score, reason), chunk), ... ]  (IDF-weighted)
        scored = rank_chunks(query, chunks)
        positive = [((s, r), c) for ((s, r), c) in scored if s > 0]

        if not positive:
            recent = [
                f.rel_path
                for f in sorted(files, key=lambda f: (-f.mtime, f.rel_path))[: cfg.top_k]
            ]
            items = []
            last_end: dict = {}
            for c in chunks:
                if c.file not in set(recent):
                    continue
                # Window chunks overlap by cfg.window_overlap lines; drop the repeated prefix.
                prev = last_end.get(c.file, 0)
                if c.end_line <= prev:
                    continue
                content = c.content
                public = self._to_public(0.0, "fallback: recent", c)
                if c.start_line <= prev:
                    content = "\n".join(content.split("\n")[prev - c.start_line + 1:])
                    public.start_line = prev + 1
                last_end[c.file] = c.end_line
                items.append((public, content))
            return items, 0

        by_file: dict = {}
        file_best: dict = {}
        for (s, r), c in positive:
            by_file.setdefault(c.file, []).append((s, r, c))
            if s > file_best.get(c.file, -1.0):
                file_best[c.file] = s
        for f in by_file:
            by_file[f].sort(key=lambda x: (-x[0], x[2].start_line))

        seed_files = sorted(file_best, key=lambda f: (-file_best[f], f))[: cfg.top_k]
        considered = len(file_best)

        # Dependency neighbors (one hop), ordered by their own lexical relevance.
        graph: dict = {}
        neighbor_files: list = []
        if cfg.expand_deps and cfg.dep_hops > 0:
            graph = build_import_graph(files)
            found = neighbors(set(seed_files), graph, hops=cfg.dep_hops)
            max_dep = max(cfg.top_k, 2 * len(seed_files))
            neighbor_files = sorted(
                found,
                key=lambda f: (-file_best.get(f, 0.0), f),
            )[:max_dep]

        counter = self.counter
        margin = 16  # keep the final assembly under the cap
        min_keep = 24
        remaining = max(0, cfg.max_tokens - margin)

        positive_neighbors = [f for f in neighbor_files if f in by_file]
        structural_neighbors = [f for f in neighbor_files if f not in by_file]
        coverage_order = seed_files + positive_neighbors

        items: list = []
        used: set = set()
        per_file: dict = {}
        emitted_header: set = set()
        header_cache: dict = {}
        entries: list = []

        def header_cost(f):
            if f not in header_cache:
                header_cache[f] = counter.count(f"{_FILE_HEADER}{f}\n")
            return header_cache[f]

        # Phase 1 — signature coverage: reserve a cheap representative for every
        # relevant file first, so no file is starved by another file's long body.
        for f in coverage_order:
            if remaining < min_keep:
                break
            cands = by_file.get(f)
            if cands:
                s, r, rep = cands[0]
            else:
                rep = self._header_chunk(chunks_by_file.get(f, []))
                if rep is None:
                    continue
                s, r = 0.0, f"header of {f}"
            # Coverage is always a cheap summary, so one large chunk can never
            # starve the rest. Relevant files are upgraded to full bodies next.
            sig = compress_chunk(rep)
            cost = counter.count(sig.content) + header_cost(f)
            if cost > remaining:
                continue
            remaining -= cost
            emitted_header.add(f)
            per_file[f] = 1
            used.add((rep.file, rep.start_line, rep.end_line))
            items.append((self._to_public(s, f"{r}; summary", sig), sig.content))
            entries.append(
                {
                    "f": f,
                    "rep": rep,
                    "sig": sig,
                    "score": s,
                    "reason": r,
                    "index": len(items) - 1,
                    "upgraded": False,
                }
            )

        # Phase 2 — upgrade the most relevant files to their full bodies while
        # the budget allows (delta = full body minus signature already paid).
        for e in entries:
            if e["upgraded"] or remaining < min_keep:
                continue
            full = e["rep"].content
            delta = counter.count(full) - counter.count(e["sig"].content)
            if delta <= remaining:
                remaining -= delta
                items[e["index"]] = (
                    self._to_public(e["score"], e["reason"], e["rep"]),
                    full,
                )
                e["upgraded"] = True

        # Phase 3 — deepen: additional chunks from files whose full body is in,
        # ranked by score density and bounded by the per-file cap.
        covered_full = {e["f"] for e in entries if e["upgraded"]}
        deepen = []
        for (s, r), c in positive:
            if c.file not in covered_full:
                continue
            if (c.file, c.start_line, c.end_line) in used:
                continue
            if s < file_best.get(c.file, 0.0) * cfg.chunk_rel_threshold:
                continue
            cost = counter.count(c.content)
            deepen.append((s / max(1, cost), s, r, c, cost))
        deepen.sort(key=lambda x: (-x[0], x[3].file, x[3].start_line))

        def without_overlap(c):
            """Window chunks overlap by cfg.window_overlap lines: drop lines already sent.
            Returns (start_line, end_line, content), or None if nothing new remains."""
            lines = c.content.split("\n")
            taken = [(s0, e0) for (f0, s0, e0) in used if f0 == c.file]
            if not any(c.start_line <= e0 and s0 <= c.end_line for s0, e0 in taken):
                return c.start_line, c.end_line, c.content
            if len(lines) != c.end_line - c.start_line + 1:
                return None  # can't map lines reliably; skip rather than duplicate
            keep = [i for i in range(len(lines))
                    if not any(s0 <= c.start_line + i <= e0 for s0, e0 in taken)]
            if not keep or keep[-1] - keep[0] + 1 != len(keep):
                return None  # fully covered, or the new lines aren't contiguous
            return c.start_line + keep[0], c.start_line + keep[-1], "\n".join(lines[keep[0]:keep[-1] + 1])

        for _dens, s, r, c, _cost in deepen:
            if remaining < min_keep:
                break
            if per_file.get(c.file, 0) >= cfg.max_chunks_per_file:
                continue
            trimmed = without_overlap(c)
            if trimmed is None:
                continue
            start_line, end_line, content = trimmed
            cost = counter.count(content)
            if cost > remaining:
                continue
            remaining -= cost
            per_file[c.file] = per_file.get(c.file, 0) + 1
            used.add((c.file, start_line, end_line))
            public = self._to_public(s, r, c)
            public.start_line, public.end_line = start_line, end_line
            items.append((public, content))

        # Phase 4 — structural neighbors with no lexical hit: header if it fits.
        for f in structural_neighbors:
            if remaining < min_keep:
                break
            head = self._header_chunk(chunks_by_file.get(f, []))
            if head is None:
                continue
            cost = counter.count(head.content) + header_cost(f)
            if cost > remaining:
                continue
            remaining -= cost
            emitted_header.add(f)
            items.append(
                (self._to_public(0.0, f"dependency header of {f}", head), head.content)
            )

        return items, considered

    @staticmethod
    def _header_chunk(chunks: list):
        for c in chunks:
            if c.kind in ("header", "module"):
                return c
        return min(chunks, key=lambda c: c.start_line) if chunks else None

    @staticmethod
    def _order(items: list) -> list:
        """Group by file (files in relevance order) and sort chunks by line."""
        file_order: dict = {}
        groups: dict = {}
        for chunk, content in items:
            if chunk.file not in groups:
                groups[chunk.file] = []
                file_order[chunk.file] = len(file_order)
            groups[chunk.file].append((chunk, content))
        ordered: list = []
        for file in sorted(groups, key=lambda f: file_order[f]):
            ordered.extend(sorted(groups[file], key=lambda it: it[0].start_line))
        return ordered

    @staticmethod
    def _to_public(score: float, reason: str, code_chunk) -> Chunk:
        return Chunk(
            file=code_chunk.file,
            start_line=code_chunk.start_line,
            end_line=code_chunk.end_line,
            score=float(score),
            reason=reason,
            symbol=code_chunk.symbol,
            kind=code_chunk.kind,
        )

    # -- assembly ---------------------------------------------------------
    def _assemble(self, items: list, by_path: dict, max_tokens: int) -> tuple:
        parts: list = []
        included: list = []
        selected_files: list = []
        seen_files: set = set()
        used = 0

        for chunk, content in items:
            header = ""
            if chunk.file not in seen_files:
                header = f"{_FILE_HEADER}{chunk.file}\n"
            block = header + content
            block_tokens = self.counter.count(block)

            if used + block_tokens <= max_tokens:
                parts.append(block.rstrip("\n"))
                used += block_tokens
                seen_files.add(chunk.file)
                included.append(chunk)
                if chunk.file not in selected_files:
                    selected_files.append(chunk.file)
                continue

            available = max_tokens - used
            if available <= self.counter.count(header) + 10:
                break
            truncated = truncate_to_tokens(block, available, self.counter)
            parts.append(truncated.rstrip("\n"))
            seen_files.add(chunk.file)
            included.append(
                Chunk(
                    file=chunk.file,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    score=chunk.score,
                    reason=chunk.reason + "; truncated to fit budget",
                    symbol=chunk.symbol,
                    kind=chunk.kind,
                )
            )
            if chunk.file not in selected_files:
                selected_files.append(chunk.file)
            break

        return "\n".join(parts), included, selected_files

    @staticmethod
    def _strip_comments(text: str) -> str:
        out_lines = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith(_FILE_HEADER):
                out_lines.append(line)
                continue
            if not stripped:
                continue
            if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("/*"):
                continue
            out_lines.append(line)
        return "\n".join(out_lines)

    def _enforce_cap(self, text: str, max_tokens: int) -> str:
        if self.counter.count(text) <= max_tokens:
            return text
        return truncate_to_tokens(text, max_tokens, self.counter)
