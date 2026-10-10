"""Self-check for Person 2's optimizer. Run from the repo root:

    python -m compression.check_contract

Builds a small sample repo (with fake secrets), calls compression.optimize_context and
redact_text, and checks every rule in compression/README.md. All checks passing means the
router can use your code with no changes on Person 1's side.
"""
import os
import tempfile
import time

import compression
from backend import context as adapter
from backend.contracts import ContextResult
from backend.tokens import count_tokens

FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # AWS's documented example key, not real
FAKE_PASSWORD = "hunter2-not-a-real-password"

SAMPLE_REPO = {
    "auth.py": '''from config import AWS_ACCESS_KEY_ID, DB_PASSWORD
import db


def login(username, password):
    """Log a user in. BUG: compares the raw password instead of the hash."""
    user = db.find_user(username)
    if user is None:
        return None
    if user.password == password:
        return create_session(user)
    return None


def create_session(user):
    return {"user_id": user.id, "token": "session-" + str(user.id)}
''',
    "config.py": f'''AWS_ACCESS_KEY_ID = "{FAKE_AWS_KEY}"
AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
DB_PASSWORD = "{FAKE_PASSWORD}"
DB_HOST = "localhost"
''',
    "db.py": '''class User:
    def __init__(self, id, username, password):
        self.id, self.username, self.password = id, username, password


USERS = {"alice": User(1, "alice", "hashed-pw")}


def find_user(username):
    return USERS.get(username)
''',
    "utils/strings.py": '''def slugify(text):
    return "-".join(text.lower().split())
''',
    # Big unrelated module so the full repo exceeds the token budget
    "reports/generate.py": "\n\n".join(
        f'''def report_section_{i}(rows):
    """Build section {i} of the quarterly sales report."""
    total = sum(r["amount"] for r in rows if r["region"] == "region_{i}")
    return {{"section": {i}, "total": total, "rows": len(rows)}}'''
        for i in range(120)
    ),
}

PROMPT = "Fix the login bug in the authentication code"
BUDGET = 1500

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def make_repo(root):
    for rel, content in SAMPLE_REPO.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def main():
    try:
        compression.optimize_context("x", None)
        fn, who = compression.optimize_context, "YOUR optimizer"
    except NotImplementedError:
        fn, who = adapter._fallback_optimize, "the built-in FALLBACK (yours raises NotImplementedError)"
    except Exception:
        fn, who = compression.optimize_context, "YOUR optimizer"
    print(f"\nChecking {who}\n")

    with tempfile.TemporaryDirectory() as repo:
        make_repo(repo)
        full_tokens = sum(count_tokens(c) for c in SAMPLE_REPO.values())

        start = time.perf_counter()
        r = fn(PROMPT, repo, code="", token_budget=BUDGET)
        elapsed = time.perf_counter() - start

        print("Contract")
        check("returns a ContextResult", isinstance(r, ContextResult), type(r).__name__)
        if not isinstance(r, ContextResult):
            return
        check("optimized_tokens <= token_budget", r.optimized_tokens <= BUDGET, f"{r.optimized_tokens} <= {BUDGET}")
        actual = count_tokens(r.context)
        check("optimized_tokens matches count_tokens(context)", abs(actual - r.optimized_tokens) <= max(5, actual * 0.05),
              f"reported {r.optimized_tokens}, actual {actual}")
        check("original_tokens covers the whole repo", r.original_tokens >= full_tokens * 0.9,
              f"{r.original_tokens} vs repo ~{full_tokens}")
        check("files_selected are real repo-relative paths",
              all(os.path.isfile(os.path.join(repo, f)) for f in r.files_selected), str(r.files_selected))
        check("finishes in under 2s", elapsed < 2, f"{elapsed:.2f}s")

        print("\nQuality")
        check("selects the relevant file (auth.py)", "auth.py" in r.files_selected, str(r.files_selected))
        check("skips the unrelated big file", "reports/generate.py" not in r.files_selected)
        saved = 1 - r.optimized_tokens / max(r.original_tokens, 1)
        check("cuts tokens by at least 50%", saved >= 0.5, f"{r.original_tokens} -> {r.optimized_tokens} ({saved:.0%} saved)")

        print("\nSecurity")
        # A prompt that makes config.py relevant, so its secrets must be masked, not just skipped
        sec = fn("Why can't the app connect to AWS? Check the AWS config", repo, code="", token_budget=BUDGET)
        check("selects config.py for an AWS-config question", "config.py" in sec.files_selected, str(sec.files_selected))
        check("fake AWS key is not in context", FAKE_AWS_KEY not in sec.context)
        check("fake DB password is not in context", FAKE_PASSWORD not in sec.context)
        check("secrets_redacted counts them", sec.secrets_redacted >= 2, f"secrets_redacted={sec.secrets_redacted}")
        check("redaction_details never contain the secret itself",
              not any(FAKE_AWS_KEY in d or FAKE_PASSWORD in d for d in sec.redaction_details), str(sec.redaction_details))

        print("\nEdge cases")
        sel = fn(PROMPT, None, code=f'API_KEY = "{FAKE_AWS_KEY}"\ndef f(): pass', token_budget=BUDGET)
        check("works with no repo (selected code only)", isinstance(sel, ContextResult) and "def f" in sel.context)
        check("redacts secrets inside selected code", FAKE_AWS_KEY not in sel.context)
        empty = fn(PROMPT, None, code="", token_budget=BUDGET)
        check("no repo + no code -> empty context", empty.context.strip() == "" and empty.optimized_tokens == 0)
        missing = fn(PROMPT, "/path/that/does/not/exist", code="", token_budget=BUDGET)
        check("missing repo_path doesn't crash", isinstance(missing, ContextResult))

    print("\nredact_text (optional)")
    try:
        text, n = compression.redact_text(f"my key is {FAKE_AWS_KEY}, why does login fail?")
        check("removes the secret from the prompt", FAKE_AWS_KEY not in text, text)
        check("returns the count", n >= 1, f"count={n}")
    except NotImplementedError:
        print("  [SKIP] not implemented yet")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    print(f"optimizer name reported: {r.optimizer!r}  (set ContextResult.optimizer to identify your version)\n")


if __name__ == "__main__":
    main()
