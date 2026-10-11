"""Backend shutdown must free every local model it (or its helpers) caused Ollama to load."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from backend import llm  # noqa: E402


class TestShutdown(unittest.TestCase):
    def test_parse_ps(self):
        self.assertEqual(llm.parse_ps({"models": [{"name": "a"}, {"model": "b"}, {}]}), ["a", "b"])
        self.assertEqual(llm.parse_ps({}), [])

    def test_unload_all_resident_models_on_exit(self):
        calls = []

        async def record(model):
            calls.append(model)

        with patch.object(llm, "loaded_models", new=AsyncMock(return_value=["m1", "m2"])), \
                patch.object(llm, "_ollama_unload", new=AsyncMock(side_effect=record)), \
                patch.object(llm, "_loaded_local_model", "m3"):
            asyncio.run(llm.unload_local_model_on_exit())
        # every model Ollama reported loaded, plus the one call_local tracked, is unloaded
        self.assertEqual(set(calls), {"m1", "m2", "m3"})

    def test_unload_is_best_effort_when_unreachable(self):
        with patch.object(llm, "loaded_models", new=AsyncMock(return_value=[])), \
                patch.object(llm, "_loaded_local_model", None), \
                patch.object(llm, "_ollama_unload", new=AsyncMock()) as unload:
            asyncio.run(llm.unload_local_model_on_exit())
        unload.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
