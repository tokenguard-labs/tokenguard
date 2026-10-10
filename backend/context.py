"""Context optimizer adapter: the integration point with Person 2.

Person 2 develops in `compression/` and exposes:
    optimize_context(prompt, repo_path, code, token_budget) -> ContextResult   (required)
    redact_text(text) -> (redacted_text, count)                                (optional)

This module calls their implementation when it is ready and falls back to the naive
keyword-based version below when it isn't (not implemented yet, or it raised), so the
router never breaks because of the optimizer.
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .contracts import ContextResult
from .tokens import count_tokens

log = logging.getLogger("localpilot.context")

SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cpp", ".c", ".h",
               ".rb", ".php", ".cs", ".kt", ".swift", ".sql", ".yaml", ".yml", ".json", ".md"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".next", ".idea", ".vscode"}
MAX_FILE_BYTES = 200_000
STOPWORDS = {"the", "a", "an", "in", "on", "of", "to", "and", "or", "is", "this", "that", "fix", "my", "it", "for", "with", "code"}

try:
    import compression as _person2
except ImportError:
    _person2 = None


def load_repo(repo_path: str) -> List[Tuple[str, str]]:
    files = []
    for root, dirs, names in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path = os.path.join(root, name)
            if os.path.splitext(name)[1] not in SOURCE_EXTS or os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    files.append((os.path.relpath(path, repo_path), f.read()))
            except (UnicodeDecodeError, OSError):
                continue
    return files


def repo_stats(repo_path: Optional[str]) -> Tuple[int, int]:
    """(total_tokens, num_files) of a repo, used by the analyzer before any routing decision."""
    if not repo_path or not os.path.isdir(repo_path):
        return 0, 0
    files = load_repo(repo_path)
    return sum(count_tokens(f"# {p}\n{c}") for p, c in files), len(files)


def _render(files: List[Tuple[str, str]]) -> str:
    return "\n\n".join(f"# File: {path}\n{content}" for path, content in files)


def _fallback_optimize(prompt: str, repo_path: Optional[str], code: str, token_budget: int) -> ContextResult:
    """Naive keyword relevance, no compression, no redaction."""
    files = load_repo(repo_path) if repo_path and os.path.isdir(repo_path) else []
    full = (code + "\n\n" + _render(files)).strip()
    original_tokens = count_tokens(full)

    terms = {t for t in re.findall(r"[a-zA-Z_]{3,}", prompt.lower()) if t not in STOPWORDS}

    def relevance(item: Tuple[str, str]) -> int:
        path, content = item
        text = (path + " " + content).lower()
        return sum(text.count(t) for t in terms) + 20 * sum(t in path.lower() for t in terms)

    selected, used = [], count_tokens(code)
    for path, content in sorted(files, key=relevance, reverse=True):
        if relevance((path, content)) == 0:
            break
        t = count_tokens(f"# File: {path}\n{content}")
        if used + t > token_budget:
            continue
        selected.append((path, content))
        used += t

    context = (code + "\n\n" + _render(selected)).strip()
    return ContextResult(
        context=context,
        original_tokens=original_tokens,
        optimized_tokens=count_tokens(context),
        files_selected=[p for p, _ in selected],
    )


def optimize_context(prompt: str, repo_path: Optional[str], code: str = "", token_budget: int = 3000) -> ContextResult:
    if _person2 is not None:
        try:
            result = _person2.optimize_context(prompt, repo_path, code=code, token_budget=token_budget)
            if not isinstance(result, ContextResult):
                raise TypeError(f"expected ContextResult, got {type(result).__name__}")
            return result
        except NotImplementedError:
            pass  # Person 2's module exists but isn't ready yet
        except Exception:
            log.exception("Person 2 optimizer failed; using fallback")
    return _fallback_optimize(prompt, repo_path, code, token_budget)


def redact_text(text: str) -> Tuple[str, int]:
    """Mask secrets in free text (the prompt). No-op until Person 2 provides it."""
    fn = getattr(_person2, "redact_text", None)
    if fn is not None:
        try:
            return fn(text)
        except NotImplementedError:
            pass
        except Exception:
            log.exception("Person 2 redact_text failed; sending prompt unredacted")
    return text, 0


# ---------- Request resolution: which repo / code does this prompt refer to? ----------

_ABS_PATH = re.compile(r"(?:~|/)[^\s'\"`<>|]+")


@dataclass
class ResolvedRequest:
    prompt: str                 # prompt with any typed paths removed (their content is attached instead)
    repo_path: Optional[str]    # repo the optimizer searches
    code: str                   # editor selection + any files named in the prompt
    sources: List[str] = field(default_factory=list)  # human-readable: where the context came from


_ROOT_MARKERS = (".git", "package.json", "pyproject.toml", "setup.py", "requirements.txt", "go.mod", "Cargo.toml", "pom.xml")


def _project_root(path: str) -> Optional[str]:
    """Nearest enclosing folder that looks like a project root (git repo or package manifest)."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if any(os.path.exists(os.path.join(d, m)) for m in _ROOT_MARKERS):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _paths_in_prompt(prompt: str) -> List[Tuple[str, str]]:
    """(text as typed, absolute path) for every existing file/folder path in the prompt.
    A whole line is tried first so paths containing spaces work when pasted on their own line."""
    found: List[Tuple[str, str]] = []
    for line in prompt.splitlines():
        whole = line.strip().strip("'\"")
        if whole.startswith(("/", "~")) and os.path.exists(os.path.expanduser(whole)):
            found.append((line.strip(), os.path.realpath(os.path.expanduser(whole))))
            continue
        for m in _ABS_PATH.finditer(line):
            text = m.group(0).rstrip(".,;:)")
            path = os.path.expanduser(text)
            if os.path.exists(path):
                found.append((text, os.path.realpath(path)))
    return found


def resolve_request(prompt: str, repo_path: Optional[str], code: str = "") -> ResolvedRequest:
    """A path typed in the prompt wins over the UI's repo_path: a folder becomes the repo to
    search, a file is attached as code and its repo is searched."""
    sources: List[str] = []
    if code.strip():
        sources.append("editor selection")
    prompt_dir: Optional[str] = None
    prompt_file_repo: Optional[str] = None
    for text, path in _paths_in_prompt(prompt):
        prompt = prompt.replace(text, " ")
        if os.path.isdir(path):
            prompt_dir = prompt_dir or path
        elif os.path.getsize(path) <= MAX_FILE_BYTES:
            try:
                with open(path, encoding="utf-8") as f:
                    code = f"{code}\n\n# File: {os.path.basename(path)}\n{f.read()}".strip()
            except (UnicodeDecodeError, OSError):
                continue
            if repo_path and path.startswith(os.path.realpath(repo_path) + os.sep):
                inferred = repo_path  # file is inside the open workspace: search the workspace
            else:
                inferred = _project_root(path) or os.path.dirname(path)
            prompt_file_repo = prompt_file_repo or inferred
            sources.append(f"file from prompt: {path}")

    cleaned = re.sub(r"[ \t]+", " ", prompt).strip()
    resolved_repo = prompt_dir or prompt_file_repo or repo_path
    if resolved_repo and os.path.isdir(resolved_repo):
        origin = "from prompt" if resolved_repo != repo_path else "workspace"
        sources.insert(0, f"repo ({origin}): {resolved_repo}")
    else:
        resolved_repo = None
    return ResolvedRequest(prompt=cleaned or prompt.strip(), repo_path=resolved_repo, code=code, sources=sources)
