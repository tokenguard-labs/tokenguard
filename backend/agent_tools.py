"""Agent workspace: a staged-edit overlay on top of the repo, a sandbox copy to run commands in,
and the file tools the agent calls.

Nothing here writes to the user's repository. Edits are kept in memory (Workspace.changes) and
returned to the editor as proposed diffs; the user accepts or rejects them there. Commands run in
a throwaway copy of the repo (ShadowWorkspace) with the staged edits applied, so tests see the
proposed code while the real files stay untouched.
"""
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from compression.context_engine.redact import RULES as SECRET_RULES

from . import config

# Never listed or searched by the agent's tools. The sandbox still has them: HEAVY_DIRS are symlinked
# (installed dependencies the project's tests need, but too large to duplicate per run), the rest copied.
HEAVY_DIRS = {"node_modules", ".venv", "venv", "env", ".tox", ".gradle", "target", "vendor"}
SKIP_DIRS = HEAVY_DIRS | {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                          ".idea", ".next", ".cache", "dist", "build", "coverage", ".DS_Store"}
MAX_FILE_BYTES = 1_000_000


class ToolError(Exception):
    """A tool call the agent made wrongly; the message goes back to the model so it can retry."""


@dataclass
class Change:
    original: Optional[str]  # file content when the agent first touched it; None = did not exist
    proposed: Optional[str]  # content after the agent's edits; None = deleted


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


class Workspace:
    """The repo as the agent sees it: files on disk with the staged changes layered on top."""

    def __init__(self, root: str):
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise ValueError(f"workspace folder not found: {root}")
        self.changes: Dict[str, Change] = {}
        self._files: Optional[List[str]] = None

    # --- paths -------------------------------------------------------------------------------
    def rel(self, path: str) -> str:
        """Normalise a path the model gave (relative or absolute) to a repo-relative POSIX path."""
        path = (path or ".").strip()
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ToolError(f"{path!r} is outside the workspace ({self.root})")
        rel = os.path.relpath(full, self.root).replace(os.sep, "/")
        if rel == ".git" or rel.startswith(".git/"):
            raise ToolError("the .git folder is off limits")
        return rel

    def abs(self, rel: str) -> str:
        return self.root if rel == "." else os.path.join(self.root, *rel.split("/"))

    # --- reading -----------------------------------------------------------------------------
    def _disk_text(self, rel: str) -> Optional[str]:
        full = self.abs(rel)
        if not os.path.isfile(full):
            return None
        if os.path.getsize(full) > MAX_FILE_BYTES:
            raise ToolError(f"{rel} is larger than {MAX_FILE_BYTES // 1000} KB; read a smaller file")
        with open(full, "rb") as f:
            data = f.read()
        if _is_binary(data):
            raise ToolError(f"{rel} is a binary file")
        return data.decode("utf-8", errors="replace")

    def read(self, rel: str) -> Optional[str]:
        """Current content (staged if changed), or None when the file does not exist."""
        if rel in self.changes:
            return self.changes[rel].proposed
        return self._disk_text(rel)

    def is_dir(self, rel: str) -> bool:
        if rel == ".":
            return True
        prefix = rel + "/"
        return os.path.isdir(self.abs(rel)) or any(
            p.startswith(prefix) and c.proposed is not None for p, c in self.changes.items())

    def _disk_files(self) -> List[str]:
        """Repo files (git's view when available, so .gitignore is honoured), cached per run."""
        if self._files is None:
            self._files = _git_files(self.root) or _walk_files(self.root)
        return self._files

    def files(self, under: str = ".") -> List[str]:
        """Every file in the overlay view (disk + staged additions - staged deletions)."""
        out = set(self._disk_files())
        for path, change in self.changes.items():
            if change.proposed is None:
                out.discard(path)
            else:
                out.add(path)
        if under != ".":
            prefix = under + "/"
            out = {p for p in out if p.startswith(prefix)}
        return sorted(out)

    # --- staging -----------------------------------------------------------------------------
    def stage(self, rel: str, content: Optional[str]) -> None:
        if rel not in self.changes:
            self.changes[rel] = Change(original=self._disk_text(rel), proposed=content)
        else:
            self.changes[rel].proposed = content

    def change_list(self) -> List[Dict[str, Any]]:
        """Net changes (no-ops dropped) with a unified diff, for the editor's review panel."""
        out = []
        for path in sorted(self.changes):
            c = self.changes[path]
            if c.original == c.proposed:
                continue
            status = "added" if c.original is None else "deleted" if c.proposed is None else "modified"
            old, new = (c.original or "").splitlines(), (c.proposed or "").splitlines()
            diff = list(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", lineterm="", n=3))
            out.append({
                "path": path,
                "status": status,
                "original": c.original,
                "proposed": c.proposed,
                "additions": sum(1 for d in diff if d.startswith("+") and not d.startswith("+++")),
                "deletions": sum(1 for d in diff if d.startswith("-") and not d.startswith("---")),
                "diff": "\n".join(diff),
            })
        return out


def _git_files(root: str) -> Optional[List[str]]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    files = []
    for p in proc.stdout.decode("utf-8", errors="replace").split("\0"):
        if p and not any(part in SKIP_DIRS for part in p.split("/")[:-1]) and os.path.isfile(os.path.join(root, p)):
            files.append(p)
    return files


def _walk_files(root: str) -> List[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
        if len(files) > 50_000:
            break
    return files


# --- tools ------------------------------------------------------------------------------------

def _clip(text: str, limit: Optional[int] = None) -> str:
    limit = limit or config.AGENT_TOOL_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3:]
    return f"{head}\n… [{len(text) - len(head) - len(tail)} characters omitted] …\n{tail}"


def list_files(ws: Workspace, path: str = ".", recursive: bool = False) -> str:
    rel = ws.rel(path)
    if not ws.is_dir(rel):
        try:
            exists = ws.read(rel) is not None
        except ToolError:
            exists = True  # binary or too large, but there
        if exists:
            return f"{rel} (a file; use read_file to see its content)"
        raise ToolError(f"{rel} does not exist")
    files = ws.files(rel)
    base = "" if rel == "." else rel + "/"
    if recursive:
        shown = [f[len(base):] for f in files]
        more = len(shown) - 400
        return "\n".join(shown[:400]) + (f"\n… and {more} more files" if more > 0 else "") or "(empty)"
    entries = set()
    for f in files:
        head, _, rest = f[len(base):].partition("/")
        entries.add(head + "/" if rest else head)
    return "\n".join(sorted(entries, key=lambda e: (not e.endswith("/"), e))) or "(empty)"


def read_file(ws: Workspace, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
    rel = ws.rel(path)
    text = ws.read(rel)
    if text is None:
        raise ToolError(f"{rel} does not exist" + (" (it is a directory)" if ws.is_dir(rel) else ""))
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    max_lines = config.AGENT_MAX_READ_LINES
    end = min(len(lines), int(end_line) if end_line else start + max_lines - 1, start + max_lines - 1)
    body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
    note = ""
    if end < len(lines):
        note = f"\n[showing lines {start}-{end} of {len(lines)}; pass start_line={end + 1} to read on]"
    return _clip(f"{rel} ({len(lines)} lines)\n{body}{note}")


def search(ws: Workspace, pattern: str, path: str = ".", glob: Optional[str] = None,
           ignore_case: bool = False) -> str:
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    rel = ws.rel(path)
    candidates = [rel] if rel != "." and not ws.is_dir(rel) else ws.files(rel)
    hits: List[str] = []
    for f in candidates:
        if glob and not (fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], glob)):
            continue
        try:
            text = ws.read(f)
        except ToolError:
            continue
        if not text:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{f}:{n}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n[stopped at 200 matches; narrow the search]"
    return "\n".join(hits) or "no matches"


def _diff_preview(rel: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="", n=2)
    return _clip("\n".join(diff), 4000)


def _matching_lines(text: str, needle: str, limit: int = 8) -> str:
    """Lines containing `needle` (or its first line), numbered, so the model can pick the right one."""
    probe = needle.strip().splitlines()[0].strip() if needle.strip() else ""
    if not probe:
        return ""
    hits = [f"  line {n}: {line.strip()[:160]}" for n, line in enumerate(text.splitlines(), 1) if probe in line]
    more = f"\n  … and {len(hits) - limit} more" if len(hits) > limit else ""
    return "\n".join(hits[:limit]) + more


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(new_lines: List[str], old_lines: List[str]) -> List[str]:
    """Small models often drop the leading indentation of a replacement (copied out of a markdown
    block). When the new block starts at column 0 but the lines it replaces were indented, move it
    to the old indentation, keeping its own relative indentation."""
    old_first = next((line for line in old_lines if line.strip()), None)
    body = [i for i, line in enumerate(new_lines) if line.strip()]
    if old_first is None or not body:
        return new_lines
    base = _indent(old_first)
    if not base or _indent(new_lines[body[0]]):
        return new_lines
    rest = body[1:]
    if rest and all(len(_indent(new_lines[i])) >= len(base) for i in rest):
        shift = {body[0]}  # only the first line lost its indentation
    else:
        shift = set(body)
    return [base + line if i in shift else line for i, line in enumerate(new_lines)]


def _loose_match(text: str, old_string: str) -> Optional[Tuple[int, int]]:
    """The one run of lines equal to old_string's lines ignoring leading/trailing whitespace,
    as (first, last) 0-based line indexes; None when there is no match or more than one."""
    want = [line.strip() for line in old_string.strip("\n").split("\n")]
    if not any(want):
        return None
    lines = [line.strip() for line in text.split("\n")]
    hits = [i for i in range(len(lines) - len(want) + 1) if lines[i:i + len(want)] == want]
    return (hits[0], hits[0] + len(want) - 1) if len(hits) == 1 else None


def _indent_continuation(new_string: str, old_string: str, lead: str) -> str:
    """new_string replaces text that starts after `lead` (a line's indentation). If its further
    lines came without that indentation while the lines they replace had it, add it."""
    first, *rest = new_string.split("\n")
    body = [line for line in rest if line.strip()]
    if not body or min(len(_indent(line)) for line in body) > 0:
        return new_string
    old_body = [line for line in old_string.split("\n")[1:] if line.strip()]
    if old_body:
        if min(len(_indent(line)) for line in old_body) < len(lead):
            return new_string
    elif any(_indent(line) for line in body):
        return new_string  # mixed column-0 and indented lines: likely new top-level code
    return "\n".join([first] + [lead + line if line.strip() else line for line in rest])


def edit_file(ws: Workspace, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    rel = ws.rel(path)
    text = ws.read(rel)
    if text is None:
        raise ToolError(f"{rel} does not exist; use write_file to create it")
    if not old_string:
        raise ToolError("old_string is empty; use write_file to replace a whole file")
    count = text.count(old_string)
    if count == 0:
        span = _loose_match(text, old_string)
        if span:  # right lines, wrong indentation: replace them and indent the new text to match
            lines = text.split("\n")
            replacement = _reindent(new_string.strip("\n").split("\n"), lines[span[0]:span[1] + 1])
            new = "\n".join(lines[:span[0]] + replacement + lines[span[1] + 1:])
            ws.stage(rel, new)
            return f"Staged edit to {rel}:\n{_diff_preview(rel, text, new)}"
        near = _matching_lines(text, old_string)
        raise ToolError(f"old_string was not found in {rel}. Copy the exact text from the file (read_file), "
                        "including indentation, or use replace_lines with line numbers."
                        + (f" Lines containing its first line:\n{near}" if near else ""))
    if count > 1 and not replace_all:
        raise ToolError(f"old_string occurs {count} times in {rel}, so it is ambiguous. Use the whole line you "
                        f"want to change (with its indentation), or replace_lines with its line number. "
                        f"Matches:\n{_matching_lines(text, old_string)}")
    if not replace_all and "\n" in new_string:
        # old_string starts after a line's indentation: indent new_string's further lines to match.
        at = text.index(old_string)
        lead = text[text.rfind("\n", 0, at) + 1:at]
        if lead and not lead.strip():
            new_string = _indent_continuation(new_string, old_string, lead)
    new = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    ws.stage(rel, new)
    return f"Staged edit to {rel}:\n{_diff_preview(rel, text, new)}"


def replace_lines(ws: Workspace, path: str, start_line: int, end_line: int, new_text: str) -> str:
    """Replace lines start_line..end_line (1-based, inclusive) with new_text (empty deletes them)."""
    rel = ws.rel(path)
    text = ws.read(rel)
    if text is None:
        raise ToolError(f"{rel} does not exist; use write_file to create it")
    lines = text.split("\n")
    count = len(lines) - (1 if text.endswith("\n") else 0)
    try:
        start, end = int(start_line), int(end_line)
    except (TypeError, ValueError):
        raise ToolError("start_line and end_line must be numbers")
    if not (1 <= start <= end <= count):
        raise ToolError(f"line range {start}-{end} is outside {rel} (lines 1-{count})")
    replacement = new_text.split("\n") if new_text else []
    if replacement and replacement[-1] == "":
        replacement.pop()  # a trailing newline in new_text does not add an empty line
    replacement = _reindent(replacement, lines[start - 1:end])
    new ="\n".join(lines[: start - 1] + replacement + lines[end:])
    ws.stage(rel, new)
    return f"Staged edit to {rel}:\n{_diff_preview(rel, text, new)}"


def write_file(ws: Workspace, path: str, content: str) -> str:
    rel = ws.rel(path)
    if ws.is_dir(rel) and ws.read(rel) is None:
        raise ToolError(f"{rel} is a directory")
    old = ws.read(rel)
    ws.stage(rel, content)
    if old is None:
        return f"Staged new file {rel} ({len(content.splitlines())} lines)"
    return f"Staged rewrite of {rel}:\n{_diff_preview(rel, old, content)}"


def delete_file(ws: Workspace, path: str) -> str:
    rel = ws.rel(path)
    if ws.read(rel) is None:
        raise ToolError(f"{rel} does not exist")
    ws.stage(rel, None)
    return f"Staged deletion of {rel}"


def syntax_problems(ws: Workspace) -> List[str]:
    """Parse errors in staged Python/JSON files. Compiled, never executed, so no approval is needed."""
    import json
    problems = []
    for path, change in sorted(ws.changes.items()):
        if change.proposed is None or change.proposed == change.original:
            continue
        try:
            if path.endswith(".py"):
                compile(change.proposed, path, "exec", dont_inherit=True)
            elif path.endswith(".json"):
                json.loads(change.proposed)
        except SyntaxError as e:
            problems.append(f"{path}:{e.lineno}: {type(e).__name__}: {e.msg}"
                            + (f"\n    {e.text.rstrip()}" if e.text else ""))
        except ValueError as e:
            problems.append(f"{path}: invalid JSON: {e}")
    return problems


# --- sandbox for commands --------------------------------------------------------------------

# Never handed to agent commands: provider keys the backend loaded from .env.
_SECRET_ENV = re.compile(r"(API_KEY|_TOKEN|_SECRET|PASSWORD)$", re.IGNORECASE)


# Files that likely hold credentials. The sandbox asks the user before copying them in.
_SENSITIVE_NAME = re.compile(
    r"^(\.env(\..+)?|.+\.(pem|key|p12|pfx|jks|keystore|kdbx|ppk)|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|\.(netrc|pgpass|pypirc|npmrc|git-credentials)|credentials(\..+)?|secrets?\.(json|ya?ml|toml|ini|txt)"
    r"|service[-_]?account.*\.json)$", re.IGNORECASE)
_TEMPLATE_NAME = re.compile(r"\.(example|sample|template|dist)$", re.IGNORECASE)
_SECRET_SCAN_BYTES = 256_000


class ShadowWorkspace:
    """A temporary copy of the repo with the staged changes applied, for running tests/builds.

    Every file is copied, gitignored ones included (only .git is left out), so commands see the
    workspace as it is on disk. Dependency folders (HEAVY_DIRS) and symlinked folders are linked
    from the real repo instead, so `pytest` / `npm test` find their installed packages without
    duplicating them. Paths in `exclude` (sensitive files the user chose to keep out) are skipped.
    """

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.dir: Optional[str] = None
        self.exclude: set = set()
        self.last_edits: List[str] = []  # repo files the last command changed, now staged
        self._plan_cache: Optional[Tuple[List[str], List[str], List[str]]] = None

    def _plan(self) -> Tuple[List[str], List[str], List[str]]:
        """(folders, files to copy, folders to symlink), all repo-relative."""
        if self._plan_cache is None:
            dirs, files, links = [], [], []
            for dirpath, dirnames, filenames in os.walk(self.ws.root):
                base = os.path.relpath(dirpath, self.ws.root)
                rel = (lambda n: n) if base == "." else (lambda n: f"{base}/{n}".replace(os.sep, "/"))
                keep = []
                for d in dirnames:
                    if d == ".git":
                        continue
                    if d in HEAVY_DIRS or os.path.islink(os.path.join(dirpath, d)):
                        links.append(rel(d))
                    else:
                        keep.append(d)
                        dirs.append(rel(d))
                dirnames[:] = keep
                files.extend(rel(n) for n in filenames if n != ".git")
            self._plan_cache = (dirs, files, links)
        return self._plan_cache

    def sensitive_files(self) -> List[str]:
        """Files that look like they hold secrets: credential file names anywhere, plus gitignored
        files whose content matches a secret pattern (tracked files are already in the repo)."""
        _, files, _ = self._plan()
        tracked = set(_git_files(self.ws.root) or [])
        out = []
        for f in files:
            name = f.rsplit("/", 1)[-1]
            if _SENSITIVE_NAME.match(name) and not _TEMPLATE_NAME.search(name):
                out.append(f)
                continue
            if f in tracked:
                continue
            full = self.ws.abs(f)
            try:
                if os.path.islink(full) or os.path.getsize(full) > _SECRET_SCAN_BYTES:
                    continue
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if _is_binary(data):
                continue
            text = data.decode("utf-8", errors="replace")
            if any(rx.search(text) for _, rx in SECRET_RULES):
                out.append(f)
        return out

    def _create(self) -> None:
        dirs, files, links = self._plan()
        files = [f for f in files if f not in self.exclude]
        total = 0
        for f in files:
            try:
                total += os.path.getsize(self.ws.abs(f))
            except OSError:
                pass
        if total > config.AGENT_SHADOW_MAX_MB * 1_000_000:
            raise ToolError(f"the repo is too large to copy into a sandbox ({total // 1_000_000} MB > "
                            f"AGENT_SHADOW_MAX_MB={config.AGENT_SHADOW_MAX_MB}); commands are unavailable")
        self.dir = tempfile.mkdtemp(prefix="localpilot-agent-")
        for d in dirs:
            os.makedirs(os.path.join(self.dir, *d.split("/")), exist_ok=True)
        for f in files:
            try:
                shutil.copy2(self.ws.abs(f), os.path.join(self.dir, *f.split("/")), follow_symlinks=True)
            except OSError:
                pass  # broken symlink, socket, unreadable file
        for d in links:
            try:
                os.symlink(os.path.realpath(self.ws.abs(d)), os.path.join(self.dir, *d.split("/")),
                           target_is_directory=True)
            except OSError:
                pass

    def sync(self) -> str:
        """Create the sandbox on first use and bring it in line with the staged changes."""
        if self.dir is None:
            self._create()
        for rel, change in self.ws.changes.items():
            dest = os.path.join(self.dir, *rel.split("/"))
            if change.proposed is None:
                if os.path.isfile(dest):
                    os.remove(dest)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(change.proposed)
        return self.dir

    def cleanup(self) -> None:
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None

    def _activate_venv(self, env: Dict[str, str]) -> None:
        """Put a Python venv on PATH, as an activated shell would.

        The backend is started as `.venv/bin/python -m uvicorn` without activating the venv, so
        `python` / `pytest` would otherwise resolve to the system interpreter (often missing, or
        without the project's packages). The repo's own venv wins; the backend's venv is the
        fallback, appended so it only fills in commands the system lacks.
        """
        bin_name = "Scripts" if os.name == "nt" else "bin"
        path = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        env.pop("PYTHONHOME", None)
        for d in (".venv", "venv", "env"):
            venv = os.path.join(self.ws.root, d)
            if os.path.isfile(os.path.join(venv, "pyvenv.cfg")):
                env["VIRTUAL_ENV"] = venv
                env["PATH"] = os.pathsep.join([os.path.join(venv, bin_name)] + path)
                return
        own = os.path.dirname(sys.executable)
        if sys.prefix != sys.base_prefix and own not in path:
            env["PATH"] = os.pathsep.join(path + [own])

    async def run(self, command: str, cwd: str = ".", timeout_s: Optional[float] = None) -> Tuple[int, str]:
        """Run a shell command in the sandbox; returns (exit code, combined output)."""
        rel = self.ws.rel(cwd)
        root = self.sync()
        workdir = os.path.join(root, *([] if rel == "." else rel.split("/")))
        if not os.path.isdir(workdir):
            raise ToolError(f"{rel} is not a directory")
        env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
        env.update(CI="1", PYTHONDONTWRITEBYTECODE="1")
        self._activate_venv(env)
        timeout = min(float(timeout_s or config.AGENT_COMMAND_TIMEOUT_S), config.AGENT_COMMAND_TIMEOUT_S)
        before = self._stamps()
        self.last_edits = []
        proc = await asyncio.create_subprocess_shell(
            command, cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            code = proc.returncode
        except asyncio.TimeoutError:
            _kill(proc)
            out, code = f"[timed out after {int(timeout)}s and was killed]".encode(), -1
        except asyncio.CancelledError:
            _kill(proc)
            raise
        # Show the model the real repo path, not the sandbox's temp path.
        text = out.decode("utf-8", errors="replace").replace(root, self.ws.root)
        self.last_edits = self._stage_edits(before)
        return code, _clip(text)

    def _stamps(self) -> Dict[str, Tuple[int, int]]:
        """(mtime, size) of each repo file in the sandbox, to spot the ones a command rewrites."""
        out = {}
        for rel in self.ws.files():
            if rel in self.exclude:
                continue
            try:
                st = os.stat(os.path.join(self.dir, *rel.split("/")))
            except OSError:
                continue
            out[rel] = (st.st_mtime_ns, st.st_size)
        return out

    def _stage_edits(self, before: Dict[str, Tuple[int, int]]) -> List[str]:
        """Stage the repo files a command changed in the sandbox (`sed -i`, `cat > f`, a script that
        writes the fix), so an edit made that way is proposed instead of lost with the sandbox.
        Only existing text files count; new files and deletions are build/test output as often as not."""
        staged = []
        for rel, stamp in before.items():
            full = os.path.join(self.dir, *rel.split("/"))
            try:
                st = os.stat(full)
                if (st.st_mtime_ns, st.st_size) == stamp or st.st_size > MAX_FILE_BYTES:
                    continue
                with open(full, "rb") as f:
                    data = f.read()
                if _is_binary(data):
                    continue
                text = data.decode("utf-8", errors="replace")
                if text != self.ws.read(rel):
                    self.ws.stage(rel, text)
                    staged.append(rel)
            except (OSError, ToolError):
                continue
        return staged


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
