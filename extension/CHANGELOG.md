# Changelog

All notable changes to this extension are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-16

First Marketplace release (preview).

### Added

- **LocalPilot sidebar**: a chat view with a model panel (status, cloud and local model pickers), the conversation, and a composer. The three panes can be resized and their sizes are remembered.
- **Routing**: every prompt is scored for complexity (0–100). Simple and medium prompts run on a local Ollama model, and complex ones are offered to a cloud LLM. Low-confidence local answers can be escalated with one click.
- **Cloud providers**: DeepSeek, Anthropic, OpenAI, any OpenAI-compatible endpoint (`custom`), and a mock provider for demos. API keys are kept in VS Code Secret Storage.
- **Context engine**: a persistent, incremental codebase index picks only the relevant code for cloud requests, fits it to the model's token budget (at most 16,000 tokens), and redacts secrets before anything is sent. Answer cards show tokens sent compared with the full context.
- **Agent mode**: the agent can explore, edit, create, delete, and move files and run commands in a sandbox copy of the workspace. Every command needs approval. Edits are reviewed in the file itself, with Accept / Reject on each change, before anything is saved.
- **Ollama management**: commands to install Ollama, start it, download and delete models, and unload them. The selected model can be preloaded and is unloaded on exit.
- **Self-contained backend**: the Python gateway ships inside the extension. On first run it is installed into a private virtual environment, built from an existing Python 3.9+ or, when there is none, from a downloaded, checksum-verified standalone Python 3.12. `tokenguard.backendPath` and `tokenguard.pythonPath` remain available as development overrides.
