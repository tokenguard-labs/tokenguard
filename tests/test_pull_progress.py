"""Ollama model-download progress: byte totals summed over layers, final state, errors."""
import asyncio
import json
from unittest import mock

import httpx

from backend import llm

_NAME = "unit-test-model"


def _record(chunks: list[dict]) -> None:
    layers: dict[str, tuple[int, int]] = {}
    for chunk in chunks:
        llm._record_progress(_NAME, layers, chunk)


def test_progress_sums_bytes_over_layers():
    _record([
        {"status": "pulling manifest"},
        {"status": "downloading sha256:a", "digest": "sha256:a", "total": 1000, "completed": 100},
        {"status": "downloading sha256:b", "digest": "sha256:b", "total": 1000, "completed": 0},
        {"status": "downloading sha256:a", "digest": "sha256:a", "total": 1000, "completed": 1000},
        {"status": "downloading sha256:b", "digest": "sha256:b", "total": 1000, "completed": 300},
    ])
    assert llm._pull_progress[_NAME] == {
        "status": "downloading sha256:b", "completed": 1300, "total": 2000, "percent": 65.0}


def test_bytes_kept_when_chunk_has_no_totals():
    # "verifying"... / "success" events carry no digest/total: keep the bytes, update the status.
    _record([
        {"status": "downloading sha256:x", "digest": "sha256:x", "total": 100, "completed": 80},
        {"status": "verifying sha256:x"},
        {"status": "success"},
    ])
    assert llm._pull_progress[_NAME] == {"status": "success", "completed": 80, "total": 100, "percent": 80.0}


def _stream_client(chunks: list[dict]):
    body = "".join(json.dumps(c) + "\n" for c in chunks).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    real_client = httpx.AsyncClient  # captured before the test patches it
    return lambda **kw: real_client(transport=transport, timeout=kw.get("timeout"))


def test_run_pull_completes_at_100_percent():
    fake = _stream_client([
        {"status": "pulling manifest"},
        {"status": "downloading sha256:x", "digest": "sha256:x", "total": 10, "completed": 7},
        {"status": "success"},
    ])
    with mock.patch.object(httpx, "AsyncClient", fake):
        asyncio.run(llm._run_pull(_NAME))
    assert llm.pull_status(_NAME) == "installed"
    assert llm.pull_progress()[_NAME]["percent"] == 100.0
    assert llm.pull_progress()[_NAME]["state"] == "installed"
    del llm._pulls[_NAME]


def test_run_pull_failure_reports_error():
    fake = _stream_client([{"status": "downloading", "digest": "sha256:x", "total": 10, "completed": 2},
                           {"error": "digest mismatch"}])
    with mock.patch.object(httpx, "AsyncClient", fake):
        asyncio.run(llm._run_pull(_NAME))
    assert llm.pull_status(_NAME).startswith("error:")
    assert llm.pull_progress()[_NAME]["state"].startswith("error:")
    del llm._pulls[_NAME]


def test_pull_progress_reports_state_of_each_pull():
    llm._pulls[_NAME] = "pulling"
    llm._pull_progress[_NAME] = {"status": "downloading", "completed": 40, "total": 100, "percent": 40.0}
    assert llm.pull_progress()[_NAME] == {
        "status": "downloading", "completed": 40, "total": 100, "percent": 40.0, "state": "pulling"}
    del llm._pulls[_NAME]
    assert _NAME not in llm.pull_progress()
