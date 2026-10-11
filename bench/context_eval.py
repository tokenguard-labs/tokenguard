"""Stage 0: retrieval evaluation harness.

Runs a labelled query set against two retrievers and reports retrieval quality + cost:

    python bench/context_eval.py                 # deterministic built-in fixture
    python bench/context_eval.py --repo /path --queries q.json

Metrics per query: recall, precision, selected tokens, token reduction vs full context, latency.
Baseline = the in-memory `context_engine`; candidate = the persistent `codeindex`. Optimize only
against this baseline, and never trade answer quality for token savings (docs/codebase-intelligence.md §8).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compression import codeindex  # noqa: E402
from compression.context_engine import ContextEngine, ContextRequest  # noqa: E402
from compression.context_engine.walker import walk_repo  # noqa: E402
from compression.context_engine.config import ContextConfig  # noqa: E402

DEFAULT_QUERIES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context_queries.json")

_FIXTURE = {
    "src/auth/auth.py": (
        "def hash_password(pw):\n    return pw[::-1]\n\n"
        "def validate_token(token):\n    return len(token) > 0\n"
    ),
    "src/auth/login.py": (
        "from .auth import validate_token\n\n"
        "def login(token):\n    return validate_token(token)\n"
    ),
    "src/db/pool.py": (
        "from ..config import POOL_TIMEOUT\n\n"
        "class ConnectionPool:\n"
        "    def acquire(self):\n        return 'conn', POOL_TIMEOUT\n"
        "    def release(self, conn):\n        return True\n"
    ),
    "src/db/store.py": (
        "def get_user(uid):\n    return {'id': uid}\n"
    ),
    "src/api/routes.py": (
        "from ..auth.login import login\nfrom ..db.store import get_user\n\n"
        "def login_route(token):\n    if login(token):\n        return get_user(1)\n    return None\n\n"
        "def health():\n    return 'ok'\n"
    ),
    "src/config.py": "POOL_TIMEOUT = 30\n",
}


def build_fixture() -> str:
    repo = tempfile.mkdtemp(prefix="context_eval_")
    for rel, content in _FIXTURE.items():
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    return repo


def _full_tokens(repo: str) -> int:
    from backend.tokens import count_tokens

    total = 0
    for f in walk_repo(repo, ContextConfig()):
        total += count_tokens(f.text)
    return total


def _recall_precision(expected, retrieved) -> tuple:
    exp, got = set(expected), set(retrieved)
    recall = len(exp & got) / len(exp) if exp else 0.0
    precision = len(exp & got) / len(got) if got else 0.0
    return round(recall, 3), round(precision, 3)


def _engine(retriever: str, repo: str, query: str, budget: int):
    from backend.tokens import count_tokens

    if retriever == "index":
        pkg = codeindex.build(repo, query, budget=budget)
        return list(pkg.files_selected), pkg.tokens["selected"]
    result = ContextEngine().build(
        ContextRequest(query=query, repo_path=repo, top_k=8, max_tokens=budget))
    return list(result.selected_files), count_tokens(result.context_text)


def evaluate(repo: str, queries: list) -> dict:
    if os.environ.get("CONTEXT_INDEX_DIR") is None:
        os.environ["CONTEXT_INDEX_DIR"] = tempfile.mkdtemp(prefix="context_eval_idx_")
    codeindex.CodebaseIndex(repo).sync()  # warm the index once
    full = _full_tokens(repo)
    rows = []
    for q in queries:
        row = {"id": q["id"], "expected": q["expected_files"], "full_tokens": full}
        for retriever in ("engine", "index"):
            start = time.perf_counter()
            files, selected = _engine(retriever, repo, q["prompt"], int(q.get("budget_tokens", 500)))
            latency = int((time.perf_counter() - start) * 1000)
            recall, precision = _recall_precision(q["expected_files"], files)
            row[retriever] = {
                "files": files, "recall": recall, "precision": precision,
                "tokens": selected, "reduction_pct": round(100 * (full - selected) / full, 1) if full else 0.0,
                "latency_ms": latency,
            }
        rows.append(row)

    def _avg(retriever, key):
        return round(sum(r[retriever][key] for r in rows) / len(rows), 3) if rows else 0.0

    return {
        "rows": rows,
        "summary": {
            "engine": {"recall": _avg("engine", "recall"), "precision": _avg("engine", "precision"),
                       "tokens": _avg("engine", "tokens"), "latency_ms": _avg("engine", "latency_ms")},
            "index": {"recall": _avg("index", "recall"), "precision": _avg("index", "precision"),
                      "tokens": _avg("index", "tokens"), "latency_ms": _avg("index", "latency_ms")},
        },
    }


def render(report: dict) -> str:
    lines = [f"{'id':16} {'engine r/p':>14} {'index r/p':>14} {'idx tokens':>11}", "-" * 60]
    for r in report["rows"]:
        e, i = r["engine"], r["index"]
        lines.append(
            f"{r['id']:16} {e['recall']:.2f}/{e['precision']:.2f}".ljust(33)
            + f"{i['recall']:.2f}/{i['precision']:.2f}".rjust(12) + f"{i['tokens']:>12}")
    s = report["summary"]
    lines += [
        "-" * 60,
        f"avg engine: recall {s['engine']['recall']} precision {s['engine']['precision']} "
        f"tokens {s['engine']['tokens']} latency {s['engine']['latency_ms']}ms",
        f"avg index : recall {s['index']['recall']} precision {s['index']['precision']} "
        f"tokens {s['index']['tokens']} latency {s['index']['latency_ms']}ms",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=None, help="repository path (default: built-in fixture)")
    parser.add_argument("--queries", default=DEFAULT_QUERIES)
    args = parser.parse_args(argv)
    repo = args.repo or build_fixture()
    with open(args.queries, encoding="utf-8") as fh:
        queries = json.load(fh)
    print(render(evaluate(repo, queries)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
