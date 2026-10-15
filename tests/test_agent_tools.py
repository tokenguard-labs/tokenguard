"""Tests for the agent's file-editing tools (backend/agent_tools.py)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from backend.agent_tools import (  # noqa: E402
    ShadowWorkspace,
    ToolError,
    Workspace,
    append_file,
    move_file,
    read_multiple_files,
    search_replace_all,
    write_file,
)


class TempRepo:
    def __init__(self, files: dict):
        self.dir = tempfile.mkdtemp(prefix="agenttools_")
        for rel, content in files.items():
            full = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)

    @property
    def path(self) -> str:
        return self.dir


class TestNewTools(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo({
            "src/a.py": "def foo():\n    return 1\n",
            "src/b.py": "foo()\nfoo()\n",
        })
        self.ws = Workspace(self.repo.path)

    def test_write_then_append(self):
        write_file(self.ws, "src/new.py", "x = 1\n")
        append_file(self.ws, "src/new.py", "y = 2\n")
        self.assertEqual(self.ws.read("src/new.py"), "x = 1\ny = 2\n")

    def test_append_missing_file(self):
        with self.assertRaises(ToolError):
            append_file(self.ws, "src/nope.py", "x")

    def test_move_file(self):
        move_file(self.ws, "src/a.py", "src/moved/a.py")
        self.assertIsNone(self.ws.read("src/a.py"))
        self.assertEqual(self.ws.read("src/moved/a.py"), "def foo():\n    return 1\n")
        statuses = {c["path"]: c["status"] for c in self.ws.change_list()}
        self.assertEqual(statuses["src/moved/a.py"], "added")
        self.assertEqual(statuses["src/a.py"], "deleted")

    def test_move_to_existing_fails(self):
        with self.assertRaises(ToolError):
            move_file(self.ws, "src/a.py", "src/b.py")

    def test_search_replace_all_with_glob_and_groups(self):
        result = search_replace_all(self.ws, r"\bfoo\b", "bar", glob="*.py")
        self.assertIn("src/a.py", result)
        self.assertEqual(self.ws.read("src/a.py"), "def bar():\n    return 1\n")
        self.assertEqual(self.ws.read("src/b.py"), "bar()\nbar()\n")

    def test_search_replace_group_reference(self):
        search_replace_all(self.ws, r"foo\(\)", r"foo(1)", glob="b.py")
        self.assertEqual(self.ws.read("src/b.py"), "foo(1)\nfoo(1)\n")

    def test_search_replace_no_match(self):
        self.assertEqual(search_replace_all(self.ws, "zzz", "x"), "no replacements made (pattern not found)")

    def test_read_multiple_files(self):
        out = read_multiple_files(self.ws, ["src/a.py", "src/missing.py"])
        self.assertIn("src/a.py", out)
        self.assertIn("does not exist", out)

    def test_write_new_file_in_new_directory(self):
        write_file(self.ws, "newpkg/sub/mod.py", "value = 1\n")
        changes = {c["path"]: c for c in self.ws.change_list()}
        self.assertIn("newpkg/sub/mod.py", changes)
        self.assertEqual(changes["newpkg/sub/mod.py"]["status"], "added")
        self.assertIsNone(changes["newpkg/sub/mod.py"]["original"])
        self.assertEqual(self.ws.read("newpkg/sub/mod.py"), "value = 1\n")

    def test_sandbox_creates_new_nested_file(self):
        write_file(self.ws, "newpkg/sub/mod.py", "value = 1\n")
        shadow = ShadowWorkspace(self.ws)
        root = shadow.sync()
        try:
            created = os.path.join(root, "newpkg", "sub", "mod.py")
            self.assertTrue(os.path.isfile(created))
            with open(created, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "value = 1\n")
        finally:
            shadow.cleanup()

    def test_outside_workspace_rejected(self):
        with self.assertRaises(ToolError):
            write_file(self.ws, "../escape.py", "x")


if __name__ == "__main__":
    unittest.main()
