"""Smoke test for the Stage 0 retrieval evaluation harness."""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "context_eval", os.path.join(_ROOT, "bench", "context_eval.py"))
context_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(context_eval)


class TestContextEval(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("CONTEXT_INDEX_DIR")
        self._saved_assist = os.environ.get("CONTEXT_LLM_ASSIST")
        os.environ["CONTEXT_INDEX_DIR"] = os.path.join(_ROOT, ".pytest_cache", "eval_idx")
        os.environ["CONTEXT_LLM_ASSIST"] = "false"  # deterministic: no local-model calls

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CONTEXT_INDEX_DIR", None)
        else:
            os.environ["CONTEXT_INDEX_DIR"] = self._saved
        if self._saved_assist is None:
            os.environ.pop("CONTEXT_LLM_ASSIST", None)
        else:
            os.environ["CONTEXT_LLM_ASSIST"] = self._saved_assist

    def test_harness_reports_metrics(self):
        import json

        with open(context_eval.DEFAULT_QUERIES, encoding="utf-8") as fh:
            queries = json.load(fh)
        report = context_eval.evaluate(context_eval.build_fixture(), queries)
        self.assertEqual(len(report["rows"]), len(queries))
        for key in ("engine", "index"):
            self.assertIn("recall", report["summary"][key])
            self.assertIn("precision", report["summary"][key])
        # The index must retrieve at least as much relevant context as the baseline on this set.
        self.assertGreaterEqual(report["summary"]["index"]["recall"],
                                report["summary"]["engine"]["recall"])

    def test_budget_is_respected(self):
        import json

        with open(context_eval.DEFAULT_QUERIES, encoding="utf-8") as fh:
            queries = json.load(fh)
        repo = context_eval.build_fixture()
        report = context_eval.evaluate(repo, queries)
        for row, q in zip(report["rows"], queries):
            self.assertLessEqual(row["index"]["tokens"], int(q["budget_tokens"]) + 5)

    def test_localpilot_repo_queries(self):
        """Regression guard for the real prompt that retrieved the wrong files."""
        import json

        path = os.path.join(_ROOT, "bench", "context_queries_localpilot.json")
        with open(path, encoding="utf-8") as fh:
            queries = json.load(fh)
        report = context_eval.evaluate(_ROOT, queries)
        # The index is more precise than the file-level baseline (it emits fewer, more relevant
        # files). Conceptual recall is the gap Stage 2 (semantic retrieval) closes, so we do not
        # require recall parity here yet.
        self.assertGreaterEqual(report["summary"]["index"]["precision"],
                                report["summary"]["engine"]["precision"])
        explain = next(r for r in report["rows"] if r["id"] == "explain_test_cases")
        self.assertGreaterEqual(explain["index"]["recall"], 0.3,
                                "the 'test cases' prompt must retrieve tests/ files")


if __name__ == "__main__":
    unittest.main()
