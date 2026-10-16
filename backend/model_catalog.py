"""Curated Ollama models worth offering for a coding assistant, with rough resource needs.

`size_gb` is the approximate download size and `min_ram_gb` the RAM (or VRAM) we'd want for a
comfortable CPU/GPU run. These are estimates for guidance, not guarantees.
"""
from typing import Dict, List

CATALOG: List[Dict] = [
    {"name": "qwen2.5-coder:0.5b", "params": "0.5B", "size_gb": 0.4, "min_ram_gb": 2,
     "tags": ["coding", "fast"], "description": "Tiny coding model; runs on almost anything."},
    {"name": "qwen2.5-coder:1.5b", "params": "1.5B", "size_gb": 1.0, "min_ram_gb": 3,
     "tags": ["coding", "fast"], "description": "Fast small coding model (project default)."},
    {"name": "gemma3:1b", "params": "1B", "size_gb": 0.8, "min_ram_gb": 3,
     "tags": ["general"], "description": "Google's compact general-purpose model."},
    {"name": "llama3.2:1b", "params": "1B", "size_gb": 1.3, "min_ram_gb": 3,
     "tags": ["general"], "description": "Very small, fast instruction model."},
    {"name": "qwen2.5-coder:3b", "params": "3B", "size_gb": 1.9, "min_ram_gb": 4,
     "tags": ["coding"], "description": "Balanced coding model for light machines."},
    {"name": "llama3.2:3b", "params": "3B", "size_gb": 2.0, "min_ram_gb": 4,
     "tags": ["general"], "description": "Small model with solid instruction following."},
    {"name": "qwen3:4b", "params": "4B", "size_gb": 2.6, "min_ram_gb": 6,
     "tags": ["general", "reasoning"], "description": "Compact reasoning + general model."},
    {"name": "gemma3:4b", "params": "4B", "size_gb": 3.3, "min_ram_gb": 6,
     "tags": ["general"], "description": "Strong small general model."},
    {"name": "qwen2.5-coder:7b", "params": "7B", "size_gb": 4.7, "min_ram_gb": 8,
     "tags": ["coding"], "description": "Strong coding model; best with 8GB+."},
    {"name": "deepseek-r1:8b", "params": "8B", "size_gb": 5.2, "min_ram_gb": 10,
     "tags": ["reasoning"], "description": "Reasoning model distilled from DeepSeek-R1."},
    {"name": "qwen3:8b", "params": "8B", "size_gb": 5.2, "min_ram_gb": 10,
     "tags": ["general", "reasoning"], "description": "8B general/reasoning model."},
    {"name": "qwen2.5-coder:14b", "params": "14B", "size_gb": 9.0, "min_ram_gb": 16,
     "tags": ["coding"], "description": "High-quality coding; wants 16GB+."},
    {"name": "deepseek-coder-v2:16b", "params": "16B", "size_gb": 8.9, "min_ram_gb": 16,
     "tags": ["coding"], "description": "Large coding expert; wants 16GB+."},
    {"name": "qwen2.5-coder:32b", "params": "32B", "size_gb": 20.0, "min_ram_gb": 32,
     "tags": ["coding"], "description": "Best local coding quality; wants 32GB+."},
]


def _annotate(item: Dict, resources: Dict) -> None:
    """Fill in `fits`/`comfortable`/`note` for one annotated catalog entry."""
    total_ram = float(resources.get("total_ram_gb") or 0)
    swap = float(resources.get("swap_total_gb") or 0)
    vram = float(resources.get("gpu_vram_gb") or 0)
    budget = max(vram, total_ram + swap)
    fast_budget = max(vram, total_ram)
    need = float(item["min_ram_gb"])
    item["fits"] = need <= budget
    item["comfortable"] = need <= fast_budget
    if not item["fits"]:
        item["note"] = f"needs ~{item['min_ram_gb']}GB RAM/VRAM — more than this machine"
    elif not item["comfortable"]:
        item["note"] = "may use swap — slower; close other apps for best speed"
    else:
        item["note"] = ""


def fit(name: str, size_gb: float, resources: Dict) -> Dict:
    """RAM-fit info for one installed model, for warnings in the sidebar.

    Catalog entries carry a curated min RAM; unknown models are estimated from their disk
    size (quantized GGUF needs a little more memory than it occupies, plus KV cache).
    """
    entry = next((m for m in CATALOG if m["name"] == name), None)
    if entry:
        item = dict(entry)
    else:
        need = round(size_gb * 1.3 + 1)
        item = {"name": name, "params": "?", "size_gb": size_gb, "min_ram_gb": max(1, need),
                "tags": [], "description": ""}
    _annotate(item, resources)
    return {k: item[k] for k in ("size_gb", "min_ram_gb", "fits", "comfortable", "note")}


def recommend(resources: Dict) -> List[Dict]:
    """Annotate the catalog with fit information for the given `system_resources()`.

    `fits` is True when the model can run using RAM + swap (or the GPU VRAM); `comfortable`
    is True when it fits in RAM/VRAM alone (no swap). Only models that fit should be offered.
    """
    out = []
    for model in CATALOG:
        item = dict(model)
        _annotate(item, resources)
        out.append(item)
    return out
