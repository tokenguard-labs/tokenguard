# Marketplace Extension Release

This document lists everything changed to make the VS Code extension (`extension/`) publishable on
the Visual Studio Marketplace as **TokenGuard — LocalPilot 0.1.0 (preview)**. The changes landed in
commit `fe599c4` ("Added licence and backend gen for extension") on `agentic_v2`.

Before this, the extension only worked from a TokenGuard repo checkout: it expected `../backend`
next to itself and a `.venv` created by `make setup`. An installed VSIX has neither, so the backend
now ships inside the package and sets up its own Python environment on first run.

## Summary

| Area | Change | Files |
|---|---|---|
| Backend bundling | `backend/` and `compression/` are copied into the VSIX at package time | `extension/scripts/copy-backend.mjs`, `extension/package.json` |
| Backend runtime | The extension creates its own venv on first run, downloading a standalone Python if needed | `extension/src/backend.ts`, `extension/src/extension.ts` |
| Runtime dependencies | A trimmed requirements file for the bundled backend | `extension/requirements.runtime.txt` |
| Packaging tool | `@vscode/vsce` added, `npm run package` script | `extension/package.json`, `extension/package-lock.json` |
| Marketplace manifest | Publisher, license, icon, preview flag, repo links, keywords, categories, banner | `extension/package.json` |
| License | MIT, at the repo root and in the extension | `LICENSE`, `extension/LICENSE` |
| Third-party notices | Licenses of bundled and downloaded components | `extension/THIRD_PARTY_NOTICES.md` |
| Changelog | Rewritten in Keep a Changelog format with a `0.1.0` entry | `extension/CHANGELOG.md` |
| README | Rewritten as the Marketplace listing page | `extension/README.md` |
| Icon | 128×128 PNG | `extension/media/icon.png` |
| Package contents | `.vscodeignore` excludes sources, `node_modules`, tests, caches | `extension/.vscodeignore` |
| Repo hygiene | Generated `extension/python/` is git-ignored; scaffold quickstart removed | `.gitignore`, `extension/vsc-extension-quickstart.md` (deleted) |

## How the backend is packaged

`scripts/copy-backend.mjs` runs as part of `vscode:prepublish`:

```json
"vscode:prepublish": "npm run compile && node scripts/copy-backend.mjs"
```

It:

1. Deletes and recreates `extension/python/`.
2. Copies every `.py` file from `../backend` and `../compression` into `python/backend` and
   `python/compression`, preserving the folder structure and skipping `__pycache__`.
3. Copies `requirements.runtime.txt` to `python/requirements.txt`.

`extension/python/` is generated, so it is listed in the root `.gitignore`. `vsce package` then
includes it in the VSIX (only `python/**/__pycache__/**` is excluded by `.vscodeignore`).

## How the backend is installed and run

`BackendManager` (`extension/src/backend.ts`) now takes the extension's global storage path
(`context.globalStorageUri.fsPath`, passed in from `extension.ts`). On activation it picks a backend
source and interpreter; **the first match wins**:

1. **`tokenguard.backendPath`** (development override). Uses `tokenguard.pythonPath` if set, else
   `<repo>/.venv`. Fails with a clear message if the folder has no `main.py`.
2. **`../backend` next to the extension**, i.e. a repo checkout or the Extension Development Host.
   Uses `tokenguard.pythonPath` or `<repo>/.venv`, and offers to run setup if the venv is missing.
3. **The bundled `python/` folder** inside the installed extension. Uses `tokenguard.pythonPath` if
   set; otherwise the extension's own venv (below).

In every case uvicorn runs `backend.main:app` from the folder that contains `backend/` and
`compression/`, bound to `127.0.0.1` on the port from the configured API URL. A backend that is
already answering on that URL (for example `make backend`) is still reused and never killed.

### First-run venv setup

For case 3, `_ensureVenv` manages `<globalStorage>/venv`:

- It hashes `python/requirements.txt` (SHA-256) and compares it to `<globalStorage>/venv.stamp`.
  If the venv exists and the hash matches, it starts straight away. If the requirements changed in
  an update, it re-runs `pip install` into the existing venv.
- Otherwise it shows a progress notification ("LocalPilot: setting up the backend (first run
  only)") and:
  1. Finds a base Python (below).
  2. Creates the venv with `python -m venv` (removing any half-created one first).
  3. Runs `pip install --disable-pip-version-check --upgrade -r requirements.txt`.
  4. Writes the stamp only after a successful install.
- All setup commands stream their output to **Output > LocalPilot Backend**. On failure the status
  says to check the internet connection and run **TokenGuard: Restart Backend**, or set
  `tokenguard.pythonPath`.

### Finding a system Python

`findSystemPython` tries, in order:

- **Windows:** `py -3`, `python`, then python.org installs under `%LOCALAPPDATA%\Programs\Python`,
  `%ProgramFiles%` and `%ProgramFiles(x86)%` (newest `Python3xx` first).
- **macOS / Linux:** `python3`, `python`, then `/opt/homebrew/bin/python3`,
  `/usr/local/bin/python3`, `/usr/bin/python3` and the python.org framework path.

A candidate is accepted only if it is **3.9 or newer** and can import `venv` and `ensurepip`. The
`ensurepip` check rejects Debian/Ubuntu Pythons without `python3-venv`, where `-m venv` would fail.

### Standalone Python download (fallback)

If no usable Python is found, `_standalonePython` downloads a pinned CPython build from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone):

| Setting | Value |
|---|---|
| Release | `20260929` |
| Python version | `3.12.14` |
| Archive | `cpython-3.12.14+20260929-<triple>-install_only.tar.gz` |
| Supported targets | `x86_64` / `aarch64` × Linux (`unknown-linux-gnu`), macOS (`apple-darwin`), Windows (`pc-windows-msvc`) |
| Integrity check | SHA-256 per target, pinned in `STANDALONE_PYTHON_SHA256` |
| Timeout | 10 minutes |
| Install location | `<globalStorage>/python` |

The archive is verified against the pinned digest before it is written to disk, unpacked with `tar`
(bundled with macOS, Linux and Windows 10+), and deleted afterwards. A failed unpack removes the
partial install. On unsupported platforms or architectures the output channel asks the user to
install Python 3.9+ or set `tokenguard.pythonPath`.

To move to a newer Python build, update `STANDALONE_PYTHON_RELEASE`, `STANDALONE_PYTHON_VERSION`
and all six digests from the release's asset list together.

### Runtime requirements

`extension/requirements.runtime.txt` is `backend/requirements.txt` without the Streamlit UI
dependencies:

```
fastapi>=0.110.0
uvicorn>=0.27.0
pydantic>=2.0
httpx>=0.27.0
tiktoken>=0.5.0
```

Keep it in sync when the backend gains or drops a dependency. Changing it changes the hash, so
existing users get a re-install on their next start.

### Settings changes

The descriptions of two settings were updated to match the new behaviour:

- `tokenguard.backendPath`: now described as a development override. Empty means the bundled backend.
- `tokenguard.pythonPath`: must already have the backend dependencies installed. Empty means the
  extension's own venv (or `<repo>/.venv` for a source checkout).

## Packaging tool (vsce)

- **Tool:** [`@vscode/vsce`](https://github.com/microsoft/vscode-vsce) `^3.9.2`, added as a dev
  dependency (the scoped package, not the deprecated unscoped `vsce`).
- **Script:** `npm run package` runs `vsce package`, which triggers `vscode:prepublish` (compile,
  vendor the webview libraries, copy the backend).
- **Output:** `tokenguard-0.1.0.vsix`, about 270 KB. `*.vsix` is git-ignored.

To check what goes into the package without building it:

```sh
cd extension
npx vsce ls
```

To publish (needs a Personal Access Token for the `tokenguard-labs` publisher):

```sh
npx vsce login tokenguard-labs
npx vsce publish
```

### What the VSIX contains

`.vscodeignore` now also excludes `node_modules/**`, `requirements.runtime.txt`,
`python/**/__pycache__/**` and `out/test/**`, on top of the existing source, map and config
exclusions. The package contains:

- `package.json`, `README.md`, `CHANGELOG.md`, `LICENSE`, `THIRD_PARTY_NOTICES.md`
- `out/*.js` (compiled extension)
- `media/` (icon, chat webview, vendored `marked`, `DOMPurify`, `highlight.js` and their licenses)
- `python/` (the bundled backend, compression packages and `requirements.txt`)

The extension has no runtime npm dependencies, so excluding `node_modules` is safe.

## Marketplace manifest (`package.json`)

| Field | Before | After |
|---|---|---|
| `displayName` | `TokenGuard` | `TokenGuard — LocalPilot` |
| `description` | `AI Router and token optimizer` | Local-first AI coding assistant: answers simple questions with a local Ollama model and sends hard ones to a cloud LLM with only the relevant, secret-redacted code. |
| `version` | `0.0.1` | `0.1.0` |
| `publisher` | — | `tokenguard-labs` |
| `license` | — | `MIT` |
| `icon` | — | `media/icon.png` |
| `preview` | — | `true` (shows the Preview badge) |
| `repository` / `bugs` / `homepage` | — | `github.com/tokenguard-labs/tokenguard` |
| `keywords` | — | ai, llm, ollama, local llm, chat, agent, privacy, token, router, deepseek, claude, openai |
| `categories` | `Other` | `AI`, `Chat`, `Programming Languages` |
| `galleryBanner` | — | `#1e1e1e`, dark theme |

The `tokenguard-labs` publisher must exist on the Marketplace, and the repository URLs must resolve,
before publishing.

## License

- **MIT License**, `Copyright (c) 2026 TokenGuard Labs`.
- Added at the repo root (`LICENSE`) and in `extension/LICENSE`, because `vsce` packages only files
  inside `extension/` and warns when no license is present.
- `package.json` declares `"license": "MIT"`, and the README ends with a License section.

## Third-party notices

`extension/THIRD_PARTY_NOTICES.md` covers two groups:

- **Bundled in the VSIX:** `marked` (MIT), `DOMPurify` (MPL-2.0 OR Apache-2.0, used under
  Apache-2.0) and `highlight.js` (BSD-3-Clause). Their license texts are in `media/vendor/licenses/`.
- **Downloaded on first run, not redistributed:** the standalone CPython 3.12 (BSD-3-Clause build
  scripts, PSF-2.0 for CPython) and the PyPI packages installed into the venv (fastapi, uvicorn,
  pydantic, httpx, tiktoken and their dependencies, under MIT, BSD-3-Clause, Apache-2.0, PSF-2.0 and
  MPL-2.0).

## Changelog

`extension/CHANGELOG.md` was rewritten from the scaffold template. It now follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/), with one entry:

**`[0.1.0] - 2026-10-16`: first Marketplace release (preview).** Added:

- LocalPilot sidebar (model panel, conversation, composer; resizable panes)
- Complexity-based routing between local Ollama and cloud models, with one-click escalation
- Cloud providers: DeepSeek, Anthropic, OpenAI, OpenAI-compatible (`custom`), mock; keys in Secret Storage
- Context engine: incremental index, 16,000-token budget, secret redaction, token savings on answer cards
- Agent mode with sandboxed commands, approval for every command, and in-file Accept / Reject review
- Ollama management commands (install, start, download, delete, unload, preload)
- Self-contained backend with first-run venv setup

Add a new section at the top for each release and bump `version` in `package.json` to match.

## README (Marketplace page)

`extension/README.md` replaced the scaffold README and is what the Marketplace shows. Sections:
Features, Requirements, Getting started, Commands, Extension settings, Privacy, Troubleshooting,
License. The Requirements section explains that no Python setup is needed and what first-run setup
downloads (about 25–65 MB for the standalone Python, if needed).

There is a commented-out placeholder for screenshots. They should go in `extension/images/` as PNG
or GIF; the Marketplace does not allow SVG images in READMEs.

## Release checklist

1. Bump `version` in `extension/package.json` and add a `CHANGELOG.md` entry.
2. If backend dependencies changed, update `extension/requirements.runtime.txt`.
3. `cd extension && npm ci && npm run package`.
4. Check the file list with `npx vsce ls`, and install the VSIX in a clean VS Code profile
   (`code --install-extension tokenguard-<version>.vsix`) to test first-run setup.
5. `npx vsce publish`.
