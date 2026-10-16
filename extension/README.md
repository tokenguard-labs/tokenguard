# tokenguard README

This is the README for your extension "tokenguard". After writing up a brief description, we recommend including the following sections.

## Features

Describe specific features of your extension including screenshots of your extension in action. Image paths are relative to this README file.

For example if there is an image subfolder under your extension project workspace:

\!\[feature X\]\(images/feature-x.png\)

> Tip: Many popular extensions utilize animations. This is an excellent way to show off your extension! We recommend short, focused animations that are easy to follow.

## Requirements

If you have any requirements or dependencies, add a section describing those and how to install and configure them.

## Extension Settings

* `tokenguard.apiUrl`: URL of the LocalPilot backend (default `http://localhost:8000`).
* `tokenguard.autoStartBackend`: Start the backend as a child process on activation.
* `tokenguard.backendPath`, `tokenguard.pythonPath`: Override the backend folder / Python interpreter.
* `tokenguard.cloudProvider`: Global LLM provider — `deepseek`, `anthropic`, `openai`, `custom`, or `mock`.
* `tokenguard.cloudModel`: Model name for the selected provider (empty = provider default).
* `tokenguard.customBaseUrl`: Base URL for the `custom` OpenAI-compatible provider (e.g. `https://api.groq.com/openai/v1`).
* `tokenguard.cloudModelsAutoFetch`: Fetch the provider's model list for the sidebar picker (default `true`).
* `tokenguard.localModel`: Ollama model for local inference (empty = `LOCAL_MODEL` from `backend/.env`).
* `tokenguard.ollamaUrl`: URL of the local Ollama server (default `http://localhost:11434`).
* `tokenguard.autoStartOllama`: Start `ollama serve` with the extension if it isn't running (default `true`).
* `tokenguard.preloadLocalModel`: Load the selected model when VS Code opens so it isn't shown idle (default `true`).
* `tokenguard.stopOllamaOnExit`: On exit, stop the `ollama serve` started by TokenGuard/setup (default `true`). Models are always unloaded to free RAM; externally started Ollama is never killed.

## Choosing a cloud model

The sidebar's **Cloud** section picks the provider and model and manages the API key inline:

- **Provider** — `deepseek`, `anthropic`, `openai`, `custom`, or `mock`. `custom` reveals a
  **Base URL** field for any OpenAI-compatible endpoint (Groq, OpenRouter, self-hosted).
- **Model** — populated from `GET /cloud/models`: the provider's live model list (using your key)
  merged with curated context-window/price metadata; without a key it falls back to the curated
  catalog. Models are grouped Recommended / All and show context and `$in/$out` per 1M tokens.
- **API key** — a masked field with **Save** / **Clear**, stored in SecretStorage (per provider);
  never written to disk. The existing command palette command still works.
- **Test connection** — re-runs the credential check (bypassing the cache) and refreshes the list.

`/analyze` shows an estimated cost per request, each cloud answer card shows its actual `cost_usd`,
and the sidebar shows cumulative cloud spend from `/stats`. Prices come from a curated catalog and
are estimates (custom endpoints show no price).

## Choosing a local model

The LocalPilot sidebar has a **Local model** dropdown with two groups (from `GET /models`,
backed by `/health`):

- **Installed** — every model Ollama has already pulled. Models bigger than this machine is
  checked against the detected RAM/swap/VRAM and flagged in the list ("may use swap") or marked
  "too big for this machine"; selecting one warns before it is loaded, and a ⚠ badge stays next
  to "loaded in memory" while a swap-bound model runs.
- **Recommended — needs download** — models that fit this machine, chosen from the host's
  detected RAM/swap/VRAM (models that only fit by using swap are marked "may use swap").
  Selecting one asks whether to download it (`ollama pull`) and shows progress.

Pick one before sending a prompt; the choice is saved to `tokenguard.localModel` and sent to the
backend as the `X-Local-Model` header. "Default" uses `LOCAL_MODEL` from `backend/.env`. You can
also run **TokenGuard: Download Local Model** from the command palette to pull a model directly.

Changing the selection calls `PUT /local-model`, which **unloads the previously loaded model
first** and then preloads the new one, so only one local model is resident in RAM/VRAM at a time.
The sidebar shows which model is currently `loaded in memory`. A `preload` flag lets the backend
load the model immediately; if it's disabled, the model loads on the next prompt.

## Process cleanup on exit

When VS Code closes, the extension stops everything **it** started: the backend process, the
loaded local model(s) (freeing RAM/VRAM), and the `ollama serve` it launched. An Ollama that was
already running (systemd, or started outside the extension) is **not** killed — only its models
are unloaded. `setup.sh` writes the PID to `.ollama.pid` when it starts Ollama so the extension
can stop that process. Use **TokenGuard: Unload Local Models** to free memory without exiting, or
set `tokenguard.stopOllamaOnExit=false` to leave the managed Ollama running.

## API keys

Cloud API keys are stored with VS Code's **SecretStorage** and sent to the backend as
`X-LLM-Provider` / `X-LLM-Model` / `X-LLM-Api-Key` headers on each request, so they are never
written to `backend/.env` or any other file.

* Run **TokenGuard: Set Cloud API Key** to choose a provider and enter (or clear) a key.
* Run **TokenGuard: Clear Cloud API Key** to remove the key for the active provider.
* The sidebar shows whether the active key validated; `/health` exposes `cloud_valid` / `cloud_detail`.

## Known Issues

Calling out known issues can help limit users opening duplicate issues against your extension.

## Release Notes

Users appreciate release notes as you update your extension.

### 1.0.0

Initial release of ...

### 1.0.1

Fixed issue #.

### 1.1.0

Added features X, Y, and Z.

---

## Following extension guidelines

Ensure that you've read through the extensions guidelines and follow the best practices for creating your extension.

* [Extension Guidelines](https://code.visualstudio.com/api/references/extension-guidelines)

## Working with Markdown

You can author your README using Visual Studio Code. Here are some useful editor keyboard shortcuts:

* Split the editor (`Cmd+\` on macOS or `Ctrl+\` on Windows and Linux).
* Toggle preview (`Shift+Cmd+V` on macOS or `Shift+Ctrl+V` on Windows and Linux).
* Press `Ctrl+Space` (Windows, Linux, macOS) to see a list of Markdown snippets.

## For more information

* [Visual Studio Code's Markdown Support](http://code.visualstudio.com/docs/languages/markdown)
* [Markdown Syntax Reference](https://help.github.com/articles/markdown-basics/)

**Enjoy!**
