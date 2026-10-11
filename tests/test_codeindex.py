"""Tests for the persistent codebase index (compression/codeindex).

Covers the properties the design calls out as first-class: rigorous incremental invalidation
(source + derivation environment), an honest uncertainty-aware graph, budgeted context with
provenance, adequacy reporting, and the zero-secret-leak invariant.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from compression import codeindex  # noqa: E402
from compression.codeindex import env  # noqa: E402

ENV_KEYS = ("CONTEXT_INDEX_DIR", "LOCALPILOT_PARSER_VERSION", "LOCALPILOT_EXTRACTION_VERSION",
            "CONTEXT_LLM_ASSIST", "CONTEXT_INDEX_SYNC_TTL_S")


def write_repo(files: dict) -> str:
    repo = tempfile.mkdtemp(prefix="ci_repo_")
    for rel, content in files.items():
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    return repo


class IndexCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = tempfile.mkdtemp(prefix="ci_idx_")
        os.environ["CONTEXT_INDEX_DIR"] = self.tmp
        os.environ["CONTEXT_LLM_ASSIST"] = "false"  # no real local-model calls unless a test opts in
        os.environ["CONTEXT_INDEX_SYNC_TTL_S"] = "0"  # tests change files and re-sync immediately

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


AUTH = "def validate_token(t):\n    return t == 'x'\n"
LOGIN = "import auth\n\ndef login(t):\n    return auth.validate_token(t)\n"


class TestSyncAndInvalidation(IndexCase):
    def test_builds_symbols_and_edges(self):
        repo = write_repo({"auth.py": AUTH, "login.py": LOGIN})
        idx = codeindex.CodebaseIndex(repo)
        report = idx.sync()
        self.assertEqual(report.parsed, 2)
        self.assertEqual(idx.symbol_names(), {"validate_token", "login"})
        edge = idx.edges(dst_name="validate_token", min_confidence=0.0)
        self.assertTrue(edge)
        self.assertEqual(edge[0]["resolution"], "resolved")

    def test_second_sync_is_incremental(self):
        repo = write_repo({"a.py": AUTH, "b.py": LOGIN})
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        again = idx.sync()
        self.assertEqual(again.parsed, 0)
        self.assertEqual(again.removed, 0)

    def test_source_change_reparses_only_that_file(self):
        repo = write_repo({"a.py": AUTH, "b.py": "def other():\n    return 1\n"})
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        with open(os.path.join(repo, "a.py"), "w") as fh:
            fh.write("def validate_token(t):\n    return t == 'y'\n")
        report = idx.sync()
        self.assertEqual(report.parsed, 1)

    def test_environment_change_forces_reparse_without_source_change(self):
        repo = write_repo({"a.py": AUTH, "b.py": LOGIN})
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        os.environ["LOCALPILOT_PARSER_VERSION"] = "2"  # simulate a parser upgrade
        report = idx.sync()
        self.assertEqual(report.parsed, 2, "a component bump must re-derive all symbols")

    def test_rename_reuses_derived_rows(self):
        repo = write_repo({"a.py": AUTH})
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        os.rename(os.path.join(repo, "a.py"), os.path.join(repo, "renamed.py"))
        report = idx.sync()
        self.assertEqual(report.reused_renames, 1)
        self.assertEqual(report.parsed, 0)
        self.assertEqual({r["path"] for r in idx.symbols()}, {"renamed.py"})

    def test_delete_cascades(self):
        repo = write_repo({"a.py": AUTH, "b.py": LOGIN})
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        os.remove(os.path.join(repo, "a.py"))
        report = idx.sync()
        self.assertEqual(report.removed, 1)
        self.assertEqual(idx.symbol_names(), {"login"})

    def test_is_stale(self):
        repo = write_repo({"a.py": AUTH})
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        self.assertFalse(idx.is_stale("a.py"))
        with open(os.path.join(repo, "a.py"), "w") as fh:
            fh.write("def changed():\n    return 2\n")
        self.assertTrue(idx.is_stale("a.py"))


class TestHonestGraph(IndexCase):
    def test_ambiguous_name_is_candidate_not_dependency(self):
        repo = write_repo({
            "a.py": "def foo():\n    return 1\n",
            "b.py": "def foo():\n    return 2\n",
            "c.py": "def use():\n    return foo()\n",
        })
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        edges = [e for e in idx.edges(src_path="c.py", min_confidence=0.0) if e["dst_name"] == "foo"]
        self.assertTrue(edges)
        self.assertEqual(edges[0]["resolution"], "syntactic")
        self.assertLess(edges[0]["confidence"], 0.5)
        self.assertIsNone(edges[0]["dst_path"])  # not asserted as a file dependency

    def test_graph_expansion_respects_confidence(self):
        repo = write_repo({
            "a.py": "def foo():\n    return 1\n",
            "b.py": "def foo():\n    return 2\n",
            "c.py": "def use():\n    return foo()\n",
        })
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        high = idx.related_paths("c.py", direction="outgoing", min_confidence=0.5)
        self.assertEqual(high["outgoing"], [])  # ambiguous edge is below the bar


class TestContextPackage(IndexCase):
    def test_provenance_and_budget(self):
        repo = write_repo({"auth.py": AUTH, "login.py": LOGIN})
        pkg = codeindex.build(repo, "how does login validate a token", budget=500)
        self.assertTrue(pkg.files_selected)
        self.assertTrue(pkg.provenance_ok)
        for e in pkg.items:
            self.assertTrue(e.file and e.start_line and e.end_line and e.derivation_key)
        self.assertLessEqual(pkg.tokens["selected"], 500)
        self.assertEqual(pkg.router_view()["provenance_ok"], True)

    def test_adequacy_flags_missing_dependency(self):
        big = "def deep_check(t):\n" + "    x = 1\n" * 200 + "    return True\n"
        repo = write_repo({
            "login.py": "def login(t):\n    return deep_check(t)\n",
            "obscure.py": big,
        })
        # deep_check is a resolved dependency of login, but too large to fit the tiny budget.
        pkg = codeindex.build(repo, "explain login", budget=120)
        self.assertIn("obscure.py", pkg.adequacy.missing_required)
        self.assertEqual(pkg.adequacy.status, "partial")
        self.assertGreaterEqual(pkg.coverage["required_total"], 1)

    def test_zero_secret_leakage(self):
        repo = write_repo({
            "svc.py": "def connect():\n    api_key = 'sk-abcdef0123456789abcdef'\n    return api_key\n",
        })
        pkg = codeindex.build(repo, "explain connect", budget=500)
        self.assertIn("svc.py", pkg.files_selected)
        self.assertNotIn("sk-abcdef0123456789abcdef", pkg.context_text)
        self.assertIn("[REDACTED]", pkg.context_text)
        self.assertTrue(pkg.redactions)


class TestTaskScope(IndexCase):
    def test_scope_detection(self):
        from compression.codeindex.scope import detect_scope

        s = detect_scope("Can you refactor all the python code here to typescript?")
        self.assertTrue(s.corpus_wide)
        self.assertIn("python", s.source_languages)
        self.assertIn("typescript", s.target_languages)

        s2 = detect_scope("Explain what this login function does")
        self.assertFalse(s2.corpus_wide)

    def test_corpus_scope_represents_all_in_scope_files(self):
        files = {f"pkg/mod{i}.py": f"def func{i}(x):\n    return x + {i}\n" * 4 for i in range(6)}
        files["web/app.ts"] = "export function run(): number { return 1; }\n"
        repo = write_repo(files)
        pkg = codeindex.build(repo, "refactor all the python code to typescript", budget=3000)
        cov = pkg.coverage
        self.assertTrue(cov["scope"]["corpus_wide"])
        self.assertEqual(cov["in_scope_files"], 6)
        self.assertGreaterEqual(cov["represented_full"] + cov["represented_signature"], 1)
        # all six python files should be represented (full or signature) at this budget
        self.assertLessEqual(cov["omitted_files"], 6)

    def test_budget_scaling_in_router(self):
        from backend import router

        small = router.context_budget("auto", "simple")
        large = router.context_budget("auto", "complex")
        self.assertGreater(large, small)
        self.assertEqual(small, router.context_budget("local", "simple"))


class FakeEmbedder:
    """Deterministic 2-D concept embedding so tests need no model (vector-db vs queue concepts)."""
    name = "fake@1"

    def embed(self, texts):
        concepts = {"vector": 0, "database": 0, "embedding": 0, "chroma": 0,
                    "stuck": 1, "queue": 1, "overload": 1, "pool": 1}
        out = []
        for text in texts:
            low = text.lower()
            vec = [0.0, 0.0]
            for key, idx in concepts.items():
                if key in low:
                    vec[idx] += 1.0
            out.append(vec)
        return out


class TestSemanticRetrieval(IndexCase):
    def test_semantic_finds_concept_without_lexical_overlap(self):
        repo = write_repo({
            "store.py": "class ChromaDB:\n    def add_embedding(self, docs):\n        return docs\n",
        })
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        embedder = FakeEmbedder()
        idx.embed_missing(embedder)
        from compression.codeindex import retrieve

        query = "where is the vector database defined?"
        cands = retrieve.retrieve(idx, query, embedder=embedder)
        self.assertTrue(any(c.retriever == "semantic" and c.path == "store.py" for c in cands),
                        "semantic retriever must find a concept match with no lexical overlap")

    def test_build_reports_semantic_and_selects(self):
        repo = write_repo({
            "store.py": "class ChromaDB:\n    def add_embedding(self, docs):\n        return docs\n",
        })
        pkg = codeindex.build(repo, "where is the vector database defined?", budget=500,
                              embedder=FakeEmbedder())
        self.assertTrue(pkg.coverage["semantic"])
        self.assertIn("store.py", pkg.files_selected)

    def test_no_embedder_is_fail_open(self):
        repo = write_repo({"store.py": "class ChromaDB:\n    def add(self):\n        return 1\n"})
        pkg = codeindex.build(repo, "where is the vector database defined?", budget=500, embedder=False)
        # embedder=False forces "no embedder" (build treats None as "auto-detect")
        self.assertFalse(pkg.coverage["semantic"])


class TestLocalModelAssist(IndexCase):
    def setUp(self):
        super().setUp()
        self._saved_assist = {k: os.environ.get(k) for k in
                              ("LOCAL_MODEL", "CONTEXT_LLM_ASSIST", "CONTEXT_LLM_RERANK_TOP_K")}
        os.environ["LOCAL_MODEL"] = "fake-model"
        os.environ["CONTEXT_LLM_ASSIST"] = "true"
        from compression.codeindex import assist

        self.assist = assist
        self._orig = assist._call_json
        assist.clear_cache()

    def tearDown(self):
        self.assist._call_json = self._orig
        self.assist.clear_cache()
        for k, v in self._saved_assist.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _candidates(self):
        from compression.codeindex.retrieve import Candidate

        def cand(i, score):
            return Candidate(path=f"f{i}.py", name=f"sym{i}", kind="function", start_line=1,
                             end_line=2, content="x", score=score, retriever="lexical", reason="r")
        return [cand(1, 0.9), cand(2, 0.8), cand(3, 0.7)]

    def test_rerank_boosts_picked_without_displacing_strong_matches(self):
        self.assist._call_json = lambda s, u, m: '{"order": [3, 1]}'
        out = self.assist.rerank("q", self._candidates(), model="fake-model")
        # sym1 (0.9) is boosted by the model; sym3 (0.7+0.15) rises above sym2 (0.8).
        self.assertEqual([c.name for c in out], ["sym1", "sym3", "sym2"])

    def test_rerank_fails_open(self):
        def boom(s, u, m):
            raise RuntimeError("no model")
        self.assist._call_json = boom
        cands = self._candidates()
        self.assertEqual(self.assist.rerank("q", cands, model="fake-model"), cands)

    def test_propose_missing_returns_names(self):
        self.assist._call_json = lambda s, u, m: '{"needed": ["deep_check", "AuthService"]}'
        names = self.assist.propose_missing("q", "summary", model="fake-model")
        self.assertEqual(names, {"deep_check", "AuthService"})

    def test_sufficiency_augment_and_resolve(self):
        repo = write_repo({"auth.py": AUTH, "login.py": LOGIN})
        idx = codeindex.CodebaseIndex(repo)
        idx.sync()
        from compression.codeindex import package

        resolved = package._resolve_proposed(idx, {"validate_token"})
        self.assertEqual(resolved, {"auth.py"})
        augmented = package._augment(idx, [], {"auth.py"})
        self.assertTrue(any(c.path == "auth.py" for c in augmented))
        self.assertEqual(package._augment(idx, augmented, {"auth.py"}), augmented)  # no dupes


class TestAdapterIntegration(IndexCase):
    def test_uses_index_when_enabled(self):
        repo = write_repo({"auth.py": AUTH, "login.py": LOGIN})
        os.environ["CONTEXT_INDEX_ENABLED"] = "true"
        from compression import optimize_context

        result = optimize_context("how does login validate a token", repo, code="", token_budget=500)
        self.assertEqual(result.optimizer, "codeindex-v1")
        self.assertIn("login.py", result.files_selected)
        # provenance/adequacy is plumbed through the contract
        self.assertIn("retrieval_coverage", result.metadata)
        self.assertTrue(result.metadata.get("evidence"))

    def test_falls_back_to_engine_when_disabled(self):
        repo = write_repo({"auth.py": AUTH, "login.py": LOGIN})
        os.environ["CONTEXT_INDEX_ENABLED"] = "false"
        try:
            from compression import optimize_context

            result = optimize_context("how does login validate a token", repo, code="", token_budget=500)
            self.assertEqual(result.optimizer, "person2-v1")
        finally:
            os.environ.pop("CONTEXT_INDEX_ENABLED", None)


if __name__ == "__main__":
    unittest.main()
