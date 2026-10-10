"""Tests for the refined pipeline: chunker, import graph, gitignore, engine v2."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "compression"))

from context_engine import ContextEngine, ContextRequest  # noqa: E402
from context_engine.chunker import chunk_file  # noqa: E402
from context_engine.config import ContextConfig  # noqa: E402
from context_engine.imports import build_import_graph, extract_imports, resolve  # noqa: E402
from context_engine.walker import FileInfo, walk_repo  # noqa: E402


class TempRepo:
    def __init__(self, files: dict):
        self._dir = tempfile.mkdtemp(prefix="p2ref_")
        for rel, content in files.items():
            full = os.path.join(self._dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)

    @property
    def path(self):
        return self._dir


def _info(rel: str, text: str) -> FileInfo:
    return FileInfo(
        path=rel,
        rel_path=rel,
        size=len(text),
        mtime=0.0,
        text=text,
        line_count=text.count("\n") + 1,
    )


class TestChunker(unittest.TestCase):
    def test_python_ast_symbols(self):
        src = (
            "import os\n\n\n"
            "def alpha():\n    return 1\n\n\n"
            "class Beta:\n    def method(self):\n        return 2\n"
        )
        chunks = chunk_file(_info("a.py", src), ContextConfig())
        symbols = {c.symbol for c in chunks}
        self.assertIn("alpha", symbols)
        self.assertIn("Beta", symbols)

    def test_ts_symbols_after_comment(self):
        src = (
            "import { x } from './x';\n\n"
            "// does a thing\nexport function doThing() {\n  return x;\n}\n\n"
            "export const CONFIG = { a: 1 };\n"
        )
        chunks = chunk_file(_info("a.ts", src), ContextConfig())
        symbols = {c.symbol for c in chunks}
        self.assertIn("doThing", symbols)
        self.assertIn("CONFIG", symbols)

    def test_chunks_cover_file(self):
        src = "const a = () => {\n  return 1;\n};\n\nconst b = () => {\n  return 2;\n};\n"
        chunks = chunk_file(_info("a.ts", src), ContextConfig())
        covered = set()
        for c in chunks:
            covered.update(range(c.start_line, c.end_line + 1))
        self.assertTrue({1, 2, 3, 5, 6, 7}.issubset(covered))


class TestImports(unittest.TestCase):
    def test_ts_extract_and_resolve(self):
        repo = TempRepo(
            {
                "src/auth/login.ts": "import { v } from './auth';\nimport { u } from '../services/userService';\n",
                "src/auth/auth.ts": "export const v = 1;\n",
                "src/services/userService.ts": "export const u = 1;\n",
            }
        )
        files = walk_repo(repo.path, ContextConfig())
        graph = build_import_graph(files)
        self.assertEqual(
            graph["src/auth/login.ts"],
            {"src/auth/auth.ts", "src/services/userService.ts"},
        )

    def test_python_relative_resolve(self):
        repo = TempRepo(
            {
                "pkg/__init__.py": "",
                "pkg/auth.py": "def validate():\n    return True\n",
                "pkg/login.py": "from .auth import validate\n",
            }
        )
        files = walk_repo(repo.path, ContextConfig())
        graph = build_import_graph(files)
        self.assertIn("pkg/auth.py", graph["pkg/login.py"])

    def test_specs_extracted(self):
        info = _info("a.ts", "import x from 'react';\nconst y = require('./y');\n")
        self.assertEqual(set(extract_imports(info)), {"react", "./y"})


class TestGitignore(unittest.TestCase):
    def test_gitignore_excludes_paths(self):
        repo = TempRepo(
            {
                ".gitignore": "src/generated/\n*.secret.ts\n",
                "src/app.ts": "export const a = 1;\n",
                "src/generated/thing.ts": "export const g = 1;\n",
                "src/token.secret.ts": "export const s = 1;\n",
            }
        )
        rels = {f.rel_path for f in walk_repo(repo.path, ContextConfig())}
        self.assertIn("src/app.ts", rels)
        self.assertNotIn("src/generated/thing.ts", rels)
        self.assertNotIn("src/token.secret.ts", rels)


class TestEngineRefined(unittest.TestCase):
    def _auth_repo(self):
        return TempRepo(
            {
                "src/auth/login.ts": (
                    "import { validateCredentials } from './auth';\n\n"
                    "export function login() {\n  return validateCredentials();\n}\n"
                ),
                "src/auth/auth.ts": "export function validateCredentials() {\n  return true;\n}\n",
                "src/unrelated/noise.ts": "export function noise() {\n  return 0;\n}\n",
            }
        )

    def test_import_neighbor_included(self):
        repo = self._auth_repo()
        result = ContextEngine().build(
            ContextRequest(query="login", repo_path=repo.path, top_k=1, chunking="symbol")
        )
        self.assertIn("src/auth/login.ts", result.selected_files)
        self.assertIn("src/auth/auth.ts", result.selected_files)

    def test_expansion_can_be_disabled(self):
        repo = self._auth_repo()
        result = ContextEngine().build(
            ContextRequest(
                query="login", repo_path=repo.path, top_k=1, chunking="symbol", expand_deps=False
            )
        )
        self.assertIn("src/auth/login.ts", result.selected_files)
        self.assertNotIn("src/auth/auth.ts", result.selected_files)

    def test_symbol_mode_respects_hard_cap(self):
        repo = self._auth_repo()
        engine = ContextEngine()
        result = engine.build(
            ContextRequest(query="login", repo_path=repo.path, top_k=3, max_tokens=30)
        )
        self.assertLessEqual(engine.counter.count(result.context_text), 30)

    def test_deterministic_refined(self):
        repo = self._auth_repo()
        engine = ContextEngine()
        req = ContextRequest(query="login", repo_path=repo.path, top_k=2)

        def stable(r):
            d = r.to_dict()
            d["stats"].pop("duration_ms", None)
            return d

        self.assertEqual(stable(engine.build(req)), stable(engine.build(req)))


if __name__ == "__main__":
    unittest.main()
