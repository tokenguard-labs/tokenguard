# LocalPilot Classification & Routing Policy

This is the full specification of how LocalPilot decides **where** a coding request runs and
**whether** a locally produced answer should be escalated. It implements the "intelligent task
classification" requirement: an explainable, cost-aware policy that combines deterministic rules,
a lightweight complexity classifier, local-model capability checks, and confidence-based
escalation.

Everything is deterministic and rule-based — no second ML model and no extra LLM call for
classification. Every decision comes back with plain-English reasons.

## Three separate decisions

Conflating these is the most common mistake. They are implemented and tested independently.

| # | Decision | Values | Where |
|---|---|---|---|
| A | **Routing preference** — what the user asked for | `auto` \| `local` \| `cloud` | request field `force_route` |
| B | **Initial routing** — before any model runs | `local_first` \| `cloud_first` \| `local_only` \| `cloud_only` | `routing.plan_route` |
| C | **Post-response escalation** — after the local answer | return local \| escalate to cloud \| explain limitation | `routing.decide_escalation` |

A classifier that confidently labels a request *simple* does not guarantee the small model will
answer it correctly — that is why B and C are separate.

## Flow

```text
User Request
     │
     ▼
A. Routing preference?  (auto | local | cloud)
     │
     ├── local ─────────────────────────────► local_only   (never auto-escalates)
     ├── cloud ──(permitted?)───────────────► cloud_only   (skips local)
     │        └──(not permitted)────────────► local_only   (explain limitation)
     └── auto
           │
           ▼
      Hard policy checks        (privacy, cloud availability)
           │
           ▼
      Capability checks         (local reachable? tools? context fits? multi-file?)
           │
           ▼
      Complexity classification (simple | medium | complex)
           │
           ├── simple/medium + capable ──► local_first  (with escalation)
           ├── complex / not capable ────► cloud_first
           └── cloud prohibited ─────────► local_only / local_first
           │
           ▼
      Run local model
           │
           ▼
C. Evaluate result (confidence · validation · failure · needs_cloud)
           │
     ┌─────┴─────┐
     NO         YES
     │           │
  Return      Escalate to cloud (at most once; context + history preserved)
```

## Layer A — hard policy (runs first)

Hard checks take precedence over the complexity score.

| Condition | Effect |
|---|---|
| `force_route = local` | `local_only`; automatic cloud escalation disabled |
| `force_route = cloud` | `cloud_only` when cloud is permitted |
| cloud requested but not permitted | `local_only` — explain the limitation |
| `CLOUD_TRANSMISSION_PROHIBITED = true` | no code / prompt / history may leave the machine |
| `CLOUD_ENABLED = false` | every request is capped at local execution |

An explicit local-only preference is **never** overridden by automatic escalation, and privacy
restrictions are never overridden by routing optimization.

## Layer B — capability + complexity

### Capability checks

Before complexity is considered, the router checks whether the selected local model can plausibly
do the job. Each model has a small `ModelProfile` (`backend/routing.py`):

```python
{
  "model_id": "qwen2.5-coder:1.5b",
  "params_billions": 1.5,
  "max_context_tokens": 8192,
  "supports_tool_calling": False,
  "supports_code_generation": True,
  "supports_multifile_reasoning": "no"        # yes | limited | no
}
```

Profiles are derived from the parameter count read from the model id and can be overridden per
model with `LOCAL_MODEL_PROFILES` (JSON); fields reflect verifiable behaviour, not assumptions.

The check fails (`local_capable = false`) when:

- Ollama is not reachable;
- the task needs tool/function calling, which the local path cannot provide;
- the task spans multiple files and the model handles this `no` (`limited` is allowed with a note);
- the **relevant** (optimized) context exceeds the local window minus a margin.

When local is not capable, the router goes straight to the cloud (when permitted) instead of
wasting a slow local attempt. It does **not** assume rules like "more than three files must be
cloud" — file count is a signal, not a law.

### Complexity classification

Tasks are first bucketed by keyword tier (`task_type`): hard/cross-cutting keywords (complex),
localized single-unit work such as *unit tests*, *modify this function*, *add error handling*
(medium), or trivial work (simple). Each tier has a floor (`TASK_TYPE_FLOOR` for complex,
`MEDIUM_TASK_FLOOR` for medium) so a prompt with no attached code still reaches the right band.

### Local-LLM tie-breaker (medium band only)

Keyword matching is shallow, so when the heuristic lands in the **medium band** (35–64) — where it
is least certain — the router asks the already-loaded local model for a zero-shot verdict
(`backend/classifier.py`): a strict JSON `{complexity, needs_cloud, reason}`. That verdict becomes
the effective category:

- `simple` → stays local-first (no escalation gate on the model's say-so);
- `complex` → cloud-first;
- `medium` or any failure/timeout → the keyword heuristic stands.

It is deliberately narrow: simple and complex requests are never sent to the classifier, so they
stay instant and deterministic, and routing never *depends* on the network. Results are cached per
`(model, prompt)` for `LOCAL_LLM_CLASSIFIER_TTL_S`, and the response records `classified_by`
(`heuristic`/`local_llm`), `heuristic_complexity` and `model_complexity` so the decision stays
explainable. Disable it with `LOCAL_LLM_CLASSIFIER=false`.

Trade-off: it adds one short local inference (roughly 1–2s warm, more on a cold model load) for
medium-band prompts, in exchange for better handling of requests whose wording does not match the
keyword lists.

Five factors, each normalised to `0..1`, combine into a `0..100` score
(weights are configurable heuristics in `COMPLEXITY_WEIGHTS`):

| Factor | Meaning |
|---|---|
| `task_type` | how hard this kind of task usually is (keyword map) |
| `context_size` | tokens of the *relevant* context (not the whole repo) |
| `files` | files mentioned in the prompt + attached repo size |
| `dependency` | cross-file / whole-repo scope (imports, modules, "throughout") |
| `reasoning` | multi-step reasoning markers + prompt length |

```text
score = 100 * Σ(weight_i × factor_i) / Σ(weight_i)      (0..100)
score = max(score, TASK_TYPE_FLOOR × task_type)          only for complex keyword matches
```

Default weights: `task_type 0.25 · context_size 0.20 · files 0.20 · dependency 0.20 · reasoning 0.15`.
The `TASK_TYPE_FLOOR` (0.8 × task_type) keeps a clearly hard, prompt-only request routing to the
cloud even with no code attached.

| Score | Category | Default route |
|---|---|---|
| 0–34 | **Simple** | local-first |
| 35–64 | **Medium** | local-first, with escalation |
| 65–100 | **Complex** | cloud-first (when permitted) |

These boundaries and weights are **starting heuristics, not empirically optimal**. They are
configurable (`COMPLEXITY_SIMPLE_MAX`, `COMPLEXITY_MEDIUM_MAX`, `COMPLEXITY_WEIGHTS`) and are
meant to be tuned against the evaluation set.

The classifier returns a structured result — the `/analyze` and `/chat` responses expose
`complexity`, `complexity_score_100`, `initial_route`, `local_capable`, `capability_reasons`,
`model_profile`, `estimated_context_tokens` and the human-readable `reasons`.

## Layer C — post-response escalation

Confidence is **one signal, not the only signal**. Both routing confidence and answer confidence
exist; do not claim a self-reported `0.85` is an 85% probability of correctness unless the score
has been calibrated.

Escalation is evaluated in this order (first match wins), for auto-routed local-first requests:

| Condition | Kind | Result |
|---|---|---|
| explicit local-only request | — | no escalation (never) |
| escalation disabled | — | no |
| escalation limit reached | — | no (prevents routing loops) |
| cloud not permitted | — | no |
| local model failed | `error` | escalate (failure policy) |
| local output failed validation | `validation` | escalate, even if confidence is high |
| local model flagged `needs_cloud` | `needs_cloud` | escalate |
| confidence missing / invalid | `missing_confidence` | per `MISSING_CONFIDENCE_POLICY` (`escalate`/`keep`) |
| `confidence < MIN_LOCAL_CONFIDENCE` (0.7) | `confidence` | escalate |
| otherwise | — | return the local answer |

A high confidence score does **not** override an objective validation failure. Malformed output and
execution failures are handled by their own policies, not the confidence threshold.

### Escalating to the cloud

When escalating, the router preserves the original user query, the relevant repository context and
(reusing the existing pipeline) the conversation history; it reuses the context optimizer, enforces
the cloud budget, applies the existing secret scanner, and respects the transmission policy. At most
`MAX_CLOUD_ESCALATIONS` (default 1) automatic escalations per request are allowed.

## Configuration

All of the policy is configurable through `backend/.env` (see `backend/.env.example`). Key knobs:

`DEFAULT_ROUTE`, `CLOUD_ENABLED`, `CLOUD_TRANSMISSION_PROHIBITED`, `ESCALATION_ENABLED`,
`MAX_CLOUD_ESCALATIONS`, `MISSING_CONFIDENCE_POLICY`, `MIN_LOCAL_CONFIDENCE`,
`COMPLEXITY_WEIGHTS`, `TASK_TYPE_FLOOR`, `MEDIUM_TASK_FLOOR`, `COMPLEXITY_SIMPLE_MAX`, `COMPLEXITY_MEDIUM_MAX`,
`LOCAL_LLM_CLASSIFIER`, `LOCAL_LLM_CLASSIFIER_TTL_S`, `LOCAL_LLM_CLASSIFIER_TIMEOUT_S`,
`LOCAL_MAX_CONTEXT_TOKENS`, `LOCAL_CONTEXT_MARGIN_TOKENS`, `LOCAL_SUPPORTS_TOOL_CALLING`,
`LOCAL_MODEL_PROFILES`, `OLLAMA_REACH_TTL_S`.

The configuration is validated at startup (`config.validate()`); an invalid value fails fast
instead of silently mis-routing. The frontend cannot override backend privacy rules or the
confidence threshold — those are enforced server-side.

## Explainability

Every `/chat` and `/analyze` response records structured routing metadata: requested route, initial
route, final route, complexity category and score, confidence and its threshold, capability checks,
whether escalation occurred and why, the model profile, latency and token usage. Neither the
backend nor the UI logs full source code or conversation contents by default.

## Evaluation

`bench/routing_queries.json` is a labelled query set of ~40 representative coding requests
(40+ labelled cases across regex/explanation/rename, localized debugging/unit tests/modification,
repository-wide refactors/migrations/concurrency, and policy cases for explicit routes, privacy
and capability limits). Each entry carries an expected complexity category and initial route.

```bash
python bench/routing_eval.py            # prints the table, per-case pass/fail and accuracy
```

`tests/test_routing_set.py` runs the same set as a regression test, so a change to the keywords,
weights or thresholds that flips a route fails the suite.

The set measures the classifier, not answer quality. To evaluate *answer* quality (task success,
escalation rate, cloud tokens, latency, privacy compliance), pair it with the context benchmarks
and a task rubric. **Do not optimize for fewer cloud calls alone** — a policy that cuts cost but
worsens answers is not a success.

## Assumptions & limitations

- Weights and thresholds are hand-picked engineering heuristics; they are configurable and should
  be tuned on representative prompts, not treated as optimal.
- Keyword heuristics are shallow: "fix this typo in the auth module" matches auth/fix and scores
  higher than it should. Phrasing outside the lists falls back to a neutral medium guess.
- `needs_cloud`/confidence come from the same local model and can be over-confident; the answer
  validation step only catches empty/malformed output today, not subtle wrongness.
- The local capability profile is coarse (parameter count + optional overrides); a learned or
  measured profile would be better once data exists.
- The bundled local path has no tool calling; requiring tools forces cloud (or a local-only
  explanation when cloud is prohibited).
