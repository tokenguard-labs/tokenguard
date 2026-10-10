"""Trace one complex query through every stage of the integrated P1+P2 pipeline."""
from __future__ import annotations

import collections
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# This transcript is written against the mock provider; force it before config loads .env
os.environ["CLOUD_PROVIDER"] = "mock"

from backend import config
from backend.tokens import count_tokens
from compression.adapter import optimize_context
from compression.context_engine.engine import ContextEngine
from compression.context_engine.chunker import chunk_repo
from compression.context_engine.config import ContextConfig
from compression.context_engine.imports import build_import_graph, neighbors
from compression.context_engine.redact import redact
from compression.context_engine.selector import extract_terms, rank_chunks
from compression.context_engine.walker import walk_repo

REPO = "/tmp/opencode/repos/requests"
QUERY = (
    "Trace the complete lifecycle of requests.get from the public API "
    "through Session and the transport adapter back to a Response object."
)
BUDGET = config.CLOUD_CONTEXT_TOKEN_BUDGET  # from .env / default 3000

OUT = []


def p(line=""):
    OUT.append(line)
    print(line)


def hr(title):
    p()
    p("=" * 78)
    p(title)
    p("=" * 78)


# STAGE 0 -- request
hr("STAGE 0  REQUEST")
p(f"prompt      : {QUERY}")
p(f"repo_path   : {REPO}")
p(f"code        : (empty)")
p(f"token_budget: {BUDGET}  (config.CLOUD_CONTEXT_TOKEN_BUDGET)")

# STAGE 1 -- ingestion
t0 = time.perf_counter()
cfg = ContextConfig(top_k=8, max_tokens=BUDGET, chunking="symbol", expand_deps=True, redact_secrets=False)
files = walk_repo(REPO, cfg)
by_path = {f.rel_path: f for f in files}
original_tokens = count_tokens("".join(f"# File: {f.rel_path}\n{f.text}" for f in files))
hr("STAGE 1  INGESTION  (walker.walk_repo)")
p(f"source files scanned : {len(files)}")
p(f"excluded by walker   : node_modules/.git/dist/build, binaries, >512KB, .gitignore")
p(f"baseline tokens      : {original_tokens}")
p("files (first 12):")
for f in files[:12]:
    p(f"  - {f.rel_path}  ({f.line_count} lines)")

# STAGE 2 -- chunking
chunks = chunk_repo(files, cfg)
kinds = collections.Counter(c.kind for c in chunks)
t_chunk = time.perf_counter() - t0
hr("STAGE 2  CHUNKING  (chunker.chunk_repo: Python AST / brace paragraphs)")
p(f"total chunks         : {len(chunks)}")
p(f"chunk kinds          : {dict(kinds)}")
chunks_by_file = collections.Counter(c.file for c in chunks)
p("chunkiest files:")
for f, n in chunks_by_file.most_common(6):
    p(f"  - {f}: {n} chunks")

# STAGE 3 -- scoring
ranked = rank_chunks(QUERY, chunks)
terms = extract_terms(QUERY)
positive = [((s, r), c) for ((s, r), c) in ranked if s > 0]
hr("STAGE 3  RELEVANCE SCORING  (selector.rank_chunks)")
p(f"query terms ({len(terms)}): {sorted(terms)}")
p(f"chunks with score > 0 : {len(positive)} / {len(chunks)}")
p("top 8 scored chunks:")
for (s, r), c in sorted(ranked, key=lambda x: -x[0][0])[:8]:
    sym = f"::{c.symbol}" if c.symbol else ""
    p(f"  {s:.3f}  {c.file}{sym} L{c.start_line}-{c.end_line}  [{r[:60]}]")

# STAGE 4 -- seeds + dependency expansion
file_best = {}
for (s, _), c in positive:
    file_best[c.file] = max(file_best.get(c.file, 0.0), s)
seed_files = sorted(file_best, key=lambda f: (-file_best[f], f))[: cfg.top_k]
graph = build_import_graph(files)
edges = sum(len(v) for v in graph.values())
seed_set = set(seed_files)
neighbor_files = sorted(neighbors(seed_set, graph, hops=cfg.dep_hops))
max_dep = max(cfg.top_k, 2 * len(seed_files))
neighbor_files = neighbor_files[:max_dep]
hr("STAGE 4  SEED SELECTION + IMPORT-GRAPH EXPANSION  (engine._select_symbol_mode)")
p(f"import graph edges   : {edges}")
p(f"seed files (top_k={cfg.top_k}):")
for f in seed_files:
    p(f"  - {f}  best={file_best[f]:.3f}")
p(f"dependency neighbors (1 hop, capped at {max_dep}):")
for f in neighbor_files:
    p(f"  - {f}")

engine = ContextEngine()
items, considered = engine._select_symbol_mode(QUERY, files, cfg)
p(f"files considered     : {considered}")
p(f"items after all waves: {len(items)} chunks")

# STAGE 5 -- assembly under budget
repo_context, included, selected_files = engine._assemble(items, by_path, cfg.max_tokens)
hr("STAGE 5  ASSEMBLY UNDER TOKEN BUDGET  (engine._assemble)")
p(f"assembled chunks     : {len(included)}")
p(f"files in context     : {len(selected_files)}")
p(f"budget               : {BUDGET}")
p("included chunks (in priority order):")
for c in included:
    sym = f"::{c.symbol}" if c.symbol else ""
    p(f"  {c.score:.3f}  {c.file}{sym} L{c.start_line}-{c.end_line}  [{c.reason[:52]}]")

# STAGE 6 -- redaction + format
redacted, reds = redact(repo_context)
context = redacted.replace("// file: ", "# File: ")
hr("STAGE 6  REDACTION + OUTPUT FORMAT  (compression.adapter)")
p(f"secret rules hit     : {len(reds)}")
for r in reds:
    p(f"  - {r.rule} in {r.file}:{r.line}")
p(f"header format        : '// file:' -> '# File:'")
p(f"final context tokens : {count_tokens(context)}")

# STAGE 7 -- contract result (what Person 1 receives)
t_opt = time.perf_counter() - t0
result = optimize_context(QUERY, REPO, code="", token_budget=BUDGET)
hr("STAGE 7  CONTRACT RESULT  (compression.optimize_context -> backend.contracts.ContextResult)")
p(f"optimizer            : {result.optimizer}")
p(f"original_tokens      : {result.original_tokens}")
p(f"optimized_tokens     : {result.optimized_tokens}")
saved = 1 - result.optimized_tokens / max(result.original_tokens, 1)
p(f"token reduction      : {saved:.1%}")
p(f"files_selected       : {result.files_selected}")
p(f"secrets_redacted     : {result.secrets_redacted}")
p(f"redaction_details    : {result.redaction_details}")
p(f"optimize_context time: {t_opt*1000:.0f} ms")

# STAGE 8..10 -- end to end through Person 1's API
from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app)
hr("STAGE 8  ROUTING  (POST /analyze  -- no model called)")
an = client.post("/analyze", json={"prompt": QUERY, "repo_path": REPO}).json()
p(f"decision             : {an['decision']}")
p(f"complexity_score     : {an['complexity_score']}")
p(f"task_type            : {an['task_type']}")
p(f"factors              : {an['factors']}")
p(f"reasons              : {an['reasons']}")
p(f"original_tokens      : {an['original_tokens']}")
p(f"estimated_cloud_tokens: {an['estimated_cloud_tokens']}")
p(f"context_optimizer    : {an['context_optimizer']}")
p(f"files_selected       : {an['files_selected']}")
p(f"secrets_redacted     : {an['secrets_redacted']}")

hr("STAGE 9  END TO END  (POST /chat, force cloud, mock provider)")
ch = client.post("/chat", json={"prompt": QUERY, "repo_path": REPO, "force_route": "cloud"}).json()
p(f"route                : {ch['route']}")
p(f"model                : {ch['model']}")
p(f"context_optimizer    : {ch['context_optimizer']}")
p(f"escalated            : {ch['escalated']}")
p(f"original_tokens      : {ch['original_tokens']}")
p(f"sent_tokens          : {ch['sent_tokens']}")
p(f"tokens_saved         : {ch['tokens_saved']}")
p(f"files_selected       : {ch['files_selected']}")
p(f"secrets_redacted     : {ch['secrets_redacted']}")
p(f"latency_ms           : {ch['latency_ms']}")
p(f"trace                : {ch['trace']}")
p(f"answer (first 160)   : {ch['answer'][:160].replace(chr(10), ' ')}")

hr("STAGE 10  DASHBOARD  (GET /stats)")
st = client.get("/stats").json()
p(str({k: v for k, v in st.items() if k != "history"}))

out_path = os.path.join(_ROOT, "docs", "pipeline-trace.md")
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("# Pipeline Trace: one complex query, end to end\n\n")
    fh.write("```text\n" + "\n".join(OUT) + "\n```\n")
print(f"\n[saved transcript to {out_path}]")
