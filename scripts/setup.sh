#!/usr/bin/env bash
# One-shot, idempotent setup for TokenGuard. Safe to re-run: every step is skipped when already done.
#
#   1. Python venv at .venv/ with the gateway + test dependencies (reinstalled only when requirements change)
#   2. localpilot/.env from .env.example (never overwrites an existing file)
#   3. Ollama: start `ollama serve` if it isn't reachable, pull LOCAL_MODEL if it's missing
#   4. VS Code extension: npm install (when needed) + compile
#
# Env knobs: PYTHON=python3.11  SKIP_OLLAMA=1  SKIP_EXTENSION=1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
BACKEND="$ROOT/localpilot"
EXTENSION="$ROOT/tokenguard"
PYTHON="${PYTHON:-python3}"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '    \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

hash_files() { cat "$@" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d' ' -f1; }

# --- 1. Python venv -------------------------------------------------------------------------
step "Python environment (.venv)"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found. Install Python 3.9+ or set PYTHON=/path/to/python3"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9+ required ($("$PYTHON" --version))"

if [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON" -m venv "$VENV" || die "venv creation failed (on Debian/Ubuntu: sudo apt install python3-venv)"
    ok "created $VENV"
else
    ok "exists"
fi

REQ_HASH="$(hash_files "$ROOT/requirements.txt" "$BACKEND/requirements.txt")"
STAMP="$VENV/.requirements.sha256"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$REQ_HASH" ]; then
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"
    echo "$REQ_HASH" > "$STAMP"
    ok "dependencies installed"
else
    ok "dependencies up to date"
fi

# --- 2. .env ----------------------------------------------------------------------------------
step "Backend config (localpilot/.env)"
if [ ! -f "$BACKEND/.env" ]; then
    cp "$BACKEND/.env.example" "$BACKEND/.env"
    ok "created from .env.example (cloud calls fall back to the mock until you set DEEPSEEK_API_KEY)"
else
    ok "exists"
fi

# Read a key from .env without sourcing it (values may contain shell-special characters)
env_get() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$BACKEND/.env" | tail -n1 | tr -d "\"'\r"; }

# --- 3. Ollama --------------------------------------------------------------------------------
step "Ollama (local LLM)"
OLLAMA_URL="${OLLAMA_URL:-$(env_get OLLAMA_URL)}"; OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
LOCAL_MODEL="${LOCAL_MODEL:-$(env_get LOCAL_MODEL)}"; LOCAL_MODEL="${LOCAL_MODEL:-qwen2.5-coder:1.5b}"

ollama_up() { curl -sf --max-time 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; }

if [ "${SKIP_OLLAMA:-0}" = "1" ]; then
    warn "skipped (SKIP_OLLAMA=1)"
elif ! command -v ollama >/dev/null; then
    warn "ollama not installed: local answers won't work. Install from https://ollama.com and re-run."
else
    if ollama_up; then
        ok "running at $OLLAMA_URL"
    else
        nohup ollama serve > "$ROOT/.ollama.log" 2>&1 &
        for _ in $(seq 1 30); do ollama_up && break; sleep 0.5; done
        if ollama_up; then ok "started ollama serve (log: .ollama.log)"; else warn "could not start ollama (see .ollama.log)"; fi
    fi

    if ollama_up; then
        want="$LOCAL_MODEL"; [[ "$want" == *:* ]] || want="$want:latest"
        if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qxF "$want"; then
            ok "model $LOCAL_MODEL present"
        else
            echo "    pulling $LOCAL_MODEL (one-time download)…"
            if ollama pull "$LOCAL_MODEL"; then ok "model $LOCAL_MODEL pulled"; else warn "pull failed for $LOCAL_MODEL"; fi
        fi
    fi
fi

# --- 4. VS Code extension ----------------------------------------------------------------------
step "VS Code extension (tokenguard/)"
if [ "${SKIP_EXTENSION:-0}" = "1" ]; then
    warn "skipped (SKIP_EXTENSION=1)"
elif ! command -v npm >/dev/null; then
    [ -f "$EXTENSION/out/extension.js" ] || die "npm not found and tokenguard/out/ is missing. Install Node.js 18+."
    warn "npm not found: using the prebuilt tokenguard/out/"
else
    NPM_HASH="$(hash_files "$EXTENSION/package.json" "$EXTENSION/package-lock.json")"
    NPM_STAMP="$EXTENSION/node_modules/.install.sha256"
    if [ "$(cat "$NPM_STAMP" 2>/dev/null)" != "$NPM_HASH" ]; then
        (cd "$EXTENSION" && npm install --no-audit --no-fund --loglevel=error)
        # npm may rewrite package-lock.json, so hash after installing
        hash_files "$EXTENSION/package.json" "$EXTENSION/package-lock.json" > "$NPM_STAMP"
        ok "npm dependencies installed"
    else
        ok "npm dependencies up to date"
    fi
    (cd "$EXTENSION" && npm run --silent compile)
    ok "compiled"
fi

step "Setup complete"
