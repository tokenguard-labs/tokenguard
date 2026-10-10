# Extension-First Plan

How to take TokenGuard / LocalPilot from a *source-checkout developer extension* to an
*install-and-go Marketplace extension*, with a comparison of the two practical ways to get
there. Decisions captured for this plan:

- **Document both** extension-first strategies (bundled backend binary vs. TypeScript port);
  the final choice is deferred.
- **Distribution:** VS Code Marketplace **and** Open VSX.
- **First release platforms:** **Linux x64 only**.

---

## 1. Purpose & scope

Today the extension only runs when the whole repository (Python backend + compression engine +
`.venv`) is present on the machine, and it is launched via `code --extensionDevelopmentPath=`
from `make dev`. That is fine for development but not something a normal user can install.

"Extension-first" means the user:

1. Installs the extension (Marketplace / Open VSX / VSIX), and
2. Provides a cloud provider + API key (and optionally uses local models via Ollama),

without cloning the repo, installing Python, creating a virtualenv, or running any build command.

This document:

- describes the **current** experience and why the path/URL settings exist,
- describes the **target** extension-first experience,
- documents **two implementation strategies** (A: bundled Python backend binary, B: backend
  rewritten in TypeScript) and compares them,
- lays out the **publishing plan** for Marketplace + Open VSX (Linux x64 first),
- gives a **phased roadmap** and the **risks/open questions**.

---

## 2. Current experience (source checkout)

### 2.1 Prerequisites

| Requirement | Why |
|---|---|
| Git + the TokenGuard repo | The engine (`backend/`, `compression/`) is not shipped in the VSIX |
| Python 3.9+ | The backend is FastAPI/uvicorn; deps installed into `.venv` |
| Node.js 18+ | To compile the extension (`extension/out/` is also committed) |
| VS Code 1.82+ with the `code` CLI | `make dev` opens an Extension Development Host |
| Ollama (optional) | Local inference; setup starts `ollama serve` and pulls the model |
| Cloud API key (optional) | DeepSeek / Anthropic / OpenAI for the "global" path |

### 2.2 One command

```bash
make dev                       # or: make dev WORKSPACE=/path/to/target-repo
```

`make dev` runs `scripts/setup.sh` (idempotent), then opens VS Code:

1. Creates `.venv/` and installs `requirements.txt` (only when the requirements hash changes).
2. Creates `backend/.env` from `.env.example` if missing.
3. Starts `ollama serve` if unreachable and pulls `LOCAL_MODEL` if missing.
4. `npm install` (when needed) and compiles the extension.
5. `code --new-window --extensionDevelopmentPath=extension <WORKSPACE>`.

Other targets: `make backend` (foreground API with `--reload`), `make test`, `make setup`,
`make clean`.

### 2.3 What the extension does at runtime

- On activation it starts the backend itself as a child process:
  `<repo>/.venv/bin/python -m uvicorn backend.main:app` (cwd = repo root). If something already
  answers `GET {tokenguard.apiUrl}/health`, it reuses it and won't kill it on exit.
- The **LocalPilot** activity-bar view opens the sidebar webview.
- Per prompt: `POST /analyze` (routing decision, no model) → user picks local/global
  (or auto-local) → `POST /chat` runs context compression + secret redaction, then Ollama
  (local) or the cloud provider. `/stats` accumulates metrics.
- API keys are stored in **VS Code SecretStorage** and sent as `X-LLM-Api-Key` (with
  `X-LLM-Provider` / `X-LLM-Model` / `X-Local-Model`) request headers; they are never written
  to `backend/.env`.
- Only **one local model** is kept resident: changing the sidebar model unloads the previous
  model (`PUT /local-model`) before loading the new one.

### 2.4 Endpoints (backend)

| Endpoint | Purpose |
|---|---|
| `POST /analyze` | Routing decision only |
| `POST /chat` | Run the pipeline (local or cloud) |
| `GET /stats` | Session metrics (mock excluded) |
| `GET /health` | Ollama + local models, active/loaded local model, cloud provider/model + key validation |
| `GET /models` | Local models available in Ollama + active/loaded |
| `PUT /local-model` | Switch active local model (unload previous, preload new) |

### 2.5 Why `apiUrl` / `backendPath` / `pythonPath` exist

The VSIX packages **only** `extension/`. The engine is ~3.3k LOC across `backend/` and
`compression/` and needs a Python interpreter with `fastapi`, `uvicorn`, `pydantic`, `httpx`,
and `tiktoken`. So the extension must be told:

- `tokenguard.backendPath` — where `backend/main.py` is.
- `tokenguard.pythonPath` — which interpreter to spawn.
- `tokenguard.apiUrl` — where to reach it (and how to reuse an externally started backend).

A true extension-first build removes the need for these by shipping the engine with the
extension.

### 2.6 Pros / cons

- **Pros:** full engine, easy to hack on, one codebase, prebuilt `out/` runs without npm.
- **Cons:** many manual prerequisites and failure points (venv, pip, Node, ports, paths);
  not distributable to end users; activation starts the backend on every VS Code launch.

---

## 3. Target extension-first experience

### 3.1 User flow

1. Install **TokenGuard** from the Marketplace / Open VSX (or a VSIX).
2. First run shows an onboarding panel:
   - choose provider (`deepseek` / `anthropic` / `openai` / `mock`) and paste the API key
     (stored in SecretStorage),
   - optionally: detect Ollama, offer to start it / pull a model / pick a local model.
3. Open the sidebar and ask a question. Local prompts stay on the machine; complex ones go to
   the selected cloud provider.

No repo, no Python, no Node, no `make`.

### 3.2 Settings surface

| Setting | Default | Notes |
|---|---|---|
| `tokenguard.cloudProvider` | `deepseek` | `deepseek` / `anthropic` / `openai` / `mock` |
| `tokenguard.cloudModel` | `""` | Empty = provider default |
| `tokenguard.localModel` | `""` | Empty = backend default; dropdown from `/models` |
| API keys | SecretStorage | Set via **TokenGuard: Set Cloud API Key** |

`apiUrl`, `backendPath`, `pythonPath`, `autoStartBackend` move into an **Advanced** section (or
are removed once the backend is bundled).

### 3.3 Prerequisites

- **Required:** VS Code 1.82+, a cloud API key (for cloud prompts).
- **Optional:** Ollama for local prompts (extension can detect/guide installation).

### 3.4 Pros / cons

- **Pros:** one install, ~1–2 minutes to first answer, no toolchain, keys handled securely,
  works offline after install.
- **Cons:** larger download (bundled backend) or a rewrite (TS port); platform-specific
  packages (Linux first); Ollama still needed for local models.

---

## 4. Strategy A — Bundle the Python backend (Linux x64 first)

Keep the existing engine; ship it *inside* the extension so no Python is required.

### 4.1 Mechanics

- Add a PyInstaller spec that bundles:
  - `backend/` and `compression/` (as data/imports),
  - the FastAPI/uvicorn/pydantic/httpx runtime,
  - **`tiktoken` data** (BPE files + native lib) via `--collect-data tiktoken` / hooks.
- The build produces a single executable (or one-folder bundle) that starts uvicorn on
  `127.0.0.1:<port>`.
- Place per-platform artifacts under:
  `extension/bin/<platform>-<arch>/tokenguard-backend[.exe]`
  (first release: `extension/bin/linux-x64/tokenguard-backend`).
- `BackendManager` changes:
  - pick the bundled binary by `process.platform` + `process.arch` when present;
  - fall back to `pythonPath` / `backendPath` for dev (keeps the current workflow working);
  - choose a free port (or keep `apiUrl` default `localhost:8000`) and wait for `/health`.
- Package a **platform-specific** VSIX: `vsce package --target linux-x64` so other platforms'
  binaries are not shipped.

### 4.2 Pros / cons

- **Pros:** reuses the full engine and the SecretStorage/header work already done; one
  codebase; no Python/Node for users.
- **Cons:** adds a release pipeline (build matrix, code signing not required on Linux);
  VSIX grows (~40–80 MB); PyInstaller + `tiktoken` data can be fiddly; platform-specific VSIXs.

---

## 5. Strategy B — Port the backend to TypeScript

Move all logic into the extension and delete the Python dependency.

### 5.1 Scope (from `backend/` + `compression/`, ~3.3k LOC)

| Area | Files today | Port notes |
|---|---|---|
| Routing / analyzer | `backend/analyzer.py`, `router.py` | Deterministic rules, straightforward |
| Context selection + compression | `compression/context_engine/*` | Largest piece (chunker, imports, selector, walker, redact) |
| Token counting | `backend/tokens.py`, `compression/context_engine/tokens.py` | Needs a JS tokenizer (`tiktoken` wasm / `js-tiktoken`) or a chars/4 estimate |
| Ollama + cloud clients | `backend/llm.py` | `fetch` to `localhost:11434` and provider APIs |
| Single local-model handling | `backend/llm.py` | Reimplement unload/preload/lock logic |
| HTTP API surface | `backend/main.py` | Not needed — call libraries directly in-process |

### 5.2 Pros / cons

- **Pros:** smallest VSIX (~1–3 MB), fastest install, no Python at all, fewest failure points.
- **Cons:** large rewrite and **parity risk** (routing/compression/token numbers must match);
  needs a JS tokenizer to keep metrics consistent; doubles maintenance if the Python engine
  lives on.

---

## 6. Comparison

| | Current (source checkout) | A — bundled backend binary | B — TypeScript port |
|---|---|---|---|
| User steps | clone + Python + Node + Ollama + `make dev` | install VSIX → provider + key → (Ollama) | install VSIX → key → (Ollama) |
| Time to first answer | ~5–15 min | ~1–2 min | ~1–2 min |
| Needs Python | Yes (3.9+) | No | No |
| Needs Node / clone | Yes | No | No |
| Prerequisites failure points | Many (venv, pip, Node, ports, paths) | Few (OS/arch match, download size) | Very few (Ollama only for local) |
| Download size (VSIX) | small source + big local Python | ~40–80 MB (Linux) | ~1–3 MB |
| Offline after install | Yes (repo) | Yes (bundled binary) | Yes |
| Engineering effort | — (done) | Medium | Large |
| Maintenance risk | one codebase | one codebase + release pipeline | two implementations (if Python kept) |
| Best for | contributors | **Linux-first launch, least risk** | **long-term smoothest UX** |

**Recommendation:** ship **Strategy A** first (fastest path to a real Marketplace extension,
reuses the engine and the secure key handling already built), then evaluate **Strategy B** later
if binary size/portability becomes a concern.

---

## 7. Publishing plan (Marketplace + Open VSX)

### 7.1 One-time accounts

- **VS Code Marketplace:** a publisher (via Azure DevOps), then a Personal Access Token with
  the **Marketplace → Manage** scope (`VSCE_PAT`).
- **Open VSX:** an Eclipse account + access token (`OVSX_PAT`).

### 7.2 Manifest hygiene (`extension/package.json`)

Add/verify:

- `publisher`, `license`, `repository`, `homepage`, `bugs`,
- `icon` — **128×128 PNG** (Marketplace requires PNG; convert `media/circuit-board.svg`),
- `keywords`, richer `categories`, optional `galleryBanner`,
- `engines.vscode` (already `^1.82.0`),
- lazy activation: activate on the view/command instead of `onStartupFinished`, and start the
  backend when the sidebar opens.

Also add `LICENSE` and `CHANGELOG.md`, and tighten `.vscodeignore` (ship `out/` + bundled
`bin/`; exclude `src/`, maps, `node_modules`, tests).

### 7.3 Tooling

- Dev deps: `@vscode/vsce` and `ovsx`.
- Scripts: `package` (`vsce package --target linux-x64`), `publish:ms`
  (`vsce publish --target linux-x64`), `publish:ovsx` (`ovsx publish`), `publish` (both).
- Use `--pre-release` while stabilizing.

### 7.4 CI (GitHub Actions)

1. Build the Linux x64 backend artifact (Strategy A) or run `tsc` (Strategy B).
2. `vsce package --target linux-x64`.
3. `vsce publish` (with `VSCE_PAT`) and `ovsx publish` (with `OVSX_PAT`).
4. Tag/version bump per release.

> Note: `vsce package` publishes the extension folder only, so the bundled `bin/` must live
> **inside** `extension/` (not at the repo root).

---

## 8. Phased roadmap

| Phase | Work | Depends on |
|---|---|---|
| 0 | Decide A vs B; confirm Linux x64 first | this doc |
| 1 | Manifest/publisher hygiene, `LICENSE`, `CHANGELOG`, icon, `vsce`/`ovsx`, lazy activation, "Advanced" settings grouping | — (safe either way) |
| 2 | Backend delivery: A = PyInstaller + `BackendManager` binary selection; B = TS port | Phase 0 |
| 3 | Onboarding: provider/key panel, Ollama detect/start/pull, local-model picker | Phase 2 |
| 4 | CI/CD: build + package + publish to Marketplace & Open VSX | Phases 1–2 |
| 5 | QA: clean Linux VM with no Python/Node — install, set key, run cloud + local prompts, test Ollama-absent path | Phases 1–4 |

Phase 1 is safe regardless of the A/B decision and can start immediately.

---

## 9. Risks & open questions

- **PyInstaller + `tiktoken`:** native lib + BPE data must be collected or token counting falls
  back to chars/4; verify metrics match the Python runs.
- **Binary size / platform VSIX:** accept ~40–80 MB for Linux; other OSes need their own builds.
- **Portability matrix:** Linux-first means macOS/Windows users stay on the source workflow
  until those binaries exist.
- **Ollama dependency:** local prompts still need Ollama; the extension should detect it and
  guide installation rather than bundle it.
- **Parity (Strategy B):** routing/compression/token results must match the Python engine; needs
  a golden test set.
- **Secrets:** keep using SecretStorage + per-request headers; never persist keys to disk.
- **Marketplace review:** use `--pre-release`, clear README/permissions, and the right
  `extensionKind`/`categories`.

---

## Appendix — key files

| Concern | Location |
|---|---|
| Extension entry + webview + commands | `extension/src/extension.ts` |
| Backend process management | `extension/src/backend.ts` |
| Manifest / settings / commands | `extension/package.json` |
| Backend API | `backend/main.py` |
| LLM clients + local-model control | `backend/llm.py` |
| Routing | `backend/analyzer.py`, `backend/router.py` |
| Context engine | `compression/context_engine/` |
| Dev setup | `Makefile`, `scripts/setup.sh` |
