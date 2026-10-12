"""Agent mode: an LLM that explores the repo, edits it and verifies its edits, in a tool loop.

Flow (POST /agent/stream): the model is given the task and a set of tools. Each step it reads,
searches, edits or runs something; results go back into the transcript until it calls `finish`
(or runs out of steps). Edits are staged in agent_tools.Workspace and returned as proposed
changes; the editor shows the diffs and the user accepts or rejects each file. Commands run in a
sandbox copy of the repo with the staged edits applied, and only after the user approves them
(POST /agent/approve), unless AGENT_AUTO_APPROVE_COMMANDS is set.

On the cloud route, secrets found in anything sent to the model are replaced with placeholders
(SecretVault) and restored in the model's edits, so a masked secret is never written back masked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from compression.context_engine.redact import RULES as SECRET_RULES

from . import agent_llm, config, llm
from .agent_llm import ToolCall
from .agent_tools import (ShadowWorkspace, ToolError, Workspace, _loose_match, delete_file, edit_file,
                          list_files, read_file, replace_lines, search, syntax_problems, write_file)
from .tokens import count_tokens

log = logging.getLogger("localpilot.agent")

Emit = Callable[[str, Dict[str, Any]], None]

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_files",
        "description": "List a directory of the workspace (folders end with /). recursive=true lists every file below it.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory relative to the workspace root. Default '.'"},
            "recursive": {"type": "boolean"},
        }},
    },
    {
        "name": "read_file",
        "description": "Read a file with line numbers (staged edits included). Long files are paged: use start_line/end_line.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        }, "required": ["path"]},
    },
    {
        "name": "search",
        "description": "Regex search over file contents in the workspace. Returns file:line: text.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "path": {"type": "string", "description": "Directory or file to search. Default '.'"},
            "glob": {"type": "string", "description": "Only files matching this glob, e.g. '*.py'"},
            "ignore_case": {"type": "boolean"},
        }, "required": ["pattern"]},
    },
    {
        "name": "edit_file",
        "description": ("Replace an exact string in a file. old_string must match the file exactly (copy it from "
                        "read_file without the line-number prefix) and be unique unless replace_all is true. "
                        "Prefer this for changes to existing files."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, "required": ["path", "old_string", "new_string"]},
    },
    {
        "name": "replace_lines",
        "description": ("Replace lines start_line..end_line (1-based, inclusive, as shown by read_file or a traceback) "
                        "with new_text. Include the indentation in new_text. Empty new_text deletes the lines."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "new_text": {"type": "string"},
        }, "required": ["path", "start_line", "end_line", "new_text"]},
    },
    {
        "name": "write_file",
        "description": "Create a file, or replace a whole file's content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    },
    {
        "name": "delete_file",
        "description": "Delete a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "run_command",
        "description": ("Run a shell command (tests, build, linter, a quick script) in a sandbox copy of the "
                        "workspace with your edits applied. The user must approve it. Returns exit code and output. "
                        "Make code changes with the edit tools, not with commands. "
                        "No network or interactive commands; the sandbox has no .git folder."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "Directory relative to the workspace root. Default '.'"},
            "timeout_s": {"type": "integer"},
        }, "required": ["command"]},
    },
    {
        "name": "finish",
        "description": ("Finish the task. summary: what you changed and why, and how you verified it "
                        "(markdown, concise). Call this once everything is done."),
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    },
]

_FILE_TOOLS = {
    "list_files": list_files, "read_file": read_file, "search": search,
    "edit_file": edit_file, "replace_lines": replace_lines, "write_file": write_file, "delete_file": delete_file,
}
_MUTATING = {"edit_file", "replace_lines", "write_file", "delete_file"}
_SCHEMAS = {t["name"]: t["parameters"] for t in TOOLS}

SYSTEM_PROMPT = """You are LocalPilot Agent, an autonomous coding agent working in the user's repository.
Workspace root: {root}
Platform: {platform}

Work through the task with the tools:
1. Explore first. Find and read the relevant code (search, list_files, read_file) before changing anything. Never guess what a file contains. In an error traceback the failing line is in the LAST frame; the earlier frames are only callers.
2. Make focused, complete changes with edit_file (preferred for existing files), replace_lines (when you know the line numbers) or write_file (new files). Match the surrounding style. Update callers, imports and tests that your change affects.
3. Verify. When the project has tests, a build or a type checker, run the relevant ones with run_command; you can also run a small script. If something fails because of your change, fix it and run it again. Skip verification only when there is nothing meaningful to run.
4. Call finish with a short summary: what changed, why, and how you verified it.

Your edits are NOT written to the user's files. They are staged and shown to the user as diffs to accept or reject, so make every edit you intend to propose with a tool call (code you only write in a message is not applied), keeping the file's indentation. Commands run in a sandbox copy that includes your staged edits. The user may decline a command; then continue without it.
Keep your messages between tool calls short (one or two sentences about what you are doing next)."""

TOP_LEVEL_NOTE = "\n\nTop level of the workspace:\n{listing}"

# Sent back when the model printed something that tried to be a tool call but was not valid.
BAD_CALL_NUDGE = ("Your last message looked like a tool call but could not be parsed, so nothing happened. "
                  "Call the tool through the tool-calling interface with valid JSON arguments. To change a few "
                  "lines, use edit_file with a short old_string copied exactly from the file, not write_file "
                  "with the whole file.")
MAX_BAD_CALL_RETRIES = 2

# Sent when the model replies without a tool call. Local models often describe the next step
# ("Let's add the method…") and stop; the run only ends through finish.
CONTINUE_NUDGE = ("You replied without a tool call, so nothing happened. If you described a next step, do it "
                  "now with a tool call. If the task is complete, call finish with your answer or a summary of "
                  "the changes. If an edit failed, read the error: it lists the matching lines.")
MAX_CONTINUE_NUDGES = 3
# Prepended to the result of a call identical to one that already failed.
REPEAT_NOTE = ("You already made this exact call and it failed the same way. Do something different: "
               "follow the error message, or use replace_lines with the line numbers it gives.\n")
MAX_REPEATED_FAILURES = 3
# Sent (once) when finish is called with nothing staged but the reply shows code or commands were run.
UNSTAGED_NUDGE = ("Not finished: no change is staged. Code you show in a message or try out in a command is not "
                  "applied to the user's files. Make the edit with edit_file or replace_lines, then call finish. "
                  "If no change is needed, call finish again.")
# finish is refused this many times while staged files do not parse.
MAX_FINISH_REJECTIONS = 2

# Mentioned files are included up front (like an @-mention), within these limits.
MENTION_MAX_FILES = 5
MENTION_MAX_CHARS = 12_000

# Below ~7B parameters a model rarely gets through a multi-step tool loop.
_SMALL_MODEL = re.compile(r"[:\-_](?:0\.5|1|1\.5|1\.8|2|3)b\b", re.IGNORECASE)


# A model that describes an edit instead of making it: "replace lines 22 and 23 with ```...```".
_LINE_RANGE = re.compile(r"\blines?\s+(\d+)(?:\s*(?:-|–|to|and|through)\s*(\d+))?", re.IGNORECASE)
_CODE_BLOCK = re.compile(r"```[\w+-]*[ \t]*\n(.*?)```", re.DOTALL)
_FILE_NAME = re.compile(r"[\w./-]+\.[A-Za-z0-9]+")


def described_edit(text: str, ws: Workspace, default_path: Optional[str]) -> Optional[ToolCall]:
    """The replace_lines call a reply describes in prose, when it is unambiguous: one code block,
    one line range with as many lines as the block, and a file (named in the text, or the one the
    agent last worked on) where those lines differ from the block."""
    blocks = _CODE_BLOCK.findall(text or "")
    if len(blocks) != 1:
        return None
    prose = _CODE_BLOCK.sub(" ", text)
    ranges = {(int(a), int(b or a)) for a, b in _LINE_RANGE.findall(prose)}
    if len(ranges) != 1:
        return None
    [(start, end)] = ranges
    new_lines = blocks[0].rstrip("\n").split("\n")
    if end < start or len(new_lines) != end - start + 1:
        return None
    paths = []
    for name in _FILE_NAME.findall(prose):
        try:
            rel = ws.rel(name)
            if ws.read(rel) is not None and rel not in paths:
                paths.append(rel)
        except ToolError:
            continue
    if len(paths) > 1 or not (paths or default_path):
        return None
    path = paths[0] if paths else default_path
    try:
        current = (ws.read(path) or "").split("\n")
    except ToolError:
        return None
    if end > len(current) or [l.strip() for l in current[start - 1:end]] == [l.strip() for l in new_lines]:
        return None
    return ToolCall(agent_llm._new_id(), "replace_lines",
                    {"path": path, "start_line": start, "end_line": end, "new_text": blocks[0].rstrip("\n")})


_DIFF_BLOCK = re.compile(r"```[\w+-]*[ \t]*\n((?:(?!```).)*?^@@.*?)```", re.DOTALL | re.MULTILINE)
_DIFF_TARGET = re.compile(r"^\+\+\+ (?:b/)?(\S+)", re.MULTILINE)


def _hunks(block: str) -> List[tuple]:
    """(old, new) text of each @@ hunk in a unified diff, from its context, - and + lines."""
    hunks: List[tuple] = []
    old: Optional[List[str]] = None
    new: List[str] = []
    for line in block.rstrip("\n").split("\n"):
        if line.startswith("@@"):
            if old is not None:
                hunks.append(("\n".join(old), "\n".join(new)))
            old, new = [], []
        elif old is None or line.startswith(("\\", "--- ", "+++ ")):
            continue
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith("+"):
            new.append(line[1:])
        else:
            old.append(line[1:] if line.startswith(" ") else line)
            new.append(old[-1])
    if old is not None:
        hunks.append(("\n".join(old), "\n".join(new)))
    return hunks


def diff_edits(text: str, ws: Workspace, default_path: Optional[str]) -> List[ToolCall]:
    """edit_file calls for the unified diffs a reply shows instead of making the edits ("```diff
    --- a/x.py +++ b/x.py @@ … ```"). Hunks are matched by content (models invent the line
    numbers); a hunk is kept only when its old lines occur once in the file and are not already
    replaced, so a diff the agent already applied is not applied twice."""
    calls: List[ToolCall] = []
    for block in _DIFF_BLOCK.findall(text or ""):
        target = _DIFF_TARGET.search(block)
        path = default_path
        if target:
            try:
                path = ws.rel(target.group(1))
            except ToolError:
                continue
        if not path:
            continue
        try:
            current = ws.read(path)
        except ToolError:
            continue
        if current is None:
            continue
        for old, new in _hunks(block):
            if not old.strip() or old == new:
                continue
            if current.count(old) != 1 and not (current.count(old) == 0 and _loose_match(current, old)):
                continue
            calls.append(ToolCall(agent_llm._new_id(), "edit_file",
                                  {"path": path, "old_string": old, "new_string": new}))
    return calls


def small_model_warning(model: str) -> Optional[str]:
    if _SMALL_MODEL.search(model or ""):
        return (f"{model} is a small model: it can handle one-line fixes but often fails on multi-step "
                "changes. For better results pick qwen2.5-coder:7b (or larger) or use the global LLM.")
    return None


class SecretVault:
    """Reversible secret masking for transcripts sent to a cloud model."""

    _TOKEN = re.compile(r"__SECRET_\d+__")

    def __init__(self) -> None:
        self._token_of: Dict[str, str] = {}
        self._value_of: Dict[str, str] = {}

    @property
    def count(self) -> int:
        return len(self._token_of)

    def _token(self, value: str) -> str:
        if self._TOKEN.fullmatch(value):
            return value
        if value not in self._token_of:
            token = f"__SECRET_{len(self._token_of) + 1}__"
            self._token_of[value] = token
            self._value_of[token] = value
        return self._token_of[value]

    def mask(self, text: str) -> str:
        for name, rx in SECRET_RULES:
            if name == "assigned_secret":
                text = rx.sub(lambda m: m.group(0).replace(m.group("val"), self._token(m.group("val"))), text)
            else:
                text = rx.sub(lambda m: self._token(m.group(0)), text)
        return text

    def unmask(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._TOKEN.sub(lambda m: self._value_of.get(m.group(0), m.group(0)), value)
        if isinstance(value, dict):
            return {k: self.unmask(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.unmask(v) for v in value]
        return value


# Runs in progress, so /agent/approve can reach the one waiting for an answer.
RUNS: Dict[str, "AgentRun"] = {}


def _args_preview(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Tool arguments for the step log, with file contents summarised instead of inlined."""
    out = {}
    for k, v in args.items():
        if k in ("content", "old_string", "new_string", "new_text") and isinstance(v, str):
            out[k] = f"{len(v.splitlines()) or 1} line(s)"
        else:
            out[k] = v
    return out


def _check_args(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if "__invalid_json__" in args:
        raise ToolError(f"the arguments for {name} were not valid JSON: {args['__invalid_json__']}")
    schema = _SCHEMAS[name]
    missing = [r for r in schema.get("required", []) if r not in args]
    if missing:
        raise ToolError(f"{name} is missing required argument(s): {', '.join(missing)}")
    return {k: v for k, v in args.items() if k in schema["properties"]}


class AgentRun:
    def __init__(self, prompt: str, repo_path: str, route: str, emit: Emit, *, code: str = "",
                 active_file: Optional[str] = None, history: Optional[List[Dict[str, str]]] = None,
                 mentioned_files: Optional[List[str]] = None, step_fn=agent_llm.step):
        self.id = uuid.uuid4().hex[:12]
        self.prompt = prompt
        self.route = route
        self.emit = emit
        self.code = code
        self.active_file = active_file
        self.history = history or []
        self.mentioned_files = mentioned_files or []
        self.ws = Workspace(repo_path)
        self.shadow = ShadowWorkspace(self.ws)
        self.vault = SecretVault() if route == "cloud" else None
        self.auto_approve = config.AGENT_AUTO_APPROVE_COMMANDS
        self._pending: Dict[str, asyncio.Future] = {}
        self._step = step_fn
        self.commands: List[Dict[str, Any]] = []
        self._last_path: Optional[str] = None  # file the agent last read or edited

    # --- approvals ---------------------------------------------------------------------------
    def resolve_approval(self, request_id: str, approved: bool, always: bool = False) -> bool:
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False
        if approved and always:
            self.auto_approve = True
        fut.set_result(bool(approved))
        return True

    async def _approve(self, command: str, cwd: str) -> bool:
        if self.auto_approve:
            return True
        return await self._ask("command", command=command, cwd=cwd)

    async def _ask(self, kind: str, **data: Any) -> bool:
        request_id = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        self.emit("approval", {"request_id": request_id, "kind": kind, **data})
        try:
            approved = await fut
        finally:
            self._pending.pop(request_id, None)
        self.emit("approval_resolved", {"request_id": request_id, "approved": approved})
        return approved

    # --- transcript --------------------------------------------------------------------------
    def _for_model(self, text: str) -> str:
        return self.vault.mask(text) if self.vault else text

    def _initial_messages(self) -> List[Dict[str, Any]]:
        try:
            listing = list_files(self.ws, ".")
        except ToolError:
            listing = "(unavailable)"
        system = SYSTEM_PROMPT.format(root=self.ws.root, platform=platform.system()) + \
            TOP_LEVEL_NOTE.format(listing=listing)
        parts = []
        if self.history:
            earlier = "\n".join(f"- Task: {h.get('prompt', '')}\n  Result: {h.get('summary', '')}"
                                for h in self.history[-3:])
            parts.append(f"Earlier agent tasks in this session (for context):\n{earlier}")
        if self.active_file:
            parts.append(f"The user has this file open: {self.active_file}")
        if self.code.strip():
            parts.append(f"The user selected this code:\n```\n{self.code}\n```")
        parts.extend(self._mentioned_contents())
        parts.append(f"Task:\n{self.prompt}")
        return [{"role": "system", "content": system},
                {"role": "user", "content": self._for_model("\n\n".join(parts))}]

    def _mentioned_contents(self) -> List[str]:
        """Files named in the prompt, included once so the model starts from the real code."""
        out = []
        for rel in self.mentioned_files[:MENTION_MAX_FILES]:
            try:
                text = self.ws.read(self.ws.rel(rel))
            except ToolError:
                continue
            if text is None:
                continue
            if len(text) > MENTION_MAX_CHARS:
                text = text[:MENTION_MAX_CHARS] + "\n… [truncated; use read_file for the rest]"
            out.append(f"Current content of {rel}:\n```\n{text}\n```")
        return out

    def _compact(self, messages: List[Dict[str, Any]]) -> None:
        """Elide the oldest tool outputs once the transcript outgrows the model's budget."""
        budget = (int(config.AGENT_LOCAL_NUM_CTX * 0.7) if self.route == "local"
                  else config.AGENT_CONTEXT_TOKENS)
        if count_tokens(json.dumps(messages)) <= budget:
            return
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        for m in tool_msgs[:-4]:
            if not m["content"].startswith("[elided"):
                m["content"] = "[elided to save context; run the tool again if you still need this]"
                if count_tokens(json.dumps(messages)) <= budget:
                    return

    # --- tools -------------------------------------------------------------------------------
    async def _execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "run_command":
            args = _check_args(name, args)
            command, cwd = str(args["command"]), str(args.get("cwd") or ".")
            self.ws.rel(cwd)  # reject paths outside the workspace before asking the user
            if not await self._approve(command, cwd):
                self.commands.append({"command": command, "cwd": cwd, "exit_code": None, "declined": True})
                return "The user declined to run this command. Do not run it again; continue without it."
            note = await self._prepare_sandbox()
            code, output = await self.shadow.run(command, cwd, args.get("timeout_s"))
            self.commands.append({"command": command, "cwd": cwd, "exit_code": code, "declined": False})
            if self.shadow.last_edits:
                paths = ", ".join(self.shadow.last_edits)
                self.emit("notice", {"text": f"The command changed {paths}; staged the change for review."})
                self.emit("changes", {"changes": self._change_stats()})
                output += (f"\n[The command changed {paths}; those changes are now staged as your proposed edits. "
                           "Prefer edit_file or replace_lines for edits.]")
            return f"{note}$ {command}\n[exit code {code}]\n{output}"
        fn = _FILE_TOOLS.get(name)
        if fn is None:
            raise ToolError(f"unknown tool {name!r}; available: {', '.join(_SCHEMAS)}")
        return fn(self.ws, **_check_args(name, args))

    async def _prepare_sandbox(self) -> str:
        """Before the sandbox is first built, ask whether files that look like secrets go in it.
        Asked even with auto-approved commands: that setting covers commands, not credentials."""
        if self.shadow.dir is not None:
            return ""
        files = await asyncio.to_thread(self.shadow.sensitive_files)
        if not files or await self._ask("sensitive_files", files=files):
            return ""
        self.shadow.exclude = set(files)
        return ("[The user chose to leave these files out of the sandbox, as they may hold secrets: "
                f"{', '.join(files)}. A failure caused by their absence is expected; do not recreate them.]\n")

    def _model_label(self) -> str:
        if self.route == "local":
            return llm.effective_local_model()
        provider = llm.effective_provider()
        return f"{provider}/{llm.effective_model(provider)}" if provider != "mock" else "mock-agent"

    # --- loop --------------------------------------------------------------------------------
    async def run(self) -> Dict[str, Any]:
        RUNS[self.id] = self
        start = time.perf_counter()
        messages = self._initial_messages()
        summary, stopped = "", "max_steps"
        steps = input_tokens = output_tokens = 0
        model, is_mock = self._model_label(), False
        bad_calls = continues = repeated = finish_rejections = 0
        unstaged_nudged = False
        failed_calls: set = set()
        tool_names = set(_SCHEMAS)
        self.emit("start", {"run_id": self.id, "route": self.route, "model": model, "repo_path": self.ws.root,
                            "warning": small_model_warning(model) if self.route == "local" else None})

        def progress(tokens: int) -> None:
            self.emit("progress", {"tokens": tokens})

        try:
            while steps < config.AGENT_MAX_STEPS:
                self._compact(messages)
                turn = await self._step(messages, TOOLS, self.route, progress=progress)
                steps += 1
                model, is_mock = turn.model or model, is_mock or turn.is_mock
                input_tokens += turn.input_tokens
                output_tokens += turn.output_tokens
                default_path = self._last_path or self.active_file
                if not turn.tool_calls:
                    # Masked text on the cloud route; the arguments are unmasked like any other call.
                    call = described_edit(turn.text, self.ws, default_path)
                    if call:
                        self.emit("notice", {"text": "The model described an edit without making it; "
                                                     f"staged it as replace_lines on {call.arguments['path']}."})
                        turn.tool_calls = [call]
                if not any(c.name in _MUTATING for c in turn.tool_calls):
                    # A diff shown in the message (or in finish's summary) instead of an edit tool call.
                    shown = turn.text + "".join("\n" + str(c.arguments.get("summary") or "")
                                                for c in turn.tool_calls if c.name == "finish")
                    edits = diff_edits(shown, self.ws, default_path)
                    if edits:
                        paths = ", ".join(sorted({c.arguments["path"] for c in edits}))
                        self.emit("notice", {"text": "The model showed a diff without making the edit; "
                                                     f"staged it on {paths}."})
                        turn.tool_calls = edits + turn.tool_calls
                messages.append(turn.message())
                text = self.vault.unmask(turn.text) if self.vault else turn.text
                if not turn.tool_calls:
                    if agent_llm.looks_like_tool_call(text, tool_names):
                        if bad_calls < MAX_BAD_CALL_RETRIES:
                            bad_calls += 1
                            self.emit("notice", {"text": "The model's tool call was malformed; asking it to retry."})
                            messages.append({"role": "user", "content": BAD_CALL_NUDGE})
                            continue
                        summary, stopped = (f"The model ({model}) kept producing malformed tool calls, so the task "
                                            "was stopped. Small local models often can't drive the agent; try "
                                            "qwen2.5-coder:7b or the global LLM."), "invalid_tool_calls"
                        break
                    if continues < MAX_CONTINUE_NUDGES and steps < config.AGENT_MAX_STEPS:
                        continues += 1
                        if text.strip():
                            self.emit("thought", {"text": text})
                        messages.append({"role": "user", "content": CONTINUE_NUDGE})
                        continue
                    if not text.strip():
                        summary, stopped = self._gave_up(model), "gave_up"
                        break
                    summary, stopped = text, "answered"
                    break
                if text.strip():
                    self.emit("thought", {"text": text})
                finished = False
                for call in turn.tool_calls:
                    args = self.vault.unmask(call.arguments) if self.vault else call.arguments
                    if call.name == "finish":
                        if (not self.ws.change_list() and not unstaged_nudged
                                and (_CODE_BLOCK.search(text + str(args.get("summary") or ""))
                                     or any(not c["declined"] for c in self.commands))):
                            # It wrote the fix in a code block, or tried it out in a command (`python -c
                            # "<fixed code>"`), but never staged it: ask once to apply it.
                            unstaged_nudged = True
                            self.emit("notice", {"text": "The model finished without staging the change it "
                                                         "showed; asking it to apply the edit."})
                            messages.append({"role": "tool", "tool_call_id": call.id, "content": UNSTAGED_NUDGE})
                            continue
                        problems = syntax_problems(self.ws)
                        if problems and finish_rejections < MAX_FINISH_REJECTIONS:
                            finish_rejections += 1
                            report = "\n".join(problems)
                            self.emit("notice", {"text": "Staged code does not parse; sent back to the model:\n" + report})
                            messages.append({"role": "tool", "tool_call_id": call.id, "content": self._for_model(
                                "Not finished: your staged changes have syntax errors. Fix them, then call finish "
                                "again.\n" + report)})
                            continue
                        summary, finished = str(args.get("summary") or text), True
                        if problems:
                            summary += "\n\n**Warning:** the proposed code still does not parse:\n```\n" + \
                                "\n".join(problems) + "\n```"
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": "done"})
                        continue
                    self.emit("tool_call", {"call_id": call.id, "name": call.name,
                                            "args": _args_preview(call.name, args)})
                    signature = json.dumps([call.name, args], sort_keys=True, default=str)
                    try:
                        result, ok = await self._execute(call.name, args), True
                    except ToolError as e:
                        result, ok = f"Error: {e}", False
                        if signature in failed_calls:
                            repeated += 1
                            result = REPEAT_NOTE + result
                        failed_calls.add(signature)
                    if ok and isinstance(args.get("path"), str) and call.name in _FILE_TOOLS:
                        try:
                            rel = self.ws.rel(args["path"])
                            self._last_path = rel if not self.ws.is_dir(rel) else self._last_path
                        except ToolError:
                            pass
                    self.emit("tool_result", {"call_id": call.id, "name": call.name, "ok": ok,
                                              "output": result[:3000]})
                    if ok and call.name in _MUTATING:
                        self.emit("changes", {"changes": self._change_stats()})
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": self._for_model(result)})
                if finished:
                    stopped = "finished"
                    break
                if repeated >= MAX_REPEATED_FAILURES:
                    summary, stopped = self._gave_up(model), "gave_up"
                    break
            else:
                summary = summary or ("Stopped after reaching the step limit "
                                      f"(AGENT_MAX_STEPS={config.AGENT_MAX_STEPS}). Review the staged changes "
                                      "below; you can ask the agent to continue.")
        finally:
            RUNS.pop(self.id, None)
            for fut in self._pending.values():
                fut.cancel()
            self.shadow.cleanup()
        return {
            "run_id": self.id,
            "summary": summary,
            "stopped_reason": stopped,
            "model": model,
            "route": self.route,
            "is_mock": is_mock,
            "steps": steps,
            "repo_path": self.ws.root,
            "changes": self.ws.change_list(),
            "commands": self.commands,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "secrets_masked": self.vault.count if self.vault else 0,
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }

    def _gave_up(self, model: str) -> str:
        staged = len(self.ws.change_list())
        return (f"The model ({model}) got stuck and stopped without finishing the task"
                + (f"; {staged} staged change(s) are listed below for review." if staged else
                   ", so nothing was changed.")
                + " Small local models often can't complete multi-step edits; try qwen2.5-coder:7b or the global LLM.")

    def _change_stats(self) -> List[Dict[str, Any]]:
        return [{k: c[k] for k in ("path", "status", "additions", "deletions")} for c in self.ws.change_list()]
