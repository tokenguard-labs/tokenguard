"""CLI demo + verification harness for the Context Engine.

Run:  python run_demo.py
Compares P2-lite (whole-file) against the refined symbol+import-graph pipeline.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "compression"))

from context_engine import ContextEngine, ContextRequest  # noqa: E402
DEMO_REPO = os.path.join(HERE, "examples", "frozen_repo")

QUERIES = [
    "Why is login failing?",
    "Explain userService",
    "Add a unit test for validateEmail",
]


def _run(engine, query, top_k, chunking):
    return engine.build(
        ContextRequest(
            query=query,
            repo_path=DEMO_REPO,
            top_k=top_k,
            max_tokens=3000,
            chunking=chunking,
            expand_deps=True,
        )
    )


def main() -> int:
    if not os.path.isdir(DEMO_REPO):
        print("Demo repo missing. Run: python examples/seed_demo_repo.py")
        return 1

    engine = ContextEngine()
    print(f"Context Engine counter: {engine.counter.label}\n")

    print("=" * 84)
    print("DETAIL: refined (symbol + import graph)")
    print("=" * 84)
    for query in QUERIES:
        result = _run(engine, query, 4, "symbol")
        _print_result(result)

    print()
    print("=" * 84)
    print(f"{'Query':<34}{'Mode':<10}{'Original':>9}{'Selected':>9}{'Saved':>9}{'Files':>7}")
    print("-" * 84)
    for query in QUERIES:
        for mode in ("file", "symbol"):
            r = _run(engine, query, 4, mode)
            t = r.tokens
            print(
                f"{query[:33]:<34}{mode:<10}{t.original:>9}{t.selected:>9}"
                f"{t.saved_pct:>8.1f}%{r.stats.files_selected:>7}"
            )
    print("=" * 84)
    return 0


def _print_result(result) -> None:
    t = result.tokens
    print("-" * 84)
    print(f"tokens: {t.original} -> {t.selected}  ({t.saved_pct}% saved)")
    print(
        f"files : scanned={result.stats.files_scanned} "
        f"considered={result.stats.files_considered} "
        f"selected={result.stats.files_selected} "
        f"({result.stats.duration_ms}ms)"
    )
    for chunk in result.chunks:
        sym = f" ::{chunk.symbol}" if getattr(chunk, "symbol", "") else ""
        lines = f"L{chunk.start_line}-{chunk.end_line}"
        print(f"   + {chunk.file}{sym}  {lines}  score={chunk.score}  [{chunk.reason}]")
    if result.redactions:
        print("redactions:")
        for r in result.redactions:
            print(f"   ! {r.file}:{r.line} ({r.rule}) -> {r.preview}")


if __name__ == "__main__":
    sys.exit(main())
