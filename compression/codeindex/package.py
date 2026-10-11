"""Assemble a budgeted ContextPackage with provenance, adequacy, and bounded model assistance.

Pipeline: retrieve → (optional local-model rerank) → assemble (relevance or corpus scope) →
sufficiency loop (fill/propose missing required, retrieve again, re-assemble) → redact → assess.

Two assembly modes:

- **Relevance** (default): a query about specific behaviour pulls the most relevant symbols/files.
- **Corpus scope**: "refactor all the python code to typescript" is scoped to the whole language
  corpus. We reserve budget for a signature map of every in-scope file and spend the rest on full
  bodies of the highest-value files plus target-language exemplars.

The index is the source of truth; the local model only reorders a shortlist and proposes names,
which the index resolves (docs/codebase-intelligence.md §6.4-§6.5).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from ..context_engine.redact import redact
from ..context_engine.tokens import TokenCounter, truncate_to_tokens
from . import assist, embed as _embed, env
from .retrieve import Candidate, retrieve
from .scope import detect_scope
from .store import CodebaseIndex

_FILE_HEADER = "// file: "
_SIGNATURE_FRACTION = 0.4  # share of the budget reserved for full bodies in corpus mode


@dataclass
class Evidence:
    file: str
    start_line: int
    end_line: int
    symbol: str
    kind: str
    source_hash: str
    derivation_key: str
    retriever: str
    reason: str
    resolution: Optional[str] = None
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Adequacy:
    status: str                       # adequate | partial | inadequate
    missing_required: List[str] = field(default_factory=list)
    uncertain: List[str] = field(default_factory=list)
    dynamic_refs: List[str] = field(default_factory=list)
    confidence: float = 0.0
    recommendation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ContextPackage:
    context_text: str
    items: List[Evidence]
    adequacy: Adequacy
    coverage: Dict[str, Any]
    tokens: Dict[str, int]
    provenance_ok: bool
    task_type: str
    env_digest: str
    latency_ms: int
    redactions: List[str] = field(default_factory=list)
    files_selected: List[str] = field(default_factory=list)

    def router_view(self) -> Dict[str, Any]:
        co = self.coverage
        return {
            "estimated_prompt_tokens": self.tokens.get("selected", 0),
            "retrieval_coverage": co,
            "adequacy_status": self.adequacy.status,
            "missing_required": list(self.adequacy.missing_required),
            "uncertain_references": list(self.adequacy.uncertain),
            "omitted_required": max(0, int(co.get("required_total", 0)) - int(co.get("required_included", 0))),
            "context_budget": self.tokens.get("budget", 0),
            "provenance_ok": self.provenance_ok,
            "task_type": self.task_type,
            "scope": co.get("scope", {}),
            "adequacy_recommendation": self.adequacy.recommendation,
        }


@dataclass
class _Assembled:
    parts: List[str]
    items: List[Evidence]
    files_selected: List[str]
    full_files: Set[str]
    sig_files: Set[str]
    omitted: List[str]
    in_scope_count: int
    corpus_mode: bool


def build(repo_path: str, query: str, *, budget: int = 3000, task_type: str = "general",
          index_dir: Optional[str] = None, min_confidence: float = 0.5,
          embedder=None, assist_llm: bool = True, semantic: bool = True) -> ContextPackage:
    start = time.perf_counter()
    index = CodebaseIndex(repo_path, index_dir=index_dir)
    index.sync()

    counter = TokenCounter()
    files = {r["path"]: r for r in index.files()}
    original_tokens = sum(int(r["token_count"] or 0) for r in files.values())

    if embedder is None and semantic:
        embedder = _embed.get_embedder()
    if embedder is not None:
        try:
            index.embed_missing(embedder)
        except Exception:  # noqa: BLE001 - semantic retrieval is optional
            embedder = None

    candidates = retrieve(index, query, min_confidence=min_confidence, embedder=embedder)
    scope = detect_scope(query)

    model = os.getenv("LOCAL_MODEL", "")
    reranked = False
    if assist_llm and assist.enabled():
        before = candidates
        candidates = assist.rerank(query, candidates, model=model)
        reranked = candidates is not before and bool(model)

    assembled = _assemble(index, files, candidates, scope, counter, budget)

    # Sufficiency loop: fill resolved dependencies that were omitted, and (optionally) ask the local
    # model for missing names, resolve them against the index, then re-assemble. Bounded + fail-open.
    max_iters = int(os.getenv("CONTEXT_SUFFICIENCY_MAX_ITERS", "2"))
    for _ in range(max(0, max_iters)):
        targets = _required_targets(index, assembled.files_selected, min_confidence)
        extra = {p for p, included in targets.items() if not included}
        if assist_llm and assist.enabled():
            extra |= _resolve_proposed(index, assist.propose_missing(query, _summary(assembled), model=model))
        augmented = _augment(index, candidates, extra)
        if len(augmented) == len(candidates):
            break
        candidates = augmented
        assembled = _assemble(index, files, candidates, scope, counter, budget)

    context_text = "\n".join(assembled.parts)
    redacted = context_text
    redaction_details: List[str] = []
    if context_text:
        redacted, redactions = redact(context_text)
        redaction_details = [f"{r.rule} in {r.file}:{r.line}" for r in redactions]
    if counter.count(redacted) > budget:
        redacted = truncate_to_tokens(redacted, budget, counter)

    targets = _required_targets(index, assembled.files_selected, min_confidence)
    adequacy = _assess(index, assembled.files_selected, min_confidence, targets, scope,
                       assembled.in_scope_count, assembled.full_files, assembled.sig_files,
                       assembled.omitted)
    coverage = {
        "files_considered": len({c.path for c in candidates}),
        "files_selected": len(assembled.files_selected),
        "candidates": len(candidates),
        "semantic": embedder is not None,
        "reranked": reranked,
        "required_total": len(targets),
        "required_included": sum(1 for included in targets.values() if included),
        "scope": scope.as_dict(),
        "in_scope_files": assembled.in_scope_count,
        "represented_full": len(assembled.full_files),
        "represented_signature": len(assembled.sig_files),
        "omitted_files": len(assembled.omitted),
    }
    provenance_ok = bool(assembled.items) and all(
        e.file and e.start_line and e.end_line and e.derivation_key for e in assembled.items)

    return ContextPackage(
        context_text=redacted,
        items=assembled.items,
        adequacy=adequacy,
        coverage=coverage,
        tokens={"original": original_tokens, "selected": counter.count(redacted), "budget": budget},
        provenance_ok=provenance_ok,
        task_type=task_type,
        env_digest=env.env_digest("symbols"),
        latency_ms=int((time.perf_counter() - start) * 1000),
        redactions=redaction_details,
        files_selected=assembled.files_selected,
    )


# -- assembly dispatch ------------------------------------------------------------------------

def _assemble(index, files, candidates, scope, counter, budget) -> _Assembled:
    corpus_mode = scope.corpus_wide and bool(scope.source_languages or scope.target_languages)
    if corpus_mode:
        in_scope, _exemplars, full, sig, omitted, parts, items, selected = _assemble_corpus(
            index, files, candidates, scope, counter, budget)
        return _Assembled(parts, items, selected, full, sig, omitted, len(in_scope), True)
    parts, items, selected = _assemble_relevance(files, candidates, counter, budget)
    return _Assembled(parts, items, selected, set(selected), set(), [], len(selected), False)


def _assemble_relevance(files, candidates, counter, budget):
    by_path: Dict[str, list] = {}
    for c in candidates:
        by_path.setdefault(c.path, []).append(c)
    order = sorted(by_path, key=lambda p: (-max(c.score for c in by_path[p]), p))

    parts: List[str] = []
    items: List[Evidence] = []
    selected: List[str] = []
    used = 0
    for path in order:
        header = f"{_FILE_HEADER}{path}\n"
        if used + counter.count(header) > budget:
            continue
        wrote = False
        for c in sorted(by_path[path], key=lambda c: c.start_line):
            prefix = header if not wrote else ""
            block = prefix + c.content.rstrip("\n")
            cost = counter.count(block)
            if used + cost > budget:
                break
            used += cost
            parts.append(block)
            wrote = True
            src = files.get(path)
            sh = src["source_hash"] if src is not None else ""
            items.append(Evidence(
                file=path, start_line=c.start_line, end_line=c.end_line, symbol=c.name,
                kind=c.kind, source_hash=sh, derivation_key=env.derivation_key(sh, "symbols"),
                retriever=c.retriever, reason=c.reason, resolution=c.resolution,
                confidence=c.confidence))
        if wrote:
            selected.append(path)
    return parts, items, selected


def _assemble_corpus(index, files, candidates, scope, counter, budget):
    scores: Dict[str, float] = {}
    for c in candidates:
        scores[c.path] = max(scores.get(c.path, 0.0), c.score)

    in_scope = _in_scope_files(files, scope)
    exemplars = _target_files(files, scope)[:4]
    order = sorted(in_scope, key=lambda p: (-scores.get(p, 0.0), p))

    parts: List[str] = []
    items: List[Evidence] = []
    full_files: Set[str] = set()
    sig_files: Set[str] = set()
    omitted: List[str] = []
    used = 0

    full_budget = int(budget * _SIGNATURE_FRACTION)
    for path in order:
        src = files.get(path)
        if src is None:
            continue
        block = f"{_FILE_HEADER}{path}\n{(src['text'] or '').rstrip()}"
        cost = counter.count(block)
        if cost > full_budget - used:
            continue
        used += cost
        parts.append(block)
        full_files.add(path)
        _append_evidence(items, src, path, "<body>", "file-body", "scope:full",
                         f"in-scope full body ({scope.reason})")

    remaining = budget - used
    for path in order + [p for p in exemplars if p not in full_files]:
        src = files.get(path)
        if src is None or path in full_files or path in sig_files:
            continue
        sig = _signature_map(src["text"] or "", list(index.symbols_in_path(path)))
        block = f"{_FILE_HEADER}{path}\n{sig}"
        cost = counter.count(block)
        if cost > remaining:
            omitted.append(path)
            continue
        remaining -= cost
        used += cost
        parts.append(block)
        sig_files.add(path)
        _append_evidence(items, src, path, "<signatures>", "signature-map", "scope:signature",
                         f"in-scope signature map ({scope.reason})")

    selected = [p for p in order if p in full_files] + [p for p in order if p in sig_files]
    selected += [p for p in exemplars if p in full_files or p in sig_files]
    return in_scope, exemplars, full_files, sig_files, omitted, parts, items, selected


def _append_evidence(items, src_row, path, symbol, kind, retriever, reason):
    sh = src_row["source_hash"]
    end = (src_row["text"] or "").count("\n") + 1
    items.append(Evidence(
        file=path, start_line=1, end_line=end, symbol=symbol, kind=kind, source_hash=sh,
        derivation_key=env.derivation_key(sh, "symbols"), retriever=retriever, reason=reason))


# -- sufficiency loop helpers -----------------------------------------------------------------

def _augment(index: CodebaseIndex, candidates: List[Candidate], extra_paths: Set[str]) -> List[Candidate]:
    existing = {c.path for c in candidates}
    extra: List[Candidate] = []
    for path in sorted(extra_paths):
        if path in existing:
            continue
        for row in index.symbols_in_path(path):
            extra.append(Candidate(
                path=path, name=row["name"] or "", kind=row["kind"] or "",
                start_line=row["start_line"] or 1, end_line=row["end_line"] or row["start_line"] or 1,
                content=row["content"] or "", score=0.5, retriever="sufficiency",
                reason="required by selected context (filled/proposed)"))
    return candidates + extra if extra else candidates


def _resolve_proposed(index: CodebaseIndex, names: Set[str]) -> Set[str]:
    paths: Set[str] = set()
    for name in names:
        for row in index.symbols(name):
            paths.add(row["path"])
    return paths


def _summary(assembled: _Assembled) -> str:
    return "\n".join(sorted({f"{e.file}:{e.symbol}" for e in assembled.items[:40]}))


# -- scope helpers ----------------------------------------------------------------------------

def _in_scope_files(files, scope) -> List[str]:
    if scope.source_languages:
        return [p for p, r in files.items() if r["language"] in scope.source_languages]
    return list(files)


def _target_files(files, scope) -> List[str]:
    tags = scope.target_tags()
    return [p for p, r in files.items() if r["language"] in tags] if tags else []


def _signature_map(text: str, symbols: list) -> str:
    lines: List[str] = []
    for s in sorted(symbols, key=lambda r: r["start_line"] or 0):
        sig = (s["signature"] or "").strip()
        if sig:
            lines.append(f"  {s['start_line']}: {s['kind']} {sig}")
        if len(lines) >= 80:
            break
    if not lines and text:
        lines = [f"  {i}: {line}" for i, line in enumerate(text.splitlines()[:8], start=1)]
    return "// signatures\n" + "\n".join(lines)


# -- adequacy ---------------------------------------------------------------------------------

def _required_targets(index: CodebaseIndex, selected_paths: List[str], min_confidence: float) -> Dict[str, bool]:
    selected = set(selected_paths)
    targets: Dict[str, bool] = {}
    for path in selected_paths:
        related = index.related_paths(path, direction="outgoing", min_confidence=min_confidence)
        for item in related["outgoing"]:
            if item["resolution"] == "resolved" and item["confidence"] >= 0.6:
                targets[item["path"]] = item["path"] in selected
    return targets


def _assess(index, selected_paths, min_confidence, targets, scope, in_scope_count,
            full_files, sig_files, omitted) -> Adequacy:
    missing = sorted(p for p, included in targets.items() if not included)
    required_total = len(targets)
    required_included = required_total - len(missing)

    uncertain = set()
    for path in selected_paths:
        for e in index.edges(src_path=path, min_confidence=0.0):
            if e["resolution"] != "resolved" and e["dst_name"]:
                uncertain.add(e["dst_name"])
    dynamic = sorted(index.dynamic_paths() & set(selected_paths))

    recommendation = None
    if scope.corpus_wide and in_scope_count:
        represented = len(full_files) + len(sig_files)
        if omitted:
            status = "inadequate" if represented < in_scope_count * 0.5 else "partial"
        elif len(full_files) >= in_scope_count:
            status = "adequate"
        else:
            status = "partial"
        if len(full_files) < in_scope_count:
            recommendation = (
                f"This spans {in_scope_count} file(s); {len(full_files)} included in full, "
                f"{len(sig_files)} as signatures, {len(omitted)} omitted. Consider a file-by-file plan.")
    elif not selected_paths:
        status = "inadequate"
    elif missing:
        status = "partial"
    else:
        status = "adequate"

    confidence = (required_included / required_total) if required_total else (
        1.0 if selected_paths else 0.0)
    return Adequacy(
        status=status, missing_required=missing, uncertain=sorted(uncertain),
        dynamic_refs=dynamic, confidence=round(confidence, 3), recommendation=recommendation)
