"""RAM-fit annotations: catalog entries, unknown models (size estimate), and the mounted machine."""
from backend import model_catalog


def test_catalog_models_annotated():
    resources = {"total_ram_gb": 16, "swap_total_gb": 8, "gpu_vram_gb": 0}
    recs = {m["name"]: m for m in model_catalog.recommend(resources)}
    assert recs["qwen2.5-coder:1.5b"]["fits"] is True
    assert recs["qwen2.5-coder:1.5b"]["comfortable"] is True
    assert recs["qwen2.5-coder:7b"]["comfortable"] is True
    assert recs["qwen3:4b"]["note"] == ""
    # Wants ~16 GB: exceeds RAM alone but fits using swap.
    swap_resources = {"total_ram_gb": 12, "swap_total_gb": 8, "gpu_vram_gb": 0}
    swap_recs = {m["name"]: m for m in model_catalog.recommend(swap_resources)}
    assert swap_recs["qwen2.5-coder:14b"]["fits"] is True
    assert swap_recs["qwen2.5-coder:14b"]["comfortable"] is False
    assert "swap" in swap_recs["qwen2.5-coder:14b"]["note"]


def test_fit_known_model_uses_catalog_needs():
    f = model_catalog.fit("qwen3:4b", 99.0, {"total_ram_gb": 8, "swap_total_gb": 0, "gpu_vram_gb": 0})
    # Curated need wins over the disk-size estimate.
    assert f["min_ram_gb"] == 6
    assert f["fits"] is True
    assert f["comfortable"] is True


def test_fit_unknown_model_estimated_from_size():
    # ~6 GB disk -> needs ~9 GB RAM: within RAM + swap, above RAM alone.
    f = model_catalog.fit("custom-llama:8b", 6.0, {"total_ram_gb": 8, "swap_total_gb": 4, "gpu_vram_gb": 0})
    assert f["min_ram_gb"] == round(6.0 * 1.3 + 1)
    assert f["fits"] is True
    assert f["comfortable"] is False


def test_fit_too_big_for_machine():
    f = model_catalog.fit("qwen2.5-coder:32b", 20.0, {"total_ram_gb": 7.5, "swap_total_gb": 8, "gpu_vram_gb": 0})
    assert f["fits"] is False
    assert f["comfortable"] is False
    assert "more than this machine" in f["note"]


def test_fit_gpu_vram_counts():
    f = model_catalog.fit("qwen2.5-coder:32b", 20.0, {"total_ram_gb": 8, "swap_total_gb": 0, "gpu_vram_gb": 40})
    assert f["fits"] is True
    assert f["comfortable"] is True
