"""Agent mode: staged edits never touch the repo, commands run in a sandbox with them applied,
commands wait for approval, and secrets are masked for the cloud but restored in edits."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from backend import agent, agent_llm, agent_tools  # noqa: E402
from backend.agent_llm import AssistantTurn, ToolCall  # noqa: E402
from backend.agent_tools import (ShadowWorkspace, ToolError, Workspace, edit_file, read_file,  # noqa: E402
                                 replace_lines, search)


def make_repo(files):
    root = tempfile.mkdtemp(prefix="agent-test-")
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    return root


def disk(root, rel):
    with open(os.path.join(root, rel)) as f:
        return f.read()


def scripted(turns):
    """A fake model that plays back `turns` (lists of ToolCall), one per step."""
    queue = list(turns)

    async def step(messages, tools, route, **_):
        calls = queue.pop(0)
        return AssistantTurn(text="", tool_calls=calls, model="fake")
    return step


class TestWorkspace(unittest.TestCase):
    def setUp(self):
        self.root = make_repo({"app.py": "def add(a, b):\n    return a - b\n", "pkg/util.py": "X = 1\n"})
        self.ws = Workspace(self.root)

    def test_edit_is_staged_not_written(self):
        edit_file(self.ws, "app.py", "return a - b", "return a + b")
        self.assertEqual(disk(self.root, "app.py"), "def add(a, b):\n    return a - b\n")
        self.assertIn("return a + b", read_file(self.ws, "app.py"))
        [change] = self.ws.change_list()
        self.assertEqual((change["path"], change["status"], change["additions"], change["deletions"]),
                         ("app.py", "modified", 1, 1))

    def test_paths_outside_workspace_are_rejected(self):
        with self.assertRaises(ToolError):
            read_file(self.ws, "../etc/passwd")
        with self.assertRaises(ToolError):
            read_file(self.ws, "/etc/passwd")

    def test_edit_needs_a_unique_exact_match(self):
        with self.assertRaises(ToolError):
            edit_file(self.ws, "app.py", "return a * b", "x")
        self.ws.stage("dup.py", "a\na\n")
        with self.assertRaises(ToolError):
            edit_file(self.ws, "dup.py", "a", "b")
        edit_file(self.ws, "dup.py", "a", "b", replace_all=True)
        self.assertEqual(self.ws.read("dup.py"), "b\nb\n")

    def test_reverted_edit_is_not_a_change(self):
        edit_file(self.ws, "app.py", "a - b", "a + b")
        edit_file(self.ws, "app.py", "a + b", "a - b")
        self.assertEqual(self.ws.change_list(), [])

    def test_search_sees_staged_files(self):
        self.ws.stage("new.py", "NEEDLE = True\n")
        self.assertIn("new.py:1:", search(self.ws, "NEEDLE"))

    def test_sandbox_runs_with_staged_edits_and_without_keys(self):
        edit_file(self.ws, "app.py", "a - b", "a + b")
        shadow = ShadowWorkspace(self.ws)
        os.environ["FAKE_PROVIDER_API_KEY"] = "sk-should-not-leak"
        try:
            code, out = asyncio.run(shadow.run(
                f'{sys.executable} -c "import os, app; print(app.add(2, 3), os.environ.get(\'FAKE_PROVIDER_API_KEY\'))"'))
        finally:
            del os.environ["FAKE_PROVIDER_API_KEY"]
            shadow.cleanup()
        self.assertEqual(code, 0, out)
        self.assertIn("5 None", out)
        self.assertIn("a - b", disk(self.root, "app.py"))

    def test_sandbox_uses_the_repo_venv_when_not_activated(self):
        # The backend runs as `.venv/bin/python -m uvicorn`, so PATH has no venv on it.
        import venv
        venv.create(os.path.join(self.root, ".venv"), with_pip=False, symlinks=(os.name != "nt"))
        shadow = ShadowWorkspace(self.ws)
        saved = {k: os.environ.get(k) for k in ("PATH", "VIRTUAL_ENV")}
        os.environ["PATH"] = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                                             if os.path.dirname(sys.executable) != p)
        os.environ.pop("VIRTUAL_ENV", None)
        try:
            code, out = asyncio.run(shadow.run('python -c "import sys; print(sys.prefix)"'))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shadow.cleanup()
        self.assertEqual(code, 0, out)
        self.assertEqual(os.path.realpath(out.strip()), os.path.realpath(os.path.join(self.root, ".venv")))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_sandbox_copies_gitignored_files(self):
        root = make_repo({"app.py": "x = 1\n", ".gitignore": "build/\nlocal.cfg\n",
                          "build/out.txt": "built\n", "local.cfg": "debug = true\n"})
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        shadow = ShadowWorkspace(Workspace(root))
        try:
            code, out = asyncio.run(shadow.run("cat build/out.txt local.cfg"))
        finally:
            shadow.cleanup()
        self.assertEqual(code, 0, out)
        self.assertIn("built", out)
        self.assertIn("debug = true", out)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_sensitive_files_are_detected(self):
        root = make_repo({".gitignore": "*.local\n", ".env": "X=1\n", ".env.example": "X=\n",
                          "deploy/id_rsa": "key\n", "settings.local": "API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n",
                          "plain.local": "debug = true\n", "app.py": "x = 1\n"})
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        found = ShadowWorkspace(Workspace(root)).sensitive_files()
        self.assertEqual(sorted(found), [".env", "deploy/id_rsa", "settings.local"])


class TestAgentRun(unittest.TestCase):
    def setUp(self):
        self.root = make_repo({"app.py": "def add(a, b):\n    return a - b\n"})

    def _run(self, turns, *, approve=True, route="local"):
        events = []
        holder = {}

        def emit(event, data):
            events.append((event, data))
            if event == "approval":
                # Answer like the sidebar would, once the run is waiting.
                asyncio.get_running_loop().call_soon(
                    holder["run"].resolve_approval, data["request_id"], approve)

        run = agent.AgentRun("fix add", self.root, route, emit, step_fn=scripted(turns))
        holder["run"] = run
        return asyncio.run(run.run()), events

    def test_explore_edit_verify_finish(self):
        check = f'{sys.executable} -c "import app; assert app.add(2, 3) == 5"'
        result, events = self._run([
            [ToolCall("1", "read_file", {"path": "app.py"})],
            [ToolCall("2", "edit_file", {"path": "app.py", "old_string": "a - b", "new_string": "a + b"})],
            [ToolCall("3", "run_command", {"command": check})],
            [ToolCall("4", "finish", {"summary": "Fixed add."})],
        ])
        self.assertEqual(result["stopped_reason"], "finished")
        self.assertEqual(result["summary"], "Fixed add.")
        self.assertEqual([c["path"] for c in result["changes"]], ["app.py"])
        self.assertEqual(result["commands"][0]["exit_code"], 0)
        self.assertIn("a - b", disk(self.root, "app.py"))  # nothing written until the user accepts
        kinds = [e for e, _ in events]
        self.assertEqual(kinds[0], "start")
        self.assertIn("approval", kinds)
        self.assertIn("changes", kinds)

    def test_declined_command_is_not_run(self):
        result, events = self._run([
            [ToolCall("1", "run_command", {"command": "touch should_not_exist"})],
            [ToolCall("2", "finish", {"summary": "ok"})],
        ], approve=False)
        self.assertTrue(result["commands"][0]["declined"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "should_not_exist")))
        output = [d["output"] for e, d in events if e == "tool_result"][0]
        self.assertIn("declined", output)

    def test_sensitive_files_left_out_when_the_user_declines(self):
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write("SECRET=1\n")
        events = []
        holder = {}

        def emit(event, data):
            events.append((event, data))
            if event == "approval":
                approve = data["kind"] == "command"  # run the command, keep the secrets out
                asyncio.get_running_loop().call_soon(holder["run"].resolve_approval, data["request_id"], approve)

        check = f'{sys.executable} -c "import os; print(\'env present\' if os.path.exists(\'.env\') else \'env absent\')"'
        run = agent.AgentRun("check", self.root, "local", emit, step_fn=scripted([
            [ToolCall("1", "run_command", {"command": check})],
            [ToolCall("2", "run_command", {"command": check})],
            [ToolCall("3", "finish", {"summary": "ok"})],
            [ToolCall("4", "finish", {"summary": "ok"})],  # again, after the nothing-staged nudge
        ]))
        holder["run"] = run
        asyncio.run(run.run())
        asks = [d for e, d in events if e == "approval" and d["kind"] == "sensitive_files"]
        self.assertEqual([a["files"] for a in asks], [[".env"]])  # asked once per run
        outputs = [d["output"] for e, d in events if e == "tool_result"]
        self.assertIn("env absent", outputs[0])
        self.assertIn("leave these files out", outputs[0])
        self.assertIn("env absent", outputs[1])

    def test_tool_errors_go_back_to_the_model(self):
        result, events = self._run([
            [ToolCall("1", "read_file", {})],
            [ToolCall("2", "nope", {})],
            [ToolCall("3", "finish", {"summary": "done"})],
        ])
        oks = [d["ok"] for e, d in events if e == "tool_result"]
        self.assertEqual(oks, [False, False])
        self.assertEqual(result["stopped_reason"], "finished")

    def test_plain_answer_ends_the_run_after_nudges(self):
        async def answer(messages, tools, route, **_):
            return AssistantTurn(text="Nothing to change.", model="fake")
        run = agent.AgentRun("q", self.root, "local", lambda *_: None, step_fn=answer)
        result = asyncio.run(run.run())
        self.assertEqual((result["stopped_reason"], result["summary"]), ("answered", "Nothing to change."))
        self.assertEqual(result["steps"], 1 + agent.MAX_CONTINUE_NUDGES)

    def test_announced_step_is_continued_not_taken_as_the_answer(self):
        turns = [AssistantTurn("Let's fix add now."),
                 AssistantTurn("", [ToolCall("1", "edit_file", {"path": "app.py", "old_string": "a - b",
                                                                "new_string": "a + b"})]),
                 AssistantTurn("", [ToolCall("2", "finish", {"summary": "Fixed add."})])]

        async def step(messages, tools, route, **_):
            return turns.pop(0)
        result = asyncio.run(agent.AgentRun("fix add", self.root, "local", lambda *_: None, step_fn=step).run())
        self.assertEqual((result["stopped_reason"], result["summary"]), ("finished", "Fixed add."))
        self.assertEqual(len(result["changes"]), 1)

    def test_list_files_on_a_file(self):
        self.assertIn("a file", agent_tools.list_files(Workspace(self.root), "app.py"))


class TestSmallModelRobustness(unittest.TestCase):
    def setUp(self):
        self.root = make_repo({"heap.py": "def f(i):\n    return g(j)\n"})

    def test_malformed_tool_call_is_retried_then_reported(self):
        broken = '{"name": "write_file", "arguments": {"path": "heap.py", "content": "say "hi""}}'
        turns = [AssistantTurn(text=broken), AssistantTurn(text=broken), AssistantTurn(text=broken)]
        sent = []

        async def step(messages, tools, route, **_):
            sent.append(messages[-1]["content"])
            return turns.pop(0)
        events = []
        result = asyncio.run(agent.AgentRun("fix", self.root, "local", lambda e, d: events.append(e),
                                            step_fn=step).run())
        self.assertEqual(result["stopped_reason"], "invalid_tool_calls")
        self.assertNotIn('"write_file"', result["summary"])  # the raw JSON is not shown as the answer
        self.assertEqual(sent.count(agent.BAD_CALL_NUDGE), 2)
        self.assertEqual(events.count("notice"), 2)

    def test_retry_can_recover(self):
        turns = [AssistantTurn(text='{"name": "edit_file", "arguments": broken'),
                 AssistantTurn("", [ToolCall("1", "edit_file", {"path": "heap.py", "old_string": "g(j)",
                                                                "new_string": "g(i)"})]),
                 AssistantTurn("", [ToolCall("2", "finish", {"summary": "fixed"})])]

        async def step(messages, tools, route, **_):
            return turns.pop(0)
        result = asyncio.run(agent.AgentRun("fix", self.root, "local", lambda *_: None, step_fn=step).run())
        self.assertEqual(result["stopped_reason"], "finished")
        self.assertIn("g(i)", result["changes"][0]["proposed"])

    def test_raw_newlines_in_printed_json_are_tolerated(self):
        text = '{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 1\n\ty = 2\n"}}'
        text = text.replace("\\n", "\n").replace("\\t", "\t")  # literal newline/tab inside the string
        [call], _ = agent_llm._text_tool_calls(text, {"write_file"})
        self.assertEqual(call.arguments["content"], "x = 1\n\ty = 2\n")

    def test_edit_made_by_a_command_is_staged(self):
        sed = "sed -i 's/g(j)/g(i)/' heap.py" if sys.platform != "darwin" else "sed -i '' 's/g(j)/g(i)/' heap.py"
        turns = [AssistantTurn("", [ToolCall("1", "run_command", {"command": sed})]),
                 AssistantTurn("", [ToolCall("2", "finish", {"summary": "fixed"})])]

        async def step(messages, tools, route, **_):
            return turns.pop(0)
        run = agent.AgentRun("fix", self.root, "local", lambda *_: None, step_fn=step)
        run.auto_approve = True
        result = asyncio.run(run.run())
        self.assertEqual(result["stopped_reason"], "finished")
        self.assertIn("g(i)", result["changes"][0]["proposed"])
        with open(os.path.join(self.root, "heap.py")) as f:
            self.assertIn("g(j)", f.read())  # the repo itself is untouched

    def test_finish_after_commands_without_staging_is_nudged_once(self):
        turns = [AssistantTurn("", [ToolCall("1", "run_command", {"command": "echo tried the fix"})]),
                 AssistantTurn("", [ToolCall("2", "finish", {"summary": "fixed"})]),
                 AssistantTurn("", [ToolCall("3", "edit_file", {"path": "heap.py", "old_string": "g(j)",
                                                                "new_string": "g(i)"})]),
                 AssistantTurn("", [ToolCall("4", "finish", {"summary": "fixed"})])]
        sent = []

        async def step(messages, tools, route, **_):
            sent.append(messages[-1]["content"])
            return turns.pop(0)
        run = agent.AgentRun("fix", self.root, "local", lambda *_: None, step_fn=step)
        run.auto_approve = True
        result = asyncio.run(run.run())
        self.assertEqual(result["stopped_reason"], "finished")
        self.assertIn(agent.UNSTAGED_NUDGE, sent)
        self.assertEqual(len(result["changes"]), 1)

    def test_ambiguous_edit_lists_the_candidate_lines(self):
        ws = Workspace(self.root)
        with self.assertRaises(ToolError) as cm:
            edit_file(ws, "heap.py", "(", "[")
        self.assertIn("line 1: def f(i):", str(cm.exception))
        self.assertIn("replace_lines", str(cm.exception))

    def test_replace_lines(self):
        ws = Workspace(self.root)
        replace_lines(ws, "heap.py", 2, 2, "    return g(i)\n")
        self.assertEqual(ws.read("heap.py"), "def f(i):\n    return g(i)\n")
        replace_lines(ws, "heap.py", 2, 2, "")
        self.assertEqual(ws.read("heap.py"), "def f(i):\n")
        with self.assertRaises(ToolError):
            replace_lines(ws, "heap.py", 3, 5, "x")

    def test_dropped_indentation_is_restored(self):
        root = make_repo({"heap.py": "class H:\n    def f(self, i):\n        l = 2 * i\n        r = 2 * i + 1\n"})
        ws = Workspace(root)
        replace_lines(ws, "heap.py", 3, 4, "l = self._left(i)\nr = self._right(i)")
        self.assertEqual(ws.read("heap.py").split("\n")[2:4], ["        l = self._left(i)", "        r = self._right(i)"])
        ws = Workspace(root)
        replace_lines(ws, "heap.py", 3, 4, "l = self._left(i)\n        r = self._right(i)")  # first line only
        self.assertEqual(ws.read("heap.py").split("\n")[2:4], ["        l = self._left(i)", "        r = self._right(i)"])
        ws = Workspace(root)
        edit_file(ws, "heap.py", "l = 2 * i\nr = 2 * i + 1", "l = self._left(i)\nr = self._right(i)")
        self.assertEqual(ws.read("heap.py").split("\n")[2:4], ["        l = self._left(i)", "        r = self._right(i)"])
        ws = Workspace(root)
        edit_file(ws, "heap.py", "l = 2 * i", "l = self._left(i)\nassert l")
        self.assertEqual(ws.read("heap.py").split("\n")[2:4], ["        l = self._left(i)", "        assert l"])
        ws = Workspace(root)
        replace_lines(ws, "heap.py", 2, 2, "    def g(self, i):")  # indentation given: kept as is
        self.assertEqual(ws.read("heap.py").split("\n")[1], "    def g(self, i):")

    def test_described_edit_is_staged(self):
        root = make_repo({"heap.py": "class H:\n    def f(self, i):\n        l = 2 * i\n        r = 2 * i + 1\n"})
        turns = [AssistantTurn("", [ToolCall("1", "read_file", {"path": "heap.py"})]),
                 AssistantTurn("Let's use replace_lines to correct lines 3 and 4:\n```python\n"
                               "l = self._left(i)\nr = self._right(i)\n```\nLet's apply this change."),
                 AssistantTurn("", [ToolCall("2", "finish", {"summary": "Fixed."})])]

        async def step(messages, tools, route, **_):
            return turns.pop(0)
        result = asyncio.run(agent.AgentRun("fix", root, "local", lambda *_: None, step_fn=step).run())
        self.assertEqual(result["stopped_reason"], "finished")
        [change] = result["changes"]
        self.assertIn("        l = self._left(i)\n        r = self._right(i)", change["proposed"])
        ws = Workspace(root)
        self.assertIsNone(agent.described_edit("Change lines 3-4:\n```\nx = 1\n```", ws, "heap.py"))  # 1 line for 2
        self.assertIsNone(agent.described_edit("```\nx = 1\n```", ws, "heap.py"))  # no line numbers

    def test_diff_shown_with_finish_is_staged(self):
        root = make_repo({"smallest.py": "class MinHeap:\n    def _sift_down(self, i):\n        smallest = i\n"
                                         "        left = self._left_child(j)\n        right = self._right_child(i)\n"})
        reply = ("Replace j with i.\n```diff\n--- a/smallest.py\n+++ b/smallest.py\n@@ -22,7 +22,7 @@ class MinHeap:\n"
                 "     def _sift_down(self, i):\n         smallest = i\n-        left = self._left_child(j)\n"
                 "+        left = self._left_child(i)\n         right = self._right_child(i)\n```\nDone.")
        turns = [AssistantTurn("", [ToolCall("1", "read_file", {"path": "smallest.py"})]),
                 AssistantTurn(reply, [ToolCall("2", "finish", {"summary": "Fixed the typo."})])]

        async def step(messages, tools, route, **_):
            return turns.pop(0)
        result = asyncio.run(agent.AgentRun("fix", root, "local", lambda *_: None, step_fn=step).run())
        self.assertEqual(result["stopped_reason"], "finished")
        [change] = result["changes"]
        self.assertIn("left = self._left_child(i)", change["proposed"])
        self.assertIn("_left_child(j)", disk(root, "smallest.py"))
        # Already applied: the same diff is not staged again.
        ws = Workspace(root)
        ws.stage("smallest.py", change["proposed"])
        self.assertEqual(agent.diff_edits(reply, ws, None), [])

    def test_finish_without_staged_change_is_sent_back_once(self):
        turns = [AssistantTurn("Change it to:\n```python\nreturn g(i)\n```", [ToolCall("1", "finish", {})]),
                 AssistantTurn("", [ToolCall("2", "edit_file", {"path": "heap.py", "old_string": "g(j)",
                                                                "new_string": "g(i)"})]),
                 AssistantTurn("", [ToolCall("3", "finish", {"summary": "fixed"})])]
        replies = []

        async def step(messages, tools, route, **_):
            replies.append(messages[-1]["content"])
            return turns.pop(0)
        result = asyncio.run(agent.AgentRun("fix", self.root, "local", lambda *_: None, step_fn=step).run())
        self.assertEqual(replies[1], agent.UNSTAGED_NUDGE)
        self.assertEqual((result["stopped_reason"], len(result["changes"])), ("finished", 1))

    def test_repeated_failing_call_stops_with_a_message(self):
        same = ToolCall("1", "edit_file", {"path": "heap.py", "old_string": "nope", "new_string": "x"})

        async def step(messages, tools, route, **_):
            return AssistantTurn("", [same])
        result = asyncio.run(agent.AgentRun("fix", self.root, "local", lambda *_: None, step_fn=step).run())
        self.assertEqual(result["stopped_reason"], "gave_up")
        self.assertIn("got stuck", result["summary"])
        self.assertEqual(result["steps"], 4)  # one first failure + MAX_REPEATED_FAILURES repeats

    def test_empty_replies_are_nudged_then_reported(self):
        async def step(messages, tools, route, **_):
            return AssistantTurn("\n")
        result = asyncio.run(agent.AgentRun("fix", self.root, "local", lambda *_: None, step_fn=step).run())
        self.assertEqual(result["stopped_reason"], "gave_up")
        self.assertTrue(result["summary"].strip())

    def test_finish_is_refused_while_code_does_not_parse(self):
        turns = [
            AssistantTurn("", [ToolCall("1", "replace_lines", {"path": "heap.py", "start_line": 2, "end_line": 2,
                                                               "new_text": "    return g(i"})]),
            AssistantTurn("", [ToolCall("2", "finish", {"summary": "done"})]),
            AssistantTurn("", [ToolCall("3", "replace_lines", {"path": "heap.py", "start_line": 2, "end_line": 2,
                                                               "new_text": "    return g(i)"})]),
            AssistantTurn("", [ToolCall("4", "finish", {"summary": "fixed"})]),
        ]
        replies = []

        async def step(messages, tools, route, **_):
            replies.append(messages[-1]["content"])
            return turns.pop(0)
        result = asyncio.run(agent.AgentRun("fix", self.root, "local", lambda *_: None, step_fn=step).run())
        self.assertIn("SyntaxError", replies[2])
        self.assertEqual((result["stopped_reason"], result["summary"]), ("finished", "fixed"))

    def test_mentioned_files_are_included_once(self):
        seen = []

        async def step(messages, tools, route, **_):
            seen.append(messages[1]["content"])
            return AssistantTurn("", [ToolCall("1", "finish", {"summary": "ok"})])
        asyncio.run(agent.AgentRun("fix heap.py", self.root, "local", lambda *_: None,
                                   mentioned_files=["heap.py", "missing.py"], step_fn=step).run())
        self.assertEqual(seen[0].count("return g(j)"), 1)
        self.assertIn("Current content of heap.py", seen[0])

    def test_small_model_warning(self):
        self.assertIsNotNone(agent.small_model_warning("qwen2.5-coder:1.5b"))
        self.assertIsNotNone(agent.small_model_warning("llama3.2:3b"))
        self.assertIsNone(agent.small_model_warning("qwen2.5-coder:7b"))


class TestPromptPaths(unittest.TestCase):
    def test_traceback_keeps_file_names_and_attaches_once(self):
        from backend import context
        root = make_repo({"smallest.py": "print('hi')\n"})
        path = os.path.join(root, "smallest.py")
        prompt = f'python {path}\n  File "{path}", line 1\n  File "{path}", line 2\nsolve this'
        res = context.resolve_request(prompt, root, "")
        self.assertIn('File "smallest.py", line 1', res.prompt)
        self.assertEqual(res.code.count("print('hi')"), 1)


class TestSecrets(unittest.TestCase):
    def test_cloud_transcript_is_masked_and_edits_unmasked(self):
        root = make_repo({"settings.py": 'API_KEY = "abc123secretvalue"\nDEBUG = False\n'})
        seen = []

        async def step(messages, tools, route, **_):
            seen.append(messages[-1]["content"])
            if len(seen) == 1:
                return AssistantTurn("", [ToolCall("1", "read_file", {"path": "settings.py"})])
            if len(seen) == 2:
                token = seen[-1].split('API_KEY = "')[1].split('"')[0]
                return AssistantTurn("", [ToolCall("2", "edit_file", {
                    "path": "settings.py", "old_string": f'API_KEY = "{token}"\nDEBUG = False',
                    "new_string": f'API_KEY = "{token}"\nDEBUG = True'})])
            return AssistantTurn("", [ToolCall("3", "finish", {"summary": "ok"})])

        run = agent.AgentRun("enable debug", root, "cloud", lambda *_: None, step_fn=step)
        result = asyncio.run(run.run())
        self.assertNotIn("abc123secretvalue", seen[1])
        self.assertIn("__SECRET_1__", seen[1])
        self.assertEqual(result["changes"][0]["proposed"], 'API_KEY = "abc123secretvalue"\nDEBUG = True\n')
        self.assertEqual(result["secrets_masked"], 1)


class TestAdapters(unittest.TestCase):
    def test_anthropic_merges_tool_results(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            AssistantTurn("looking", [ToolCall("a", "read_file", {"path": "x"}),
                                      ToolCall("b", "read_file", {"path": "y"})]).message(),
            {"role": "tool", "tool_call_id": "a", "content": "X"},
            {"role": "tool", "tool_call_id": "b", "content": "Y"},
        ]
        system, out = agent_llm.to_anthropic(msgs)
        self.assertEqual(system, "sys")
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user"])
        self.assertEqual([b["tool_use_id"] for b in out[2]["content"]], ["a", "b"])
        self.assertEqual(out[1]["content"][1]["input"], {"path": "x"})

    def test_ollama_names_tool_results(self):
        msgs = [AssistantTurn("", [ToolCall("a", "search", {"pattern": "p"})]).message(),
                {"role": "tool", "tool_call_id": "a", "content": "hits"}]
        out = agent_llm.to_ollama(msgs)
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], {"pattern": "p"})
        self.assertEqual(out[1]["tool_name"], "search")

    def test_json_printed_tool_calls_are_recovered(self):
        text = 'I will read it.\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
        [call], rest = agent_llm._text_tool_calls(text, {"read_file"})
        self.assertEqual((call.name, call.arguments), ("read_file", {"path": "a.py"}))
        self.assertEqual(rest, "I will read it.")
        self.assertEqual(agent_llm._text_tool_calls('{"name": "rm", "arguments": {}}', {"read_file"})[0], [])


if __name__ == "__main__":
    unittest.main()
