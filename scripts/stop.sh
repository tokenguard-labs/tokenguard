#!/usr/bin/env bash
# Stop everything TokenGuard may have started when it is NOT run through the VS Code extension
# (e.g. `make backend`, or a crashed session):
#   1. unload every model Ollama holds in memory (frees RAM/VRAM)
#   2. stop the `ollama serve` that setup.sh launched (recorded in .ollama.pid)
# An Ollama that was already running (systemd / your own `ollama serve`) is left alone, matching the
# extension's policy. Use `ollama stop` yourself if you want to stop an external one.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"

env_get() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$BACKEND/.env" 2>/dev/null | tail -n1 | tr -d "\"'\r"; }
OLLAMA_URL="${OLLAMA_URL:-$(env_get OLLAMA_URL)}"; OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

# 1. Unload resident models.
if curl -sf --max-time 3 "$OLLAMA_URL/api/ps" >/dev/null 2>&1; then
    names="$(curl -s --max-time 3 "$OLLAMA_URL/api/ps" | python3 -c \
        'import sys,json; print("\n".join((m.get("name") or m.get("model") or "") for m in json.load(sys.stdin).get("models", [])))' 2>/dev/null || true)"
    if [ -n "${names//[[:space:]]/}" ]; then
        while read -r model; do
            [ -n "$model" ] && curl -s --max-time 5 -X POST "$OLLAMA_URL/api/generate" \
                -H 'Content-Type: application/json' \
                -d "{\"model\":\"$model\",\"keep_alive\":0}" >/dev/null 2>&1
        done <<< "$names"
        echo "  unloaded model(s): $(echo "$names" | tr '\n' ' ')"
    else
        echo "  no models were loaded"
    fi
else
    echo "  Ollama not reachable at $OLLAMA_URL"
fi

# 2. Stop the Ollama that setup.sh started (never an external one).
if [ -f "$ROOT/.ollama.pid" ]; then
    pid="$(cat "$ROOT/.ollama.pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        if [ -r "/proc/$pid/comm" ] && ! grep -qi ollama "/proc/$pid/comm"; then
            echo "  refusing to kill pid $pid (not ollama)"
        else
            kill "$pid" 2>/dev/null || true
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" 2>/dev/null || true; fi
            echo "  stopped ollama serve (pid $pid)"
        fi
    fi
    rm -f "$ROOT/.ollama.pid"
else
    echo "  no .ollama.pid marker (Ollama was external, or already stopped)"
fi
