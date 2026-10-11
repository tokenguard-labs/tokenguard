"""Tests for the classification and routing policy (backend/routing.py + analyzer.py).

These are deterministic and model-free: capabilities are passed in explicitly and the few
router-level tests mock the model adapters. They cover the Phase 10 checklist.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from backend import analyzer, classifier, config, main, routing  # noqa: E402
from backend.contracts import ContextResult  # noqa: E402
from backend.llm import LLMResult  # noqa: E402
from backend import router  # noqa: E402

LOCAL_MODEL = "qwen2.5-coder:7b"


def _analysis(prompt: str, *, context_tokens: int = 0, num_repo_files: int = 0) -> analyzer.Analysis:
    return analyzer.analyze(prompt, context_tokens=context_tokens, num_repo_files=num_repo_files)


def _plan(prompt: str, *, requested: str = "auto", context_tokens: int = 0, num_repo_files: int = 0,
          local_available: bool = True, cloud_allowed: bool = True, local_model: str = LOCAL_MODEL,
          requires_tools=None):
    return routing.plan_route(
        requested,
        prompt=prompt,
        analysis=_analysis(prompt, context_tokens=context_tokens, num_repo_files=num_repo_files),
        estimated_context_tokens=context_tokens + 100,
        local_model=local_model,
        local_available=local_available,
        cloud_allowed=cloud_allowed,
        requires_tools=requires_tools,
    )


class TestComplexityCategory(unittest.TestCase):
    def test_matrix(self):
        for prompt, tokens, files, want in [
            ("Explain what this function does", 0, 0, "simple"),
            ("Debug this small module and explain the trade-offs", 1500, 3, "medium"),
            ("Refactor the authentication architecture across the codebase", 4000, 12, "complex"),
        ]:
            a = _analysis(prompt, context_tokens=tokens, num_repo_files=files)
            self.assertEqual(a.complexity, want, prompt)

    def test_score_100_and_category_agree(self):
        a = _analysis("Debug this small module and explain the trade-offs", context_tokens=1500, num_repo_files=3)
        self.assertEqual(a.complexity, analyzer.complexity_category(a.complexity_score_100))
        self.assertGreaterEqual(a.complexity_score_100, 0)
        self.assertLessEqual(a.complexity_score_100, 100)


class TestHardPolicy(unittest.TestCase):
    def test_simple_is_local_first(self):  # case 1
        plan = _plan("Explain what this function does")
        self.assertEqual(plan.initial_route, "local_first")
        self.assertTrue(plan.escalate_allowed)

    def test_medium_is_local_first(self):  # case 2
        plan = _plan("Debug this small module and explain the trade-offs", context_tokens=1500, num_repo_files=3)
        self.assertEqual(plan.initial_route, "local_first")

    def test_complex_is_cloud_first_when_permitted(self):  # case 3
        plan = _plan("Refactor the authentication architecture across the codebase",
                     context_tokens=4000, num_repo_files=12)
        self.assertEqual(plan.initial_route, "cloud_first")
        self.assertEqual(plan.decision, "cloud")
        self.assertFalse(plan.escalate_allowed)

    def test_unsupported_capability_routes_cloud(self):  # case 4
        plan = _plan("Explain what this function does", requires_tools=True)
        self.assertFalse(plan.local_capable)
        self.assertEqual(plan.initial_route, "cloud_first")

    def test_local_unavailable_routes_cloud(self):  # case 4b
        plan = _plan("Explain what this function does", local_available=False)
        self.assertFalse(plan.local_capable)
        self.assertEqual(plan.initial_route, "cloud_first")

    def test_context_bigger_than_local_window(self):  # case 4c
        plan = _plan("Explain what this function does", context_tokens=9000, local_model="qwen2.5-coder:0.5b")
        self.assertFalse(plan.local_capable)
        self.assertEqual(plan.initial_route, "cloud_first")

    def test_local_only_never_cloud(self):  # case 5
        plan = _plan("Refactor the authentication architecture across the codebase",
                     requested="local", context_tokens=4000, num_repo_files=12)
        self.assertEqual(plan.initial_route, "local_only")
        self.assertFalse(plan.escalate_allowed)
        self.assertEqual(plan.decision, "local")

    def test_cloud_only_skips_local(self):  # case 6
        plan = _plan("Explain what this function does", requested="cloud")
        self.assertEqual(plan.initial_route, "cloud_only")


class TestEscalation(unittest.TestCase):
    def _decide(self, **kw):
        args = dict(
            requested="auto", confidence=0.9, initial_route="local_first",
            needs_cloud=False, local_failed=False, output_valid=True,
            cloud_allowed=True, escalation_enabled=True, escalations_used=0,
            threshold=0.7, missing_policy="escalate",
        )
        args.update(kw)
        return routing.decide_escalation(**args)

    def test_high_confidence_no_escalation(self):  # case 7
        self.assertFalse(self._decide(confidence=0.9).escalate)

    def test_low_confidence_escalates(self):  # case 8
        d = self._decide(confidence=0.69)
        self.assertTrue(d.escalate)
        self.assertEqual(d.kind, "confidence")

    def test_threshold_exactly_no_escalation(self):  # case 9
        self.assertFalse(self._decide(confidence=0.7).escalate)

    def test_missing_confidence_fallback_escalate(self):  # case 10a
        self.assertTrue(self._decide(confidence=None, missing_policy="escalate").escalate)

    def test_missing_confidence_fallback_keep(self):  # case 10b
        self.assertFalse(self._decide(confidence=None, missing_policy="keep").escalate)

    def test_invalid_confidence_fallback(self):  # case 10c
        self.assertEqual(self._decide(confidence="nope").kind, "missing_confidence")

    def test_malformed_output_escalates_despite_high_confidence(self):  # case 11
        d = self._decide(confidence=0.95, output_valid=False)
        self.assertTrue(d.escalate)
        self.assertEqual(d.kind, "validation")

    def test_local_failure_uses_failure_policy(self):  # case 12
        d = self._decide(confidence=0.99, local_failed=True, local_error="ollama down")
        self.assertTrue(d.escalate)
        self.assertEqual(d.kind, "error")

    def test_cloud_disabled_no_escalation(self):  # case 13
        self.assertFalse(self._decide(confidence=0.1, cloud_allowed=False).escalate)

    def test_privacy_restriction_no_escalation(self):  # case 14
        self.assertFalse(self._decide(confidence=0.1, cloud_allowed=False).escalate)

    def test_no_routing_loop_after_limit(self):  # case 15
        self.assertFalse(self._decide(confidence=0.1, escalations_used=config.MAX_CLOUD_ESCALATIONS).escalate)

    def test_local_only_request_never_escalates(self):  # case 5 (escalation side)
        self.assertFalse(self._decide(requested="local", confidence=0.0).escalate)

    def test_existing_behavior_simple_local_complex_cloud(self):  # case 18
        self.assertEqual(_plan("Explain what this function does").decision, "local_first")
        self.assertEqual(
            _plan("Refactor the authentication architecture across the codebase",
                  context_tokens=4000, num_repo_files=12).decision,
            "cloud")

    def test_profile_parsing(self):
        p = routing.profile_for("qwen2.5-coder:14b")
        self.assertEqual(p.params_billions, 14.0)
        self.assertEqual(p.supports_multifile_reasoning, "yes")
        small = routing.profile_for("qwen2.5-coder:1.5b")
        self.assertEqual(small.supports_multifile_reasoning, "no")
        self.assertFalse(small.supports_tool_calling)


class TestRouterIntegration(unittest.TestCase):
    """Cases 16/17: escalation preserves context, and the final route is reported."""

    def _ctx(self):
        return ContextResult(
            context="CTX", original_tokens=100, optimized_tokens=50,
            files_selected=["a.py"], secrets_redacted=0, optimizer="test")

    def _run(self, prompt, *, force_route="auto", confidence=0.2, local_answer="draft",
             cloud_enabled=True, local_error=None, model_complexity=None, model_reason=""):
        async def fake_local(p, context, emit=None):
            if local_error:
                raise RuntimeError(local_error)
            return LLMResult(answer=local_answer, model="local", latency_ms=5, confidence=confidence)

        local_mock = AsyncMock(side_effect=fake_local)
        cloud_mock = AsyncMock(return_value=LLMResult(
            answer="final", model="cloud", latency_ms=7, input_tokens=42, output_tokens=9))
        clf = (None if model_complexity is None
               else classifier.ModelClassification(complexity=model_complexity, reason=model_reason))
        with patch.object(router, "compress", return_value=self._ctx()), \
                patch.object(router, "redact_text", return_value=("PROMPT", 0)), \
                patch.object(router, "ollama_reachable", new=AsyncMock(return_value=True)), \
                patch.object(classifier, "classify_local", new=AsyncMock(return_value=clf)), \
                patch.object(router, "call_local", new=local_mock), \
                patch.object(router, "call_cloud", new=cloud_mock), \
                patch.object(config, "CLOUD_ENABLED", cloud_enabled):
            result = asyncio.run(router.handle(prompt, force_route=force_route))
        return result, local_mock, cloud_mock

    def test_escalation_preserves_prompt_and_context(self):
        result, local_mock, cloud_mock = self._run("Debug this small module and explain the trade-offs")
        self.assertTrue(result["escalated"])
        self.assertEqual(result["route"], "cloud")
        self.assertEqual(result["final_route"], "cloud")
        self.assertEqual(result["escalation_kind"], "confidence")
        # Both models saw the same prompt and the same optimized context.
        self.assertEqual(local_mock.await_args.args[0], "Debug this small module and explain the trade-offs")
        self.assertEqual(local_mock.await_args.args[1], "CTX")
        self.assertEqual(cloud_mock.await_args.args[1], "CTX")

    def test_local_only_does_not_call_cloud(self):
        result, _local, cloud_mock = self._run("Debug this small module and explain the trade-offs",
                                               force_route="local", confidence=0.1)
        self.assertEqual(result["route"], "local")
        self.assertFalse(result["escalated"])
        cloud_mock.assert_not_awaited()

    def test_cloud_disabled_keeps_request_local(self):
        result, _local, cloud_mock = self._run(
            "Refactor the authentication architecture across the codebase", cloud_enabled=False)
        self.assertEqual(result["route"], "local")
        self.assertFalse(result["cloud_allowed"])
        cloud_mock.assert_not_awaited()

    def test_local_failure_escalates(self):
        result, _local, cloud_mock = self._run(
            "Debug this small module and explain the trade-offs", local_error="ollama down")
        self.assertEqual(result["escalation_kind"], "error")
        self.assertEqual(result["route"], "cloud")
        cloud_mock.assert_awaited()

    def test_result_matches_api_schema(self):  # case 17
        result, _local, _cloud = self._run("Debug this small module and explain the trade-offs")
        self.assertIn("final_route", main.ChatResponse.model_fields)
        model = main.ChatResponse(**result)  # must not raise on the pipeline's output
        self.assertEqual(model.final_route, "cloud")
        self.assertEqual(model.requested_route, "auto")

    def test_refusal_escalates_as_validation(self):
        result, _local, cloud_mock = self._run(
            "Debug this small module and explain the trade-offs", confidence=0.95,
            local_answer="I apologize, but I cannot see the provided context.")
        self.assertTrue(result["escalated"])
        self.assertEqual(result["escalation_kind"], "validation")
        self.assertEqual(result["route"], "cloud")
        cloud_mock.assert_awaited()

    def test_tiebreaker_promotes_medium_to_cloud(self):
        result, local_mock, cloud_mock = self._run(
            "Debug this small module and explain the trade-offs", model_complexity="complex")
        self.assertEqual(result["classified_by"], "local_llm")
        self.assertEqual(result["model_complexity"], "complex")
        self.assertEqual(result["initial_route"], "cloud_first")
        self.assertEqual(result["route"], "cloud")
        local_mock.assert_not_awaited()
        cloud_mock.assert_awaited()

    def test_classifier_reason_surfaces(self):
        result, *_ = self._run("Debug this small module and explain the trade-offs",
                               model_complexity="complex", model_reason="spans multiple modules")
        self.assertEqual(result["model_complexity_reason"], "spans multiple modules")
        self.assertIn("spans multiple modules", " ".join(result["routing_reasons"]))

    def test_tiebreaker_keeps_medium_local_when_simple(self):
        result, local_mock, _cloud = self._run(
            "Debug this small module and explain the trade-offs", model_complexity="simple")
        self.assertEqual(result["classified_by"], "local_llm")
        self.assertEqual(result["complexity"], "simple")
        self.assertEqual(result["initial_route"], "local_first")
        local_mock.assert_awaited()


class TestClassifierParsing(unittest.TestCase):
    def test_parses_plain_json(self):
        c = classifier._parse('{"complexity": "complex", "needs_cloud": true, "reason": "multi-file"}')
        self.assertEqual(c.complexity, "complex")
        self.assertTrue(c.needs_cloud)

    def test_parses_json_embedded_in_text(self):
        c = classifier._parse('Sure!\n{"complexity":"simple","reason":"regex"}\nthanks')
        self.assertEqual(c.complexity, "simple")

    def test_rejects_unknown_complexity(self):
        self.assertIsNone(classifier._parse('{"complexity": "huge"}'))

    def test_rejects_non_json(self):
        self.assertIsNone(classifier._parse("I think it is simple"))


class TestComplexityHint(unittest.TestCase):
    def _medium_plan(self, hint):
        prompt = "Debug this small module and explain the trade-offs"
        analysis = _analysis(prompt, context_tokens=1500, num_repo_files=3)
        self.assertEqual(analysis.complexity, "medium")
        return routing.plan_route(
            "auto", prompt=prompt, analysis=analysis, estimated_context_tokens=1600,
            local_model=LOCAL_MODEL, local_available=True, cloud_allowed=True,
            complexity_hint=hint, model_complexity=hint,
        )

    def test_hint_complex_promotes_route(self):
        plan = self._medium_plan("complex")
        self.assertEqual(plan.complexity, "complex")
        self.assertEqual(plan.initial_route, "cloud_first")
        self.assertEqual(plan.classified_by, "local_llm")
        self.assertEqual(plan.heuristic_complexity, "medium")

    def test_hint_simple_demotes_route(self):
        plan = self._medium_plan("simple")
        self.assertEqual(plan.complexity, "simple")
        self.assertEqual(plan.initial_route, "local_first")

    def test_no_hint_keeps_heuristic(self):
        plan = self._medium_plan(None)
        self.assertEqual(plan.complexity, "medium")
        self.assertEqual(plan.classified_by, "heuristic")


if __name__ == "__main__":
    unittest.main()
