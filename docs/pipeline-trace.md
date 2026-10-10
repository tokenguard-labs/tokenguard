# Pipeline Trace: one complex query, end to end

```text

==============================================================================
STAGE 0  REQUEST
==============================================================================
prompt      : Trace the complete lifecycle of requests.get from the public API through Session and the transport adapter back to a Response object.
repo_path   : /tmp/opencode/repos/requests
code        : (empty)
token_budget: 3000  (config.CLOUD_CONTEXT_TOKEN_BUDGET)

==============================================================================
STAGE 1  INGESTION  (walker.walk_repo)
==============================================================================
source files scanned : 37
excluded by walker   : node_modules/.git/dist/build, binaries, >512KB, .gitignore
baseline tokens      : 94777
files (first 12):
  - docs/_themes/flask_theme_support.py  (87 lines)
  - docs/conf.py  (386 lines)
  - setup.py  (10 lines)
  - src/requests/__init__.py  (220 lines)
  - src/requests/__version__.py  (15 lines)
  - src/requests/_internal_utils.py  (52 lines)
  - src/requests/_types.py  (189 lines)
  - src/requests/adapters.py  (749 lines)
  - src/requests/api.py  (181 lines)
  - src/requests/auth.py  (355 lines)
  - src/requests/certs.py  (19 lines)
  - src/requests/compat.py  (116 lines)

==============================================================================
STAGE 2  CHUNKING  (chunker.chunk_repo: Python AST / brace paragraphs)
==============================================================================
total chunks         : 385
chunk kinds          : {'module': 36, 'class': 141, 'file': 11, 'function': 146, 'header': 51}
chunkiest files:
  - tests/test_utils.py: 60 chunks
  - tests/test_requests.py: 58 chunks
  - src/requests/utils.py: 56 chunks
  - src/requests/exceptions.py: 27 chunks
  - src/requests/cookies.py: 20 chunks
  - src/requests/models.py: 16 chunks

==============================================================================
STAGE 3  RELEVANCE SCORING  (selector.rank_chunks)
==============================================================================
query terms (14): ['adapter', 'api', 'back', 'complete', 'get', 'lifecycle', 'object', 'public', 'requests', 'response', 'session', 'through', 'trace', 'transport']
chunks with score > 0 : 320 / 385
top 8 scored chunks:
  0.359  src/requests/sessions.py::SessionRedirectMixin L127-206  [symbol match: session; path match: requests, session; conten]
  0.341  tests/test_requests.py::TestRequests L1204-1283  [symbol match: requests; path match: requests; content match:]
  0.339  src/requests/adapters.py::HTTPAdapter L578-657  [symbol match: adapter; path match: adapter, requests; conten]
  0.338  src/requests/sessions.py::__module__ L1-75  [path match: requests, session; content match: adapter, back,]
  0.312  src/requests/adapters.py::BaseAdapter L122-155  [symbol match: adapter; path match: adapter, requests; conten]
  0.311  src/requests/adapters.py::HTTPAdapter L368-447  [symbol match: adapter; path match: adapter, requests; conten]
  0.308  src/requests/adapters.py::HTTPAdapter L158-237  [symbol match: adapter; path match: adapter, requests; conten]
  0.298  src/requests/sessions.py::SessionRedirectMixin L267-346  [symbol match: session; path match: requests, session; conten]

==============================================================================
STAGE 4  SEED SELECTION + IMPORT-GRAPH EXPANSION  (engine._select_symbol_mode)
==============================================================================
import graph edges   : 80
seed files (top_k=8):
  - src/requests/sessions.py  best=0.359
  - tests/test_requests.py  best=0.341
  - src/requests/adapters.py  best=0.339
  - src/requests/api.py  best=0.295
  - src/requests/__init__.py  best=0.292
  - src/requests/_types.py  best=0.275
  - src/requests/models.py  best=0.266
  - src/requests/utils.py  best=0.260
dependency neighbors (1 hop, capped at 16):
  - src/requests/__version__.py
  - src/requests/_internal_utils.py
  - src/requests/auth.py
  - src/requests/compat.py
  - src/requests/cookies.py
  - src/requests/exceptions.py
  - src/requests/help.py
  - src/requests/hooks.py
  - src/requests/status_codes.py
  - src/requests/structures.py
  - tests/__init__.py
  - tests/compat.py
  - tests/testserver/server.py
  - tests/utils.py
files considered     : 35
items after all waves: 22 chunks

==============================================================================
STAGE 5  ASSEMBLY UNDER TOKEN BUDGET  (engine._assemble)
==============================================================================
assembled chunks     : 22
files in context     : 22
budget               : 3000
included chunks (in priority order):
  0.359  src/requests/sessions.py::SessionRedirectMixin L127-206  [symbol match: session; path match: requests, session]
  0.341  tests/test_requests.py::TestRequests L1204-1283  [symbol match: requests; path match: requests; conten]
  0.339  src/requests/adapters.py::HTTPAdapter L578-657  [symbol match: adapter; path match: adapter, requests]
  0.295  src/requests/api.py::request L24-71  [path match: api, requests; content match: get, objec]
  0.292  src/requests/__init__.py::__module__ L109-220  [path match: requests; content match: api, back, get,]
  0.275  src/requests/_types.py::__module__ L53-189  [path match: requests; content match: api, complete, ]
  0.266  src/requests/models.py::PreparedRequest L378-457  [path match: requests; content match: back, get, obje]
  0.260  src/requests/utils.py::get_unicode_from_response L633-671  [symbol match: get, response; path match: requests; c]
  0.257  tests/testserver/server.py::Server L25-135  [content match: back, get, requests, response, trace;]
  0.215  src/requests/status_codes.py::__module__ L1-108  [path match: requests; content match: complete, objec]
  0.214  src/requests/cookies.py::MockRequest L31-111  [path match: requests; content match: back, get, obje]
  0.199  src/requests/structures.py::LookupDict L96-130  [path match: requests; content match: get, object, th]
  0.153  src/requests/auth.py::HTTPDigestAuth L264-343  [path match: requests; content match: get, object, re]
  0.123  src/requests/help.py::_implementation L35-64  [path match: requests; content match: complete; summa]
  0.122  src/requests/compat.py::__module__ L46-116  [path match: requests; content match: back, get; summ]
  0.118  src/requests/exceptions.py::RequestException L20-35  [path match: requests; content match: object, respons]
  0.104  src/requests/hooks.py::dispatch_hook L32-48  [path match: requests; content match: get, response; ]
  0.076  src/requests/_internal_utils.py::to_native_string L26-36  [path match: requests; content match: object; summary]
  0.027  src/requests/__version__.py L1-15  [path match: requests; content match: requests; summa]
  0.022  tests/__init__.py L1-15  [content match: requests; summary]
  0.022  tests/compat.py::u L14-23  [content match: requests; summary]
  0.000  tests/utils.py::__module__ L1-5  [dependency header of tests/utils.py]

==============================================================================
STAGE 6  REDACTION + OUTPUT FORMAT  (compression.adapter)
==============================================================================
secret rules hit     : 0
header format        : '// file:' -> '# File:'
final context tokens : 2985

==============================================================================
STAGE 7  CONTRACT RESULT  (compression.optimize_context -> backend.contracts.ContextResult)
==============================================================================
optimizer            : person2-v1
original_tokens      : 94777
optimized_tokens     : 2965
token reduction      : 96.9%
files_selected       : ['src/requests/sessions.py', 'tests/test_requests.py', 'src/requests/adapters.py', 'src/requests/api.py', 'src/requests/__init__.py', 'src/requests/_types.py', 'src/requests/models.py', 'src/requests/utils.py', 'tests/testserver/server.py', 'src/requests/status_codes.py', 'src/requests/cookies.py', 'src/requests/structures.py', 'src/requests/auth.py', 'src/requests/help.py', 'src/requests/compat.py', 'src/requests/exceptions.py', 'src/requests/hooks.py', 'src/requests/_internal_utils.py', 'src/requests/__version__.py', 'tests/__init__.py', 'tests/compat.py']
secrets_redacted     : 0
redaction_details    : []
optimize_context time: 279 ms

==============================================================================
STAGE 8  ROUTING  (POST /analyze  -- no model called)
==============================================================================
decision             : local_first
complexity_score     : 0.535
task_type            : general
factors              : {'task_type': 0.45, 'reasoning': 0.175, 'context_size': 1.0, 'files': 0.6}
reasons              : ['no strong task keywords, assuming medium', 'context size ~119065 tokens', 'repo attached with 62 source files']
original_tokens      : 94800
estimated_cloud_tokens: 2988
context_optimizer    : person2-v1
files_selected       : ['src/requests/sessions.py', 'tests/test_requests.py', 'src/requests/adapters.py', 'src/requests/api.py', 'src/requests/__init__.py', 'src/requests/_types.py', 'src/requests/models.py', 'src/requests/utils.py', 'tests/testserver/server.py', 'src/requests/status_codes.py', 'src/requests/cookies.py', 'src/requests/structures.py', 'src/requests/auth.py', 'src/requests/help.py', 'src/requests/compat.py', 'src/requests/exceptions.py', 'src/requests/hooks.py', 'src/requests/_internal_utils.py', 'src/requests/__version__.py', 'tests/__init__.py', 'tests/compat.py']
secrets_redacted     : 0

==============================================================================
STAGE 9  END TO END  (POST /chat, force cloud, mock provider)
==============================================================================
route                : cloud
model                : mock-cloud
context_optimizer    : person2-v1
escalated            : False
original_tokens      : 94800
sent_tokens          : 2993
tokens_saved         : 91807
files_selected       : ['src/requests/sessions.py', 'tests/test_requests.py', 'src/requests/adapters.py', 'src/requests/api.py', 'src/requests/__init__.py', 'src/requests/_types.py', 'src/requests/models.py', 'src/requests/utils.py', 'tests/testserver/server.py', 'src/requests/status_codes.py', 'src/requests/cookies.py', 'src/requests/structures.py', 'src/requests/auth.py', 'src/requests/help.py', 'src/requests/compat.py', 'src/requests/exceptions.py', 'src/requests/hooks.py', 'src/requests/_internal_utils.py', 'src/requests/__version__.py', 'tests/__init__.py', 'tests/compat.py']
secrets_redacted     : 0
latency_ms           : 1532
trace                : ['score=0.535 -> cloud']
answer (first 160)   : [MOCK CLOUD ANSWER] Received 2993 tokens of request + context. Set CLOUD_PROVIDER=anthropic or openai to get a real answer.

==============================================================================
STAGE 10  DASHBOARD  (GET /stats)
==============================================================================
{'total_requests': 1, 'local_requests': 0, 'cloud_requests': 1, 'escalations': 0, 'total_original_tokens': 94800, 'total_sent_tokens': 2993, 'total_tokens_saved': 91807, 'cloud_token_reduction_pct': 96.8, 'avg_latency_ms': 1532}
```
