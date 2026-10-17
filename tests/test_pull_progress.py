"""Ollama model-download progress: percent tracking from pull-stream chunks and final state kept."""
import asyncio
import json
from unittest import mock

import httpx

from backend import llm

_NAME = "unit-test-model"


def _record(chunks: list[dict]) -> None:
    """Feed chunks through _record_progress the way _run_pull does (layers summed over chunks)."""
    layers: dict[str, tuple[int, int]] = {}
    for chunk in chunks:
        llm._record_progress(_NAME, layers, chunk)


def test_progress_reported_from_chunks():
    _record([
        {"status": "pulling manifest"},
        {"status": "downloading sha256:x", "digest": "sha256:x", "total": 1000, "completed": 100},
        {"status": "downloading sha256:x", "digest": "sha256:x", "total": 1000, "completed": 650},
    ])
    assert llm._pull_progress[_NAME] == {
        "status": "downloading sha256:x", "completed": 650, "total": 1000, "percent": 65.0
    }


def test_percent_kept_when_chunk_has_no_totals():
    # "verifying"... / "success" events carry no digest/totals: layer bytes add up, percent stays.
    _record([
        {"status": "downloading sha256:x", "digest": "sha256:x", "total": 100, "completed": 80},
        {"status": "verifying sha256:x"},
        {"status": "success"},
    ])
    assert llm._pull_progress[_NAME] == {
        "status": "success", "completed": 80, "total": 100, "percent": 80.0
    }


def _stream_client(chunks: list[dict]):
    body = "".join(json.dumps(c) + "\n" for c in chunks).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    real_client = httpx.AsyncClient  # captured before the test patches it
    return lambda **kw: real_client(transport=transport, timeout=kw.get("timeout"))


def test_run_pull_completes_and_keeps_final_state():
    fake = _stream_client([
        {"status": "pulling manifest"},
        {"status": "downloading sha256:x", "total": 10, "completed": 10},
        {"status": "success"},
    ])
    with mock.patch.object(httpx, "AsyncClient", fake):
        asyncio.run(llm._run_pull(_NAME))
    assert llm.pull_status(_NAME) == "installed"
    # The finished entry stays so the sidebar can show 100% for it until it is forgotten.
    assert llm.pull_progress()[_NAME]["state"] == "installed"
    assert llm.pull_progress()[_NAME]["percent"] == 100.0


def test_run_pull_failure_reports_error_and_keeps_state():
    fake = _stream_client([{"status": "downloading", "total": 10, "completed": 2},
                           {"error": "digest mismatch"}])
    with mock.patch.object(httpx, "AsyncClient", fake):
        asyncio.run(llm._run_pull(_NAME))
    assert llm.pull_status(_NAME).startswith("error:")
    assert llm.pull_progress()[_NAME]["state"].startswith("error:")


def test_run_pull_cleans_task_handle():
    fake = _stream_client([{"status": "success"}])
    llm._pulls[_NAME] = "pulling"
    try:
        with mock.patch.object(httpx, "AsyncClient", fake):
            asyncio.run(llm._run_pull(_NAME))
        assert _NAME not in llm._pull_tasks
    finally:
        llm._pulls.pop(_NAME, None)
        llm._pull_progress.pop(_NAME, None)


def test_pull_progress_reports_every_tracked_state():
    llm._pulls[_NAME] = "pulling"
    llm._pull_progress[_NAME] = {"status": "downloading", "completed": 40, "total": 100, "percent": 40.0}
    assert llm.pull_progress()[_NAME]["state"] == "pulling"
    llm._pulls[_NAME] = "cancelled"
    assert llm.pull_progress()[_NAME]["state"] == "cancelled"
    del llm._pulls[_NAME]
