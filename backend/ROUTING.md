# LocalPilot: How the Router Decides Local vs Cloud

The router answers two questions for every coding request:

> **Can the small model on my laptop handle this, or do we need to pay for a big cloud model?**
> **If it answered, is the answer good enough to keep, or should we escalate?**

It keeps three decisions apart:

1. **Routing preference** — what the user asked for (`auto` / `local` / `cloud`). An explicit
   `local` is never silently overridden by automatic escalation; a privacy restriction is never
   overridden by routing optimization.
2. **Before calling any model** — hard policy, **capability checks**, then a complexity score.
3. **After calling the local model** — validate the answer and check its confidence; escalate only
   when warranted.

Everything is rule-based: no training and no extra ML model. Every decision comes with plain-English
`reasons`. The full policy spec (capabilities, weights, categories, escalation kinds, configuration)
is in [`docs/routing-policy.md`](../docs/routing-policy.md); the decision code lives in
[`backend/routing.py`](routing.py).

---

## The full flow

```text
Request (prompt + optional code + optional repo_path, force_route)
   │
   ▼
1. Context optimizer: pick the relevant files (≤ CLOUD_CONTEXT_TOKEN_BUDGET)
   │
   ▼
2. RoutePlan (routing.plan_route)
   A. Hard policy:  local-only / cloud-only / privacy / cloud-enabled
   B. Capability:   Ollama reachable? needs tools? relevant context fits the local window? multi-file?
   C. Complexity:   weighted score 0..100 → SIMPLE / MEDIUM / COMPLEX
   │
   ├─ local_only / cloud_only       → forced
   ├─ capable + simple/medium       → LOCAL FIRST (escalate if unsure)
   ├─ complex or not local-capable  → CLOUD FIRST (when permitted)
   └─ cloud prohibited              → local, explain the limitation
   │
   ├─ LOCAL FIRST ─► Ollama (LOCAL_MODEL)
   │                  returns {answer, confidence, needs_cloud}
   │                  │
   │                  ▼
   │                3. Escalation check (routing.decide_escalation)
   │                  failure → error · malformed → validation · needs_cloud · low/missing confidence
   │                  │
   │                  ├─ no  → return local answer ✅
   │                  └─ yes → ESCALATE to cloud (≤ MAX_CLOUD_ESCALATIONS, context preserved)
   │
   └─ CLOUD / CLOUD FIRST ─► Cloud LLM with the optimized, redacted context only
   │
   ▼
4. Response + routing metadata (requested/initial/final route, complexity, reasons, tokens, latency)
```

Code: [`backend/analyzer.py`](analyzer.py) (scoring), [`backend/routing.py`](routing.py) (policy),
[`backend/router.py`](router.py) (pipeline), [`backend/llm.py`](llm.py) (model calls and confidence).

---

## Stage 1: The complexity score

The score is a weighted sum of five factors. Each factor is between 0 and 1 (weights are
configurable heuristics, see `COMPLEXITY_WEIGHTS`):

```text
weighted = 0.25 × task_type  +  0.20 × context_size  +  0.20 × files  +  0.20 × dependency  +  0.15 × reasoning
score    = max(weighted, 0.8 × task_type)        ← "strong task signal" floor (only when complex keywords matched)

complexity_score_100 = 100 × score    →  SIMPLE (≤34) · MEDIUM (35–64) · COMPLEX (≥65)
```

`context_size` uses the **relevant** (optimized) context, not the whole repository. `dependency`
captures cross-file scope (imports, modules, "throughout the codebase"); the strategy here is
capability-first, not score-first — see the next stage.

**Why the floor?** Without it, a prompt with no code attached has context_size = files = 0, so it could never score above 0.6. That means it could never go to the cloud, however hard the task. The floor makes sure a clearly hard task (e.g. "refactor the architecture across the codebase", task_type = 1.0 → score ≥ 0.8) still routes to the cloud with no code attached.

### Factor 1: Task type (25%), the biggest single signal

We look for keywords in the prompt. Each keyword has a difficulty value:

| Complex keywords (examples) | Value | Simple keywords (examples) | Value |
|---|---|---|---|
| architecture, system design, race condition, deadlock, whole codebase | 1.0 | hello world | 0.0 |
| migrate, vulnerability, memory leak, concurrency, multiple files | 0.9 | typo | 0.05 |
| refactor, security, codebase, end-to-end | 0.8 | rename, docstring, one-liner | 0.1 |
| rewrite, performance, optimize, across, entire, authentication | 0.7 | explain, comment, format, syntax | 0.15–0.2 |
| debug, bug, crash, implement, feature | 0.6 | regex, convert | 0.3 |
| fix | 0.5 | | |

The rules:
- **Any complex keyword found:** take the highest value, then **add 0.05 for each extra complex keyword** (capped at 1.0). Several hard signals together make the task harder.
- **Exactly one complex keyword plus a simple keyword** (e.g. "*explain* this *race condition*"): blend 75% complex and 25% simple. The task is still hard, just slightly less so.
- **No complex keyword, but a localized one** (`unit test`, `modify`, `add error handling`, `add validation`, `refactor this function`, …): take the highest, 0.45–0.55. These keep a floor of `MEDIUM_TASK_FLOOR` (0.75) × task_type so they stay out of the simple band.
- **Only simple keywords:** take the lowest (easiest) value.
- **No keywords at all:** 0.45, a neutral "medium" guess.

### Factor 2: Reasoning (15%)

Does the request need multi-step thinking?

```text
reasoning = max( words_in_prompt / 120 ,  reasoning_markers_found / 3 )     (each capped at 1)
```

Reasoning markers: *why, trade-off, compare, best approach, step by step, should I, pros and cons, root cause, and then, first, also*.

### Factor 3: Context size (20%)

How much *relevant* code comes with the request — the optimized context, not the whole repo.

```text
context_size = optimized_context_tokens / 8000      (capped at 1)
```

Small local models (the default is `qwen2.5-coder:0.5b`) get noticeably worse, and on CPU much slower, with large contexts, so at 8k+ tokens this factor is maxed out.

### Factor 4: Files (20%)

```text
files = files_mentioned_in_prompt / 4                      (e.g. "auth.py", "routes.ts")
if a repo is attached: files = max(files, min(repo_files / 50, 0.6))
```

### Factor 5: Dependency scope (20%)

Cross-file / repository-wide work: imports, modules, "throughout", "across the codebase",
"multiple files", "end-to-end". Scored from those markers and the number of files in scope. This
feeds both the score and the capability check (`requires_multifile`).

### Worked examples (real outputs)

**"Write a Python one-liner to reverse a string"**

| Factor | Value | × Weight |
|---|---|---|
| task_type ("one-liner") | 0.10 | 0.025 |
| context_size | 0 | 0 |
| files | 0 | 0 |
| dependency | 0 | 0 |
| reasoning (8 words) | 0.067 | 0.010 |
| Weighted sum | | 0.035 |
| **Score** (no complex keywords, so no floor) | | **0.035 → 4/100 SIMPLE → LOCAL** |

**"Refactor the routing architecture across the codebase to support multiple cloud providers and explain the trade-offs"**, with a repo attached:

| Factor | Value | × Weight |
|---|---|---|
| task_type (5 complex keywords: 1.0 + 4×0.05, capped) | 1.00 | 0.250 |
| context_size (~6,400 tokens) | 0.80 | 0.160 |
| files (8-file repo) | 0.16 | 0.032 |
| dependency ("across the codebase") | 1.00 | 0.200 |
| reasoning ("trade-off" = 1 marker) | 0.333 | 0.050 |
| Weighted sum | | 0.692 |
| **Score** = max(0.692, 0.8 × 1.0) | | **0.80 → 80/100 COMPLEX → CLOUD** |

---

## Stage 1b: User consent in the UI (Streamlit demo)

The UI first calls `POST /analyze`. This is a dry run: it returns the decision, reasons and token estimates **without calling any model**. Then:

| Decision | What the UI does |
|---|---|
| `local` / `local_first` (simple or medium, local model capable) | Answers locally. If the backend flags the answer `low_confidence` (below `MIN_LOCAL_CONFIDENCE`, 0.7), it highlights a **"Retry with global LLM"** button. |
| `cloud` (complex, or local model not capable) | **Asks the user.** It shows the reasons and how many tokens would be sent, with two buttons: **Use local** and **Use global (recommended)**. |

The decision comes from `routing.plan_route` (see `docs/routing-policy.md`); the UIs only render it.

Nothing is sent to the cloud unless the user clicks a "global" button. In the UI, every `/chat` call uses `force_route`, so the API never escalates on its own. The automatic escalation in Stage 2 only applies to API clients that use `force_route: "auto"`.

---

## Stage 2: Confidence-based escalation

For requests routed **LOCAL** or **LOCAL FIRST**, we ask the local model to answer in this JSON shape:

```json
{"answer": "...", "confidence": 0.0-1.0, "needs_cloud": true/false, "more_available": true/false}
```

The answer is length-limited (`detail: "brief"`, about `LOCAL_BRIEF_WORDS` words, hard cap `LOCAL_BRIEF_MAX_TOKENS`); when a fuller answer would help, `more_available` makes the sidebar offer **More detailed explanation**, which re-asks with `detail: "detailed"`.

Small models tend to overrate their confidence, so we adjust the number they give:
- **−0.4** if the answer is under 20 characters (empty or near-empty)
- **−0.1 per hedging phrase**, up to 3: *"I'm not sure", "might be", "it depends", "without more context", "unclear"*...

**We escalate to the cloud if any of these are true** (first match wins; each has a `kind`):
- Ollama failed (crashed, timed out, not running) — `error`
- the local answer was empty/malformed — `validation` (even when confidence is high)
- the local model itself says `needs_cloud: true` — `needs_cloud`
- confidence is missing/invalid — `missing_confidence` (per `MISSING_CONFIDENCE_POLICY`)
- adjusted confidence **< 0.7** — `confidence`

Escalation is capped at `MAX_CLOUD_ESCALATIONS` (default 1) per request, so it can never loop. An
explicit local-only request and a privacy-restricted request are never escalated. When we escalate,
the user still gets an answer, from the cloud, with the same prompt and optimized context; the
response shows `escalated: true`, `escalation_kind` and the reason.

---

## Stage 3: Only relevant context goes to the cloud

Before anything goes to the cloud, the context optimizer picks only the code relevant to the prompt (persistent codebase index, `compression/codeindex`; in-memory engine as fallback) and masks secrets. The budget depends on the task: local-bound requests get the local window, complex cloud-bound ones get the cloud model's window minus room for the answer, capped at `CLOUD_MAX_CONTEXT_TOKENS` (16,000).

How we measure the savings:

```text
original_tokens = prompt + ALL code/repo        (what a normal cloud-only assistant would send)
sent_tokens     = tokens actually sent to cloud (0 if answered locally)
tokens_saved    = original_tokens − sent_tokens
```

Real example: the refactor request above would have sent **6,454** tokens. LocalPilot sent **2,972**, which is **54% fewer**.

---

## Why this works: pros and cons

### Pros

| Pro | Why it matters |
|---|---|
| **Most coding requests are simple** | Explaining code, writing docstrings, renames, small snippets and regexes are a large share of everyday requests. A small local coding model handles these well, so they never reach the cloud. |
| **Two safety nets** | The upfront score catches obviously hard tasks. The confidence check catches tasks that *looked* easy but weren't. Each one covers mistakes the other misses. |
| **Explainable** | Every decision lists its reasons, e.g. "complex task keywords: refactor, architecture" or "context size ~6,400 tokens". No black box. Good for the demo and easy to debug. |
| **Fast and free to decide** | Scoring takes under a millisecond and costs nothing. Using an LLM to make the routing decision would itself cost tokens and time. |
| **Easy to tune live** | Thresholds and weights are plain numbers in `.env` and `analyzer.py`. If a demo prompt routes wrong, we fix it in seconds. |
| **Fails safely** | If Ollama is down, requests automatically go to the cloud. Users always get an answer. |
| **Privacy by default** | Simple requests never leave the laptop. Cloud requests carry only the relevant files, not the whole repo. |

### Cons, and how we'd fix them

| Con | Impact | Mitigation |
|---|---|---|
| **Keyword matching is shallow** | "Fix this typo in the auth module" contains "auth" and "fix", so it scores higher than it should. Phrasing we haven't listed falls back to the neutral 0.45. | Medium-band requests are re-checked by the local-LLM tie-breaker (`classifier.py`). Later: a classifier trained on logged requests. |
| **Small models are overconfident** | In testing, the qwen2.5-coder models almost always report 0.9–1.0, so the confidence check rarely triggers (refusals are caught separately as validation failures). | Blend in the complexity score, e.g. `0.7 × self_confidence + 0.3 × (1 − score)`. Or run a quick self-check: ask the local model to verify its own answer. |
| **Hand-picked weights and thresholds** | The 34 / 64 category boundaries and the 25/20/20/20/15 weights are educated guesses, not learned from data. | `bench/routing_eval.py` runs a labelled prompt set; all values are configurable in `.env`. Log every decision so we could fit the weights later. |
| **"Local first" can be slow** | A big local answer takes 60s or more on a laptop (more with a large repo context on CPU). If it then escalates, the user waits for both models. | Local answers are now length-limited (brief by default, **More detail** on request). Next: run local and cloud in parallel for the middle band and cancel whichever isn't needed. |
| **No check that the answer is correct** | High confidence doesn't mean the answer is correct. | Future: run generated code or tests, check syntax, or have the cloud model review local answers on a sample. |

**One-line summary for the pitch:** cheap rules filter out the easy requests, the local model's own uncertainty catches the borderline ones, and the cloud only sees the hard ones, with trimmed context.

---

## Setting up the real cloud provider

### Claude or Gemini?

**Important: a Claude Pro subscription does NOT include API access.** Pro covers the claude.ai apps (and Claude Code). The API is a separate product with separate, pay-as-you-go billing through the Claude Console. So the real choice is between these:

| | **Claude API** (recommended if you can spend ~$5) | **Gemini API free tier** (recommended if you can't spend anything) |
|---|---|---|
| Cost | Pay as you go. Sonnet 5.5: $2 / $10 per million input / output tokens. Opus 5.5: $4 / $20. | Free tier with rate limits (via Google AI Studio) |
| Demo cost estimate | ~50 cloud calls × (3k in + 1k out) ≈ **$0.80 (Sonnet 5.5)** or **$1.60 (Opus 5.5)** | $0 |
| Coding quality | Excellent | Good |
| Code changes needed | None. Already built in (`CLOUD_PROVIDER=anthropic`). | None. Uses our OpenAI-compatible mode (`CLOUD_PROVIDER=openai`). |
| Fit with our pitch | Paid API traffic isn't used for training by default. Fits the "protect your code" message. | Free-tier prompts **may be used by Google to improve its products**. That slightly undercuts a pitch about not sending your code to the cloud. Check the current terms. |

**Recommendation:** use the **Claude API** with a small prepaid credit (also ask the hackathon organizers whether they provide API credits). It's the best coding quality, the demo costs a couple of dollars, and it fits the privacy story. If nobody can add a card, use **Gemini's free tier**. Switching later is just a change to `.env`.

### Option A: Claude API

1. Go to **console.anthropic.com**, sign in, open **Billing**, and add credits.
2. Go to **API Keys**, click **Create Key**, and copy it (it starts with `sk-ant-`).
3. In `backend/`, copy `.env.example` to `.env` and set:
   ```bash
   CLOUD_PROVIDER=anthropic
   ANTHROPIC_API_KEY=sk-ant-...
   ANTHROPIC_MODEL=claude-sonnet-5-5   # or claude-opus-5-5 for the strongest answers
   CLOUD_MAX_TOKENS=16000
   ```
4. Restart the server (`make backend` from the repo root) and test:
   ```bash
   curl -s localhost:8000/health
   curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
     -d '{"prompt":"Design an architecture for a rate limiter", "force_route":"cloud"}'
   ```
   The response should show `"model": "claude-sonnet-5-5"` instead of `mock-cloud`.

Notes:
- Keep `CLOUD_MAX_TOKENS` high (16000). Newer Claude models can think before answering, and that thinking counts toward the limit. A low cap can cut the answer off.
- **Never commit `.env`.** It's already in `.gitignore`.

### Option B: Gemini (free tier)

1. Go to **aistudio.google.com**, click **Get API key**, then **Create API key**.
2. Set `.env` (Gemini offers an OpenAI-compatible endpoint, so our existing code works unchanged):
   ```bash
   CLOUD_PROVIDER=openai
   OPENAI_API_KEY=<your Gemini key>
   OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
   OPENAI_MODEL=gemini-2.5-flash      # check AI Studio for the current model names
   ```
3. Restart the server and run the same `curl` test as above.

### Mock mode

`CLOUD_PROVIDER=mock` (the default) returns a placeholder cloud answer after 1.2s. Use it to build and test the UI without spending anything.
