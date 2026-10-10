"""Stdlib unittest coverage for the P2-lite Context Engine."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "compression"))

from context_engine import ContextEngine, ContextRequest  # noqa: E402
from context_engine.config import ContextConfig  # noqa: E402
from context_engine.redact import redact  # noqa: E402
from context_engine.selector import extract_terms, score_file  # noqa: E402
from context_engine.walker import walk_repo  # noqa: E402


class TempRepo:
    def __init__(self, files: dict):
        self._dir = tempfile.mkdtemp(prefix="p2test_")
        for rel, content in files.items():
            full = os.path.join(self._dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)

    @property
    def path(self):
        return self._dir


class TestRedaction(unittest.TestCase):
    def test_assigned_secret_is_redacted_but_key_kept(self):
        text = '// file: a.ts\nconst API_KEY = "sk-abcdef0123456789abcdef";\n'
        out, reds = redact(text)
        self.assertIn("API_KEY", out)
        self.assertNotIn("sk-abcdef0123456789abcdef", out)
        self.assertIn("[REDACTED]", out)
        self.assertEqual(reds[0].file, "a.ts")
        self.assertEqual(reds[0].line, 1)

    def test_ordinary_token_expression_untouched(self):
        text = '// file: a.ts\nconst token = getToken();\n'
        out, reds = redact(text)
        self.assertEqual(out, text)
        self.assertEqual(reds, [])

    def test_vendor_patterns(self):
        text = "// file: a.ts\nx = 'AKIAABCDEFGHIJKLMNOP'\n"
        out, reds = redact(text)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", out)
        self.assertTrue(any(r.rule == "aws_access_key" for r in reds))


class TestSelector(unittest.TestCase):
    def test_terms_split_camel_and_snake(self):
        terms = extract_terms("findUserByEmail and user_service")
        self.assertIn("user", terms)
        self.assertIn("email", terms)
        self.assertIn("service", terms)

    def test_path_match_outranks_content_only(self):
        repo = TempRepo(
            {
                "src/auth/login.ts": "export function x() {}\n",
                "src/utils/other.ts": "// mentions login once\n",
            }
        )
        cfg = ContextConfig()
        files = {f.rel_path: f for f in walk_repo(repo.path, cfg)}
        terms = extract_terms("login")
        login_score, _ = score_file(files["src/auth/login.ts"], terms)
        other_score, _ = score_file(files["src/utils/other.ts"], terms)
        self.assertGreater(login_score, other_score)


class TestWalker(unittest.TestCase):
    def test_excludes_node_modules_and_dist(self):
        repo = TempRepo(
            {
                "src/app.ts": "export const a = 1;\n",
                "node_modules/lib/index.ts": "export const b = 2;\n",
                "dist/bundle.ts": "export const c = 3;\n",
            }
        )
        rels = [f.rel_path for f in walk_repo(repo.path, ContextConfig())]
        self.assertEqual(rels, ["src/app.ts"])


class TestEngine(unittest.TestCase):
    def _repo(self):
        return TempRepo(
            {
                "src/auth/login.ts": "export function login() { return 1; }\n",
                "src/auth/auth.ts": (
                    "// login validation\n"
                    'const SECRET = "sk-abcdef0123456789abcdef";\n'
                    "export function validate() { return true; }\n"
                ),
                "src/utils/helper.ts": "export const helper = 1;\n",
            }
        )

    def test_hard_cap_never_exceeded(self):
        engine = ContextEngine()
        repo = self._repo()
        result = engine.build(
            ContextRequest(
                query="login", repo_path=repo.path, top_k=8, max_tokens=40
            )
        )
        self.assertLessEqual(engine.counter.count(result.context_text), 40)

    def test_deterministic(self):
        engine = ContextEngine()
        repo = self._repo()
        req = ContextRequest(query="login validation", repo_path=repo.path, top_k=3)

        def stable(r):
            d = r.to_dict()
            d["stats"].pop("duration_ms", None)
            return d

        self.assertEqual(stable(engine.build(req)), stable(engine.build(req)))

    def test_no_match_fallback_is_non_empty(self):
        engine = ContextEngine()
        repo = self._repo()
        result = engine.build(
            ContextRequest(query="zzzznomatch", repo_path=repo.path, top_k=2)
        )
        self.assertTrue(result.context_text)
        self.assertTrue(result.chunks)
        self.assertEqual(result.stats.files_considered, 0)

    def test_secret_redacted_end_to_end_and_savings_reported(self):
        engine = ContextEngine()
        repo = self._repo()
        result = engine.build(
            ContextRequest(query="login", repo_path=repo.path, top_k=2)
        )
        self.assertNotIn("sk-abcdef0123456789abcdef", result.context_text)
        self.assertTrue(result.redactions)
        self.assertGreaterEqual(result.tokens.original, result.tokens.selected)
        self.assertGreaterEqual(result.tokens.saved_pct, 0.0)


if __name__ == "__main__":
    unittest.main()
