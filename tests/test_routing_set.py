"""Run the labelled routing query set (bench/routing_queries.json) through the current policy.

This is the "test set over current logic": each query carries an expected complexity category
and initial route, and the test asserts the deterministic policy still matches every label.
Run `python bench/routing_eval.py` for the same result plus a printed table.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Load bench/routing_eval.py (bench is not an importable package).
_EVAL_PATH = os.path.join(_ROOT, "bench", "routing_eval.py")
_spec = importlib.util.spec_from_file_location("routing_eval", _EVAL_PATH)
routing_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(routing_eval)


class TestRoutingQuerySet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.queries = routing_eval.load_queries(routing_eval.DEFAULT_QUERIES)
        cls.report = routing_eval.evaluate(cls.queries)

    def test_every_label_matches_current_logic(self):
        self.assertEqual(
            self.report["mismatches"], [],
            "routing labels no longer match the policy:\n" + routing_eval.render(self.report))

    def test_accuracy_is_perfect(self):
        self.assertEqual(self.report["complexity_accuracy"], 1.0)
        self.assertEqual(self.report["route_accuracy"], 1.0)
        self.assertEqual(self.report["exact_accuracy"], 1.0)

    def test_set_is_representative(self):
        self.assertGreaterEqual(len(self.report["rows"]), 30, "query set too small")
        for category in ("simple", "medium", "complex"):
            self.assertGreaterEqual(self.report["by_category"][category], 5, f"too few {category} cases")
        for route in ("local_first", "cloud_first", "local_only", "cloud_only"):
            self.assertGreaterEqual(self.report["by_route"][route], 1, f"no {route} case")

    def test_ids_are_unique(self):
        ids = [q["id"] for q in self.queries]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
