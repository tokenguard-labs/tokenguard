"""Evaluate the deterministic routing policy against the labelled query set.

    python bench/routing_eval.py [path/to/queries.json]

The set (bench/routing_queries.json) pairs representative coding requests with an expected
complexity category and initial route. It is deterministic and model-free: it exercises the
classifier and the hard policy, not model quality. Use it to tune the heuristic weights,
keywords and thresholds, and as a regression guard (tests/test_routing_set.py asserts it).

To evaluate *answer* quality (task success, escalation rate, cloud tokens, latency, privacy
compliance) pair this with the context benchmarks and a task rubric.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import analyzer, config, routing  # noqa: E402

DEFAULT_QUERIES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routing_queries.json")
DEFAULT_LOCAL_MODEL = "qwen2.5-coder:7b"
PROMPT_HEADROOM_TOKENS = 100  # rough prompt size when sizing the local capability check


def load_queries(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def plan_for(case: dict) -> routing.RoutePlan:
    """Run the current policy for one labelled case (no model is called)."""
    context_tokens = int(case.get("context_tokens", 0))
    analysis = analyzer.analyze(
        case["prompt"],
        context_tokens=context_tokens,
        num_repo_files=int(case.get("num_repo_files", 0)),
    )
    return routing.plan_route(
        case.get("force_route", "auto"),
        prompt=case["prompt"],
        analysis=analysis,
        estimated_context_tokens=context_tokens + PROMPT_HEADROOM_TOKENS,
        local_model=case.get("local_model", DEFAULT_LOCAL_MODEL),
        local_available=case.get("local_available", True),
        cloud_allowed=case.get("cloud_allowed", True),
        requires_tools=case.get("requires_tools"),
    )


def evaluate(cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        plan = plan_for(case)
        want_c = case.get("expected_complexity")
        want_r = case.get("expected_initial_route")
        rows.append({
            "id": case["id"],
            "complexity": plan.complexity,
            "expected_complexity": want_c,
            "initial_route": plan.initial_route,
            "expected_initial_route": want_r,
            "score_100": plan.complexity_score_100,
            "local_capable": plan.local_capable,
            "complexity_ok": want_c is None or plan.complexity == want_c,
            "route_ok": want_r is None or plan.initial_route == want_r,
        })
    n = len(rows) or 1
    mismatches = [r for r in rows if not (r["complexity_ok"] and r["route_ok"])]
    by_cat = {c: sum(r["complexity"] == c for r in rows) for c in ("simple", "medium", "complex")}
    by_route = {r: sum(x["initial_route"] == r for x in rows)
                for r in ("local_first", "cloud_first", "local_only", "cloud_only")}
    return {
        "rows": rows,
        "mismatches": mismatches,
        "complexity_accuracy": round(sum(r["complexity_ok"] for r in rows) / n, 3),
        "route_accuracy": round(sum(r["route_ok"] for r in rows) / n, 3),
        "exact_accuracy": round(sum(r["complexity_ok"] and r["route_ok"] for r in rows) / n, 3),
        "by_category": by_cat,
        "by_route": by_route,
    }


def render(report: dict) -> str:
    lines = [f"{'id':26} {'complexity':>10} {'score':>6} {'route':>12} {'ok':>4}", "-" * 66]
    for r in report["rows"]:
        ok = "yes" if (r["complexity_ok"] and r["route_ok"]) else "NO"
        lines.append(f"{r['id']:26} {r['complexity']:>10} {r['score_100']:>6} {r['initial_route']:>12} {ok:>4}")
    lines += [
        "-" * 66,
        f"complexity accuracy: {report['complexity_accuracy']:.0%}   "
        f"route accuracy: {report['route_accuracy']:.0%}   exact: {report['exact_accuracy']:.0%}",
        f"categories: {report['by_category']}",
        f"routes: {report['by_route']}",
    ]
    if report["mismatches"]:
        lines.append("mismatches:")
        for r in report["mismatches"]:
            detail = []
            if not r["complexity_ok"]:
                detail.append(f"complexity {r['complexity']} != {r['expected_complexity']}")
            if not r["route_ok"]:
                detail.append(f"route {r['initial_route']} != {r['expected_initial_route']}")
            lines.append(f"  {r['id']}: " + "; ".join(detail))
    lines.append(f"(weights={config.COMPLEXITY_WEIGHTS}, "
                 f"simple<={config.COMPLEXITY_SIMPLE_MAX}, medium<={config.COMPLEXITY_MEDIUM_MAX})")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_QUERIES
    report = evaluate(load_queries(path))
    print(render(report))
    return 0 if not report["mismatches"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
