"""Local model switching: a load that fails or times out must put the previous model back."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import importlib  # noqa: E402

from backend import config, llm, model_catalog, system  # noqa: E402


class TestSwitchLocalModel(unittest.TestCase):
    def _switch(self, preload, start_loaded, model="new", fallback=None, timeout_s=0.05):
        unloads = []

        async def unload(name):
            unloads.append(name)

        with patch.object(llm, "_ollama_preload", new=AsyncMock(side_effect=preload)), \
                patch.object(llm, "_ollama_unload", new=AsyncMock(side_effect=unload)), \
                patch.object(llm, "_loaded_local_model", start_loaded):
            result = asyncio.run(llm.switch_local_model(model, timeout_s=timeout_s, fallback=fallback))
            loaded = llm.loaded_local_model()
        return result, loaded, unloads

    def test_successful_switch(self):
        async def preload(name):
            pass

        result, loaded, unloads = self._switch(preload, "old")
        self.assertEqual(result, {"loaded": "new", "previous": "old"})
        self.assertEqual(loaded, "new")
        self.assertEqual(unloads, ["old"])

    def test_timeout_reverts_to_previous(self):
        async def preload(name):
            if name == "new":
                await asyncio.sleep(5)  # slower than the timeout

        result, loaded, unloads = self._switch(preload, "old")
        self.assertTrue(result["timed_out"])
        self.assertIn("longer than", result["error"])
        self.assertEqual(result["reverted_to"], "old")
        self.assertEqual(loaded, "old")
        self.assertEqual(unloads, ["old", "new"])  # the half-loaded model is evicted too

    def test_failure_uses_fallback_when_nothing_was_loaded(self):
        async def preload(name):
            if name == "new":
                raise RuntimeError("boom")

        result, loaded, _ = self._switch(preload, None, fallback="saved")
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["reverted_to"], "saved")
        self.assertEqual(loaded, "saved")

    def test_failed_revert_leaves_nothing_loaded(self):
        async def preload(name):
            raise RuntimeError("ollama down")

        result, loaded, _ = self._switch(preload, "old")
        self.assertIsNone(result["reverted_to"])
        self.assertIsNone(loaded)


class TestWindowsMemory(unittest.TestCase):
    def test_windows_branch_fills_ram_and_swap(self):
        kib = 1024 * 1024  # 1 GB in KiB
        fake = {"MemTotal": 16 * kib, "MemAvailable": 6 * kib, "SwapTotal": 4 * kib, "SwapFree": 3 * kib}
        with patch.object(system.os.path, "exists", return_value=False), \
                patch.object(system.os, "name", "nt"), \
                patch.object(system, "_windows_meminfo", return_value=fake), \
                patch.object(system, "_gpu_vram_gb", return_value=0.0):
            info = system.system_resources(force=True)
        system._cache = None
        self.assertEqual(info["total_ram_gb"], 16.0)
        self.assertEqual(info["available_ram_gb"], 6.0)
        self.assertEqual(info["swap_total_gb"], 4.0)
        self.assertEqual(info["swap_free_gb"], 3.0)

    def test_windows_meminfo_is_empty_off_windows(self):
        if os.name == "nt":
            self.skipTest("runs the real API on Windows")
        self.assertEqual(system._windows_meminfo(), {})


class TestWindowsOllamaDiscovery(unittest.TestCase):
    def _url_for(self, value):
        with patch.dict(os.environ, {"OLLAMA_URL": value}):
            return importlib.reload(config).OLLAMA_URL

    def tearDown(self):
        importlib.reload(config)

    def test_localhost_becomes_ipv4_loopback(self):
        self.assertEqual(self._url_for("http://localhost:11434/"), "http://127.0.0.1:11434")
        self.assertEqual(self._url_for("http://localhost"), "http://127.0.0.1")
        self.assertEqual(self._url_for("http://localhost.example:11434"), "http://localhost.example:11434")
        self.assertEqual(self._url_for("http://10.0.0.5:11434"), "http://10.0.0.5:11434")

    def test_loopback_bypasses_proxies(self):
        with patch.dict(os.environ, {"NO_PROXY": "corp.example"}):
            importlib.reload(config)
            hosts = os.environ["NO_PROXY"].split(",")
        self.assertIn("corp.example", hosts)
        self.assertTrue({"localhost", "127.0.0.1", "::1"} <= set(hosts))

    def test_unknown_ram_still_offers_models(self):
        recs = model_catalog.recommend({"total_ram_gb": 0, "swap_total_gb": 0, "gpu_vram_gb": 0})
        self.assertTrue(recs and all(m["fits"] for m in recs))
        self.assertIn("couldn't detect", recs[0]["note"])


if __name__ == "__main__":
    unittest.main()
