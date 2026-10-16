"""Ollama model-download progress: percent tracking from pull-stream chunks and cleanup."""
import asyncio
import json
from unittest import mock

import httpx

from backend import llm

_NAME = "unit-test-model"


def _absorb(chunks: list[dict]) -> None:
    for chunk in chunks:
        llm._absorb_pull_chunk(_NAME, chunk)


def test_progress_reported_from_chunks():
    _absorb([
        {"status": "pulling manifest"},
        {"status": "downloading sha256:x", "digest": "sha256:x", "total": 1000, "completed": 100},
        {"status": "downloading sha256:x", "total": 1000, "completed": 650},
    ])
    assert llm._pull_progress[_NAME] == {"percent": 65, "status": "downloading sha256:x"}


def test_percent_kept_when_chunk_has_no_totals():
    # "verifying"... / "success" events carry no total/completed: keep the last percent.
    _absorb([
        {"status": "downloading sha256:x", "total": 100, "completed": 80},
        {"status": "verifying sha256:x"},
        {"status": "success"},
    ])
    assert llm._pull_progress[_NAME] == {"percent": 80, "status": "success"}


def _stream_client(chunks: list[dict], delay_s: float = 0.0):
    body = "".join(json.dumps(c) + "\n" for c in chunks).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    real_client = httpx.AsyncClient  # captured before the test patches it
    return lambda **kw: real_client(transport=transport, timeout=kw.get("timeout"))


def test_run_pull_completes_and_cleans_progress():
    fake = _stream_client([
        {"status": "pulling manifest"},
        {"status": "downloading sha256:x", "total": 10, "completed": 10},
        {"status": "success"},
    ])
    with mock.patch.object(httpx, "AsyncClient", fake):
        asyncio.run(llm._run_pull(_NAME))
    assert llm.pull_status(_NAME) == "installed"
    assert _NAME not in llm._pull_progress


def test_run_pull_failure_cleans_progress_and_reports_error():
    fake = _stream_client([{"status": "downloading", "total": 10, "completed": 2},
                           {"error": "digest mismatch"}])
    with mock.patch.object(httpx, "AsyncClient", fake):
        asyncio.run(llm._run_pull(_NAME))
    assert llm.pull_status(_NAME).startswith("error:")
    assert _NAME not in llm._pull_progress


def test_pull_progress_only_lists_active_pulls():
    llm._pulls[_NAME] = "pulling"
    llm._pull_progress[_NAME] = {"percent": 40, "status": "downloading"}
    assert llm.pull_progress()[_NAME]["percent"] == 40
    llm._pulls[_NAME] = "installed"
    assert _NAME not in llm.pull_progress()
    del llm._pulls[_NAME]
