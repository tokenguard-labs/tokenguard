# TokenGuard developer commands. `make dev` is the only one you need:
# it sets everything up and opens VS Code with the extension, which starts the backend itself.

VENV      := .venv
PY        := $(VENV)/bin/python
PORT      ?= 8000
# Folder opened in the Extension Development Host (the project you want to ask questions about)
WORKSPACE ?= $(CURDIR)

.PHONY: dev setup backend test clean help

help:
	@echo "make dev        setup + open VS Code with the extension (backend auto-starts)"
	@echo "make setup      venv, deps, .env, Ollama + model, compile extension (idempotent)"
	@echo "make backend    setup + run the API in the foreground on PORT=$(PORT)"
	@echo "make test       run the Python tests"
	@echo "make clean      remove the Python venv"

setup:
	@bash scripts/setup.sh

dev: setup
	@command -v code >/dev/null || { echo "VS Code 'code' CLI not found on PATH"; exit 1; }
	code --new-window --extensionDevelopmentPath="$(CURDIR)/extension" "$(WORKSPACE)"
	@echo "VS Code is opening. The LocalPilot backend starts with the extension (Output > LocalPilot Backend)."

backend: setup
	$(PY) -m uvicorn backend.main:app --reload --reload-dir backend --reload-dir compression --host 127.0.0.1 --port $(PORT)

test: setup
	$(PY) -m pytest tests
	$(PY) -m compression.check_contract

clean:
	rm -rf $(VENV)
