# Context Optimizer: Person 2 Handoff

**You own everything in `compression/`.** Person 1 (router) won't edit files here, and you don't need to edit anything outside it. The router already calls your code. Until it's ready, a simple built-in version runs in its place, so nothing is blocked.

## Where your code fits

```text
User prompt ──► Router (Person 1) ──► score says CLOUD
                                         │
                                         ▼
               compression.optimize_context(prompt, repo_path, code, token_budget)   ◄── YOU
                                         │  returns ContextResult
                                         ▼
                 Cloud LLM receives:  redact_text(prompt)  +  result.context
```

Your function is called on every `/analyze` and `/chat` request. That means once per request in the UI's preview step and once more when the answer runs. Keep it fast: **under 2 seconds** for a normal repo.

## What you receive (inputs)

| Argument | Type | Example | Notes |
|---|---|---|---|
| `prompt` | `str` | `"Fix the login bug"` | The user's request. Use it to judge relevance. |
| `repo_path` | `str` or `None` | `"/Users/me/project"` | Absolute path to the project. Can be `None`, or a path that doesn't exist (don't crash). |
| `code` | `str` | `"def login(...): ..."` | Code the user selected in the editor. `""` if none. **Always include it, first.** |
| `token_budget` | `int` | `3000` | Hard limit for the size of `context`. Set by `CLOUD_CONTEXT_TOKEN_BUDGET` in `.env`. |

## What you return (output)

A `ContextResult`, defined in [`backend/contracts.py`](../backend/contracts.py):

| Field | Type | Meaning |
|---|---|---|
| `context` | `str` | **The exact text sent to the cloud** with the prompt, already compressed and redacted. Start each file with `# File: <path>`. |
| `original_tokens` | `int` | Tokens of **all** code: selected code plus every source file in the repo, before any trimming. This is the "cloud-only" baseline used for the savings numbers. Don't include the prompt. |
| `optimized_tokens` | `int` | `count_tokens(context)`. Must be `<= token_budget`. |
| `files_selected` | `List[str]` | Repo-relative paths that made it into `context`, e.g. `["auth.py", "db.py"]`. The UI shows these. |
| `secrets_redacted` | `int` | How many secrets you masked. |
| `redaction_details` | `List[str]` | What was masked and where, e.g. `"AWS access key in config.py:1"`. **Never include the secret itself.** |
| `optimizer` | `str` | Your version name, e.g. `"person2-v1"`. It shows up in API responses, so we can see whose code ran. |

Example:

```python
ContextResult(
    context="# File: auth.py\ndef login(username, password): ...\n\n# File: config.py\nAWS_ACCESS_KEY_ID = \"[REDACTED_AWS_KEY]\"\n...",
    original_tokens=7590,
    optimized_tokens=412,
    files_selected=["auth.py", "config.py"],
    secrets_redacted=2,
    redaction_details=["AWS access key in config.py:1", "password in config.py:3"],
    optimizer="person2-v1",
)
```

## Rules

1. **Count tokens with `from backend.tokens import count_tokens`.** This is the same counter (tiktoken `cl100k_base`) the router and UI use, so the numbers line up in the dashboard.
2. **Stay within `token_budget`.** If the relevant code is bigger, compress it (strip comments and docstrings, keep function signatures, summarize long bodies) or drop the least relevant pieces.
3. **Never put a secret in `context`.** Redact before you count tokens. Covers API keys, AWS keys, private keys, passwords, tokens and connection strings, including inside `code`.
4. **Don't crash.** Handle `None` or missing `repo_path`, binary files, huge files, and non-UTF-8 files. If your function raises an unexpected error, the router logs it and falls back to the simple version, but please don't rely on that.
5. **Keep the two function signatures in `compression/__init__.py` unchanged.** Put the real work in other files in this folder (e.g. `ingest.py`, `relevance.py`, `compress.py`, `redact.py`) and call them from `__init__.py`.

## Optional: `redact_text(text) -> (redacted_text, count)`

The prompt itself is also sent to the cloud, and users sometimes paste keys into it. If you implement `redact_text`, the router runs it on the prompt before every cloud call. Until then, the prompt is sent as-is.

## How to check your work

From the repo root:

```bash
source .venv/bin/activate
python -m compression.check_contract
```

It builds a sample repo with fake secrets (`AKIAIOSFODNN7EXAMPLE` is AWS's documented example key) and checks every rule above: contract, relevance, token savings, redaction and edge cases. Right now it runs our simple built-in version and gets **14/18**. The 4 failures are the redaction checks, which are yours to build. **Target: 18/18**, plus the 2 optional `redact_text` checks.

To check it end to end, start the API (`make backend`) and send a request with a `repo_path`. The response should show `"context_optimizer": "person2-v1"` along with your `files_selected` and `secrets_redacted`.

## Suggested order (P0 first)

1. **File ingestion and relevance:** load the repo, score files against the prompt, and pick the best ones within the budget. This alone makes the demo numbers work.
2. **Secret redaction:** regex patterns for common key formats, plus `KEY/SECRET/PASSWORD/TOKEN = "..."` assignments.
3. **Compression:** strip comments and docstrings, and reduce unselected-but-related files to signatures only.
4. **If time remains:** function-level chunks, import and dependency following with `ast` (e.g. `auth.py` imports `db`, so include `db.find_user`), and caching repeated requests.

## What we need back from you

- Your implementation in `compression/`, with `python -m compression.check_contract` passing.
- A short note listing which secret patterns you cover, so we can say so on stage.
- Tell Person 1 before changing any field in `backend/contracts.py`. It's shared by both sides.
