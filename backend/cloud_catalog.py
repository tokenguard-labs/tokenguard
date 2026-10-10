"""Curated cloud-model metadata: display names, context windows and token prices.

Provider APIs list model ids but not their context window or price, so we keep a small overlay
keyed by model id. Prices are USD per 1M tokens and are estimates kept for guidance only;
`PRICES_UPDATED` records when they were last reviewed. Unknown ids still work — they just have
no metadata.
"""
from typing import Dict, List, Optional, Tuple

PRICES_UPDATED = "2026-10"

# provider -> {"default": model_id, "models": {model_id: {...}}}
CLOUD_CATALOG: Dict[str, Dict] = {
    "deepseek": {
        "default": "deepseek-chat",
        "models": {
            "deepseek-chat": {
                "display": "DeepSeek-V3 (chat)", "context": 64000, "in": 0.27, "out": 1.10,
                "caps": ["tools"], "tags": ["coding", "general"],
                "note": "Best all-round value", "recommended": True,
            },
            "deepseek-reasoner": {
                "display": "DeepSeek-R1 (reasoner)", "context": 64000, "in": 0.55, "out": 2.19,
                "caps": ["reasoning"], "tags": ["reasoning"],
                "note": "Chain-of-thought reasoning", "recommended": True,
            },
        },
    },
    "openai": {
        "default": "gpt-4o-mini",
        "models": {
            "gpt-4o-mini": {
                "display": "GPT-4o mini", "context": 128000, "in": 0.15, "out": 0.60,
                "caps": ["tools", "vision"], "tags": ["cheap", "general"],
                "note": "Cheapest capable default", "recommended": True,
            },
            "gpt-4o": {
                "display": "GPT-4o", "context": 128000, "in": 2.50, "out": 10.00,
                "caps": ["tools", "vision"], "tags": ["general"],
                "note": "Stronger, pricier", "recommended": False,
            },
            "o3-mini": {
                "display": "o3-mini", "context": 200000, "in": 1.10, "out": 4.40,
                "caps": ["reasoning"], "tags": ["reasoning"],
                "note": "Reasoning at lower cost", "recommended": False,
            },
        },
    },
    "anthropic": {
        "default": "claude-sonnet-5-5",
        "models": {
            "claude-sonnet-5-5": {
                "display": "Claude Sonnet 5.5", "context": 200000, "in": 3.00, "out": 15.00,
                "caps": ["tools", "vision"], "tags": ["coding", "general"],
                "note": "Balanced quality", "recommended": True,
            },
            "claude-haiku-4-5": {
                "display": "Claude Haiku 4.5", "context": 200000, "in": 1.00, "out": 5.00,
                "caps": ["tools"], "tags": ["cheap"],
                "note": "Fast and cheap", "recommended": False,
            },
        },
    },
    # "custom" endpoints (Groq/OpenRouter/self-hosted) have unknown prices/context.
    "custom": {"default": "", "models": {}},
}


def default_model(provider: str) -> str:
    return CLOUD_CATALOG.get(provider, {}).get("default", "")


def model_info(provider: str, model: str) -> Optional[Dict]:
    """Curated metadata for a model id, or None when we have none."""
    entry = CLOUD_CATALOG.get(provider, {}).get("models", {}).get(model)
    if not entry:
        return None
    return {"id": model, **entry, "known": True}


def recommended(provider: str) -> List[Dict]:
    out = []
    for model, entry in CLOUD_CATALOG.get(provider, {}).get("models", {}).items():
        if entry.get("recommended"):
            out.append({"id": model, **entry, "known": True})
    return out


def all_models(provider: str) -> List[Dict]:
    """Every curated model for a provider (used when there is no live list)."""
    return [{"id": model, **entry, "known": True}
            for model, entry in CLOUD_CATALOG.get(provider, {}).get("models", {}).items()]


def price(provider: str, model: str) -> Tuple[Optional[float], Optional[float]]:
    """(input, output) USD per 1M tokens, or (None, None) when unknown."""
    entry = CLOUD_CATALOG.get(provider, {}).get("models", {}).get(model)
    if not entry or entry.get("in") is None or entry.get("out") is None:
        return None, None
    return float(entry["in"]), float(entry["out"])


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    in_price, out_price = price(provider, model)
    if in_price is None or out_price is None:
        return None
    return round(input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price, 6)


def merge(provider: str, ids: List[str]) -> List[Dict]:
    """Merge provider-reported ids with the curated overlay; unknown ids get minimal metadata."""
    known = CLOUD_CATALOG.get(provider, {}).get("models", {})
    seen = set()
    out: List[Dict] = []
    for model in ids:
        if model in seen:
            continue
        seen.add(model)
        info = model_info(provider, model)
        if info:
            out.append(info)
        else:
            out.append({
                "id": model, "display": model, "context": None, "in": None, "out": None,
                "caps": [], "tags": [], "note": "", "recommended": False, "known": False,
            })
    return out
