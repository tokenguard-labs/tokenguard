"""Quick manual test of the LocalPilot API. Start the server first (`make backend`), then:
    python scripts/smoke_test.py          # TOKENGUARD_API_URL overrides http://localhost:8000
"""
import os

import requests

API = os.getenv("TOKENGUARD_API_URL", "http://localhost:8000").rstrip("/")
REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")

CASES = [
    {"prompt": "Write a Python one-liner to reverse a string"},
    {"prompt": "Explain what this function does", "code": "def f(xs): return {x: xs.count(x) for x in set(xs)}"},
    {"prompt": "Refactor the routing architecture across the codebase to support multiple cloud providers "
               "and explain the trade-offs", "repo_path": REPO},
]

for case in CASES:
    r = requests.post(f"{API}/chat", json=case, timeout=300).json()
    print(f"\n> {case['prompt']}")
    print(f"  route={r['route']} score={r['complexity_score']} conf={r['confidence']} escalated={r['escalated']}")
    print(f"  tokens: original={r['original_tokens']} sent={r['sent_tokens']} saved={r['tokens_saved']}")
    print(f"  latency={r['latency_ms']}ms  reasons={r['reasons']}")
    print("  answer:", r["answer"][:200].replace("\n", " "), "...")

print("\n/stats:", {k: v for k, v in requests.get(f"{API}/stats").json().items() if k != "history"})
