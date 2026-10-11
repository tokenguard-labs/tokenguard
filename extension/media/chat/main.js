// LocalPilot sidebar. Loaded by src/chatView.ts after the vendor scripts
// (marked, DOMPurify, highlight.js), which may be missing in a broken install:
// every use of them falls back to plain text.
(function () {
    const vscode = acquireVsCodeApi();
    const history = document.getElementById('history');
    const input = document.getElementById('prompt');
    const sendBtn = document.getElementById('send');
    const modelSelect = document.getElementById('localModel');
    const emptyState = document.getElementById('empty');
    const scroller = document.getElementById('scroller');
    const cloudProviderSel = document.getElementById('cloudProvider');
    const cloudModelSel = document.getElementById('cloudModel');
    const cloudBaseUrlRow = document.getElementById('cloudBaseUrlRow');
    const cloudBaseUrlInput = document.getElementById('cloudBaseUrl');
    const cloudKeyInput = document.getElementById('cloudKey');
    const cloudStateEl = document.getElementById('cloudState');
    const CLOUD_PROVIDERS = ['deepseek', 'anthropic', 'openai', 'custom', 'mock'];

    let nextId = 1;
    let busy = false;               // one request at a time, like the Streamlit UI
    let activeId = null;            // turn whose request is in flight (what Stop cancels)
    const turns = new Map();        // id -> { prompt, req, analysis, el, pendingEl, stream }

    const EXAMPLES = [
        'Explain what the selected code does',
        'Write unit tests for the selected function',
        'Find possible bugs in the selected code',
        'Refactor the selected code to be easier to read',
        'Where is the configuration loaded in this repo?',
        'Summarize the architecture of this project'
    ];

    function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        })[c]);
    }

    function fmt(n) { return Number(n ?? 0).toLocaleString(); }

    // ---- Markdown -------------------------------------------------------------------------

    const hasMarkdown = typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined';
    const hasHighlight = typeof hljs !== 'undefined';

    if (hasMarkdown) {
        marked.use({
            gfm: true,
            breaks: true, // models often rely on single newlines, which the old renderer kept
            renderer: {
                // Render images as links: loading a remote image could leak data from the prompt.
                image(token) {
                    return '<a href="' + esc(token.href) + '">' + esc(token.text || token.href) + '</a>';
                }
            }
        });
    }
    // Model output is untrusted: sanitize everything marked produces.
    const PURIFY = {
        FORBID_TAGS: ['img', 'style', 'form', 'input', 'button', 'textarea', 'select', 'iframe', 'svg', 'math'],
        FORBID_ATTR: ['style', 'id', 'class'] // marked's language-* class is re-added below
    };

    /** Render model markdown into `container`, with highlighted, actionable code blocks. */
    function fillMarkdown(container, text) {
        const source = String(text ?? '');
        if (!hasMarkdown) {
            container.style.whiteSpace = 'pre-wrap';
            container.textContent = source;
            return;
        }
        // Keep the fence language before DOMPurify strips classes.
        const langs = [];
        const html = marked.parse(source).replace(/<code class="language-([^"]*)">/g, (_, lang) => {
            langs.push(lang);
            return '<code data-lang="' + langs.length + '">';
        });
        container.innerHTML = DOMPurify.sanitize(html, { ...PURIFY, ADD_ATTR: ['data-lang'] });
        container.querySelectorAll('pre > code').forEach(code => {
            const i = Number(code.getAttribute('data-lang'));
            code.removeAttribute('data-lang');
            enhanceCodeBlock(code, i ? langs[i - 1] : '');
        });
    }

    function enhanceCodeBlock(code, fenceLang) {
        const pre = code.parentElement;
        const raw = code.textContent.replace(/\n$/, '');
        let lang = (fenceLang || '').toLowerCase();
        if (hasHighlight) {
            try {
                if (lang && hljs.getLanguage(lang)) {
                    code.innerHTML = hljs.highlight(raw, { language: lang, ignoreIllegals: true }).value;
                    code.classList.add('hljs');
                } else if (!lang && raw.length < 20000) {
                    const auto = hljs.highlightAuto(raw);
                    if (auto.language && auto.relevance >= 5) {
                        code.innerHTML = auto.value;
                        code.classList.add('hljs');
                        lang = auto.language;
                    }
                }
            } catch {
                code.textContent = raw; // leave it unhighlighted
            }
        }
        const block = document.createElement('div');
        block.className = 'code-block';
        block.dataset.lang = lang;
        const bar = document.createElement('div');
        bar.className = 'code-bar';
        bar.innerHTML = '<span class="code-lang">' + esc(lang || 'text') + '</span>' +
            '<span class="code-actions">' +
                '<button class="icon-btn" data-code-action="copy" title="Copy to clipboard">⧉ Copy</button>' +
                '<button class="icon-btn" data-code-action="insert" title="Insert at the cursor in the active editor (replaces the selection)">↳ Insert</button>' +
            '</span>';
        pre.replaceWith(block);
        block.append(bar, pre);
    }

    // highlight.js ships one theme per color scheme; follow VS Code's theme as it changes.
    function applyCodeTheme() {
        const cls = document.body.classList;
        const dark = cls.contains('vscode-dark') || (cls.contains('vscode-high-contrast') && !cls.contains('vscode-high-contrast-light'));
        const light = document.getElementById('hljsLight');
        const darkSheet = document.getElementById('hljsDark');
        if (light) light.disabled = dark;
        if (darkSheet) darkSheet.disabled = !dark;
    }
    applyCodeTheme();
    new MutationObserver(applyCodeTheme).observe(document.body, { attributes: true, attributeFilter: ['class'] });

    // ---- Local model picker ---------------------------------------------------------------

    // Populate the local-model picker: installed models plus recommendations that fit
    // this machine (pulled on demand). The empty value means "use LOCAL_MODEL from .env".
    let installedModels = [];
    let recommendedModels = [];
    let loadingModel = null;   // model currently being loaded into memory

    function renderModelSelect(available, recommended, pulling, current, saved, loaded) {
        const installed = available || [];
        const recs = recommended || [];
        const inFlight = pulling || [];
        installedModels = installed;
        recommendedModels = recs;
        const state = document.getElementById('modelState');
        if (!installed.length && !recs.length) {
            modelSelect.innerHTML = '<option value="">(no Ollama models found)</option>';
            modelSelect.disabled = true;
            if (state) state.textContent = '';
            return;
        }
        modelSelect.disabled = false;
        const opts = ['<option value="">Default (' + esc(current || 'backend') + ')</option>'];
        if (installed.length) {
            opts.push('<optgroup label="Installed">');
            installed.forEach(m => opts.push('<option value="' + esc(m) + '">' + esc(m) + '</option>'));
            opts.push('</optgroup>');
        }
        if (recs.length) {
            opts.push('<optgroup label="Recommended — needs download">');
            recs.forEach(m => {
                const size = m.size_gb ? ' — ' + m.size_gb + ' GB' : '';
                const warn = m.comfortable === false ? ' (may use swap)' : '';
                const downloading = inFlight.includes(m.name) ? ' — downloading…' : '';
                opts.push('<option value="' + esc(m.name) + '">' + esc(m.name) + size + warn + downloading + '</option>');
            });
            opts.push('</optgroup>');
        }
        modelSelect.innerHTML = opts.join('');
        const known = installed.concat(recs.map(m => m.name));
        modelSelect.value = known.includes(saved) ? saved : '';
        if (state) {
            if (loadingModel && loaded === loadingModel) {
                loadingModel = null; // finished loading
            }
            let text;
            if (loadingModel) {
                text = '<span class="spinner">loading ' + esc(loadingModel) + '…</span>';
            } else {
                text = loaded ? 'loaded in memory: ' + esc(loaded) : 'no model loaded';
            }
            if (inFlight.length) text += ' · downloading: ' + esc(inFlight.join(', '));
            state.innerHTML = text;
        }
    }

    // ---- Turns ----------------------------------------------------------------------------

    function setBusy(value, id) {
        busy = value;
        activeId = value ? id : null;
        sendBtn.disabled = false;
        sendBtn.classList.toggle('stop', value);
        sendBtn.textContent = value ? '■ Stop' : 'Send Request';
        sendBtn.title = value ? 'Stop generating (Esc)' : 'Send (Enter)';
        document.querySelectorAll('button[data-route], button[data-retry]').forEach(b => {
            b.disabled = value || b.dataset.used === '1';
        });
    }

    function updateEmptyState() {
        emptyState.hidden = history.children.length > 0;
    }

    // Keep the conversation pinned to the newest message while the user is at (or near) the bottom;
    // cards grow after they are added (answers, telemetry), so a one-off scroll is not enough.
    let stickToBottom = true;
    scroller.addEventListener('scroll', () => {
        stickToBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
    });
    new MutationObserver(() => {
        if (stickToBottom) scroller.scrollTop = scroller.scrollHeight;
    }).observe(history, { childList: true, subtree: true, characterData: true });

    function addCard(html, cls = 'msg') {
        const div = document.createElement('div');
        div.className = cls;
        div.innerHTML = html;
        history.appendChild(div);
        updateEmptyState();
        stickToBottom = true;
        scroller.scrollTop = scroller.scrollHeight;
        return div;
    }

    function send() {
        const prompt = input.value.trim();
        if (!prompt || busy) return;
        const id = nextId++;
        addCard('<b>You:</b> ' + esc(prompt), 'msg user');
        const el = addCard('');
        turns.set(id, { prompt, el });
        analyze(id);
        input.value = '';
    }

    function analyze(id) {
        const turn = turns.get(id);
        turn.el.innerHTML = '<span class="spinner">Analyzing prompt (local vs global)…</span>';
        setBusy(true, id);
        vscode.postMessage({ type: 'sendPrompt', id, prompt: turn.prompt });
    }

    /** Run the turn on `route`, rendering into `el` (a retried card) or a new card. */
    function run(id, route, el) {
        const turn = turns.get(id);
        if (!turn || busy) return;
        const label = route === 'local' ? 'local LLM' : 'global (cloud) LLM';
        turn.pendingEl = el || addCard('');
        turn.pendingEl.innerHTML = '<span class="spinner">Asking the ' + label + '…</span>';
        turn.route = route;
        endStream(turn);
        setBusy(true, id);
        vscode.postMessage({ type: 'run', id, req: turn.req, route });
    }

    function stop() {
        if (!busy || activeId === null) return;
        sendBtn.disabled = true;
        sendBtn.textContent = 'Stopping…';
        vscode.postMessage({ type: 'cancel', id: activeId });
    }

    function clearChat() {
        if (busy && activeId !== null) {
            vscode.postMessage({ type: 'cancel', id: activeId });
        }
        turns.clear();
        history.innerHTML = '';
        setBusy(false);
        updateEmptyState();
        input.focus();
    }

    function decisionLabel(d) {
        return d === 'cloud' ? '🔵 Complex → global LLM recommended'
             : d === 'local_first' ? '🟡 Medium → local first'
             : '🟢 Simple → local LLM';
    }

    function accordion(title, body) {
        return '<details class="acc"><summary>' + title + '</summary><div class="acc-body">' + body + '</div></details>';
    }

    function fileList(files) {
        return files.length
            ? '<div class="files">' + files.map(f => '<code>' + esc(f) + '</code>').join(', ') + '</div>'
            : '<div class="muted">no repo files</div>';
    }

    function renderAnalysis(id, a, contextInfo) {
        const turn = turns.get(id);
        const reasons = (a.reasons || []).map(r => '<li>' + esc(r) + '</li>').join('');
        const files = a.files_selected || [];
        const isCloud = a.decision === 'cloud';
        turn.el.innerHTML =
            '<span class="badge ' + esc(a.decision) + '">' + decisionLabel(a.decision) + '</span>' +
            '<div class="metrics"><b>Complexity:</b> ' + esc(a.complexity || '?') +
            ' (' + esc(a.complexity_score_100 != null ? a.complexity_score_100 : '?') + '/100)' +
            ' · <b>Task:</b> ' + esc(a.task_type) +
            ' · initial: ' + esc(a.initial_route || '?') +
            ' · ~' + fmt(a.estimated_cloud_tokens) + ' tokens if sent to cloud</div>' +
            (a.local_capable === false
                ? '<div class="warn">Local model not capable: ' +
                  esc((a.capability_reasons || []).join('; ') || 'capability check failed') + '</div>'
                : '') +
            (reasons || Object.keys(a.factors || {}).length
                ? accordion('Why this route',
                    '<div class="muted">factors: ' + Object.keys(a.factors || {}).map(k => esc(k) + ': ' + esc(a.factors[k])).join(' · ') + '</div>' +
                    (reasons ? '<ul>' + reasons + '</ul>' : ''))
                : '') +
            accordion('Context · ' + files.length + ' file' + (files.length === 1 ? '' : 's'),
                '<div class="muted">Repo searched: ' + (a.repo_path ? '<code>' + esc(a.repo_path) + '</code>' : 'none') +
                ' · from ' + esc((a.context_sources || []).join('; ') || contextInfo) + '</div>' +
                '<div class="muted">Optimizer: ' + esc(a.context_optimizer) + '</div>' +
                fileList(files)) +
            accordion('Token estimate',
                'Global would send ~<b>' + fmt(a.estimated_cloud_tokens) + '</b> tokens instead of ' +
                fmt(a.original_tokens) + ' for the full context' +
                (a.secrets_redacted ? ', with <b>' + esc(a.secrets_redacted) + '</b> secret(s) masked' : '') +
                '. Local sends nothing off your machine.') +
            '<div class="choices">' +
                '<button data-route="local" data-id="' + id + '" class="' + (isCloud ? 'secondary' : '') + '">🖥️ Use local LLM</button>' +
                '<button data-route="cloud" data-id="' + id + '" class="' + (isCloud ? '' : 'secondary') + '">☁️ Use global LLM' + (isCloud ? ' (recommended)' : '') + '</button>' +
            '</div>';
    }

    /** Why this task got its complexity category: heuristic reasons, factors, and classifier verdict. */
    function complexityReason(r) {
        const reasons = (r.reasons || []).map(x => '<li>' + esc(x) + '</li>').join('');
        const factors = r.factors || {};
        const facts = Object.keys(factors).map(k => esc(k) + ': ' + esc(factors[k])).join(' · ');
        if (!reasons && !facts && !r.model_complexity_reason) return '';
        const verdict = r.model_complexity
            ? ' · local classifier: <b>' + esc(r.model_complexity) + '</b>'
            : '';
        return accordion('Why this complexity · ' + esc(r.complexity || '?') + ' (' + esc(r.complexity_score_100) + '/100)',
            '<div class="muted">heuristic: ' + esc(r.heuristic_complexity || '?') + verdict + '</div>' +
            (r.model_complexity_reason ? '<div class="muted">classifier reason: ' + esc(r.model_complexity_reason) + '</div>' : '') +
            (facts ? '<div class="muted">factors: ' + facts + '</div>' : '') +
            (reasons ? '<ul>' + reasons + '</ul>' : ''));
    }

    /** "Retrieved context" from the router_view metadata: what was retrieved, why, and adequacy. */
    function contextDetails(r) {
        const cm = r.context_metadata || {};
        if (!cm.retrieval_coverage && !r.files_selected) return '';
        const cov = cm.retrieval_coverage || {};
        const missing = cm.missing_required || [];
        const uncertain = cm.uncertain_references || [];
        const evidence = cm.evidence || [];
        const count = cov.files_selected != null ? cov.files_selected : (r.files_selected || []).length;
        const lines = evidence.slice(0, 10).map(e =>
            '<div class="muted"><code>' + esc(e.file) + ':' + esc(e.start_line) + '</code> ' +
            esc(e.symbol || '') + ' <span class="muted">[' + esc(e.retriever || '') + '] ' + esc(e.reason || '') + '</span></div>'
        ).join('');
        const scope = cm.scope || {};
        let scopeLine = '';
        if (scope.corpus_wide) {
            const langs = (scope.source_languages || []).join(', ') +
                ((scope.target_languages || []).length ? ' → ' + scope.target_languages.join(', ') : '');
            scopeLine = '<div class="muted">scope: <b>corpus-wide</b>' + (langs ? ' · ' + esc(langs) : '') +
                ' · ' + esc(cov.in_scope_files != null ? cov.in_scope_files : '?') + ' files · full ' +
                esc(cov.represented_full != null ? cov.represented_full : '?') + ' · signatures ' +
                esc(cov.represented_signature != null ? cov.represented_signature : '?') + ' · omitted ' +
                esc(cov.omitted_files != null ? cov.omitted_files : '?') + '</div>';
        }
        return accordion('Retrieved context · ' + esc(r.context_optimizer || 'n/a'),
            '<div class="muted">files: <b>' + esc(count) + '</b> · candidates: ' + esc(cov.candidates != null ? cov.candidates : '?') +
            ' · adequacy: <b>' + esc(cm.adequacy_status || 'n/a') + '</b>' + (cov.semantic ? ' · semantic: on' : '') +
            ' · budget: ' + esc(cm.context_budget != null ? cm.context_budget : '?') + '</div>' +
            scopeLine +
            (cm.adequacy_recommendation ? '<div class="warn">' + esc(cm.adequacy_recommendation) + '</div>' : '') +
            (missing.length ? '<div class="warn">missing required: ' + esc(missing.join(', ')) + '</div>' : '') +
            (uncertain.length ? '<div class="muted">uncertain refs: ' + esc(uncertain.join(', ')) + '</div>' : '') +
            fileList(r.files_selected || []) + lines);
    }

    function routeBadge(isLocal) {
        return '<span class="badge ' + (isLocal ? 'local' : 'cloud') + '">' +
            (isLocal ? '🟢 Local inference' : '🔵 Global (cloud) LLM') + '</span>';
    }

    // ---- Streaming ------------------------------------------------------------------------
    // While /chat/stream runs, the card shows the answer as it is generated; the final 'result'
    // re-renders it with the metrics. Markdown is re-rendered at most every STREAM_RENDER_MS.

    const STREAM_RENDER_MS = 60;

    function onStream(id, event, data) {
        const turn = turns.get(id);
        const el = turn.pendingEl;
        if (!el) return;
        if (event === 'start') {
            // A second start (escalation from local to cloud) replaces the first answer.
            endStream(turn);
            turn.stream = { text: '' };
            el.innerHTML = routeBadge(data.route === 'local') +
                '<div class="metrics"><b>Model:</b> ' + esc(data.model) + ' · <span class="spinner">generating…</span></div>' +
                (data.escalation_reason ? '<div class="warn">Escalated: ' + esc(data.escalation_reason) + '</div>' : '') +
                '<div class="stream-note"></div><div class="answer"></div>';
            return;
        }
        if (!turn.stream) return;
        if (event === 'delta') {
            turn.stream.text += data.text;
        } else if (event === 'reset') {
            turn.stream.text = '';
            const note = el.querySelector('.stream-note');
            if (note) note.innerHTML = '<div class="warn">' + esc(data.reason) + '</div>';
        }
        if (!turn.stream.timer) {
            turn.stream.timer = setTimeout(() => {
                if (!turn.stream) return;
                turn.stream.timer = null;
                const answer = el.querySelector('.answer');
                if (answer) fillMarkdown(answer, turn.stream.text);
            }, STREAM_RENDER_MS);
        }
    }

    /** Stop streaming into the turn's card; returns the text received so far. */
    function endStream(turn) {
        const stream = turn.stream;
        turn.stream = null;
        if (!stream) return '';
        clearTimeout(stream.timer);
        return stream.text;
    }

    /** A partial streamed answer, kept on the card when the run is stopped or fails. */
    function partialAnswer(el, text) {
        if (!text) return;
        const answer = document.createElement('div');
        answer.className = 'answer partial';
        el.prepend(answer);
        fillMarkdown(answer, text);
    }

    function renderResult(id, r) {
        const turn = turns.get(id);
        endStream(turn);
        const isLocal = r.route === 'local';
        const minConf = turn.analysis ? turn.analysis.min_local_confidence : null;
        const conf = r.confidence == null ? 'n/a' : Math.round(r.confidence * 100) + '%';
        const lowConf = isLocal && r.confidence != null && minConf != null && r.confidence < minConf;
        const pct = r.original_tokens ? Math.round(100 * r.tokens_saved / r.original_tokens) : 0;
        const files = r.files_selected || [];
        const trace = (r.trace || []).map(t => '<li>' + esc(t) + '</li>').join('');

        const routing = accordion('Routing policy',
            '<div class="muted">requested: <code>' + esc(r.requested_route) + '</code> · initial: <code>' +
            esc(r.initial_route) + '</code> · final: <code>' + esc(r.final_route || r.route) + '</code></div>' +
            '<div class="muted">complexity: <b>' + esc(r.complexity) + '</b> (' + esc(r.complexity_score_100) +
            '/100) · escalation: ' + esc(r.escalation_kind || 'none') + '</div>' +
            (r.capability_reasons && r.capability_reasons.length
                ? '<div class="muted">capability: ' + esc(r.capability_reasons.join('; ')) + '</div>' : ''));

        let metrics;
        let details;
        if (isLocal) {
            metrics = '<b>Model:</b> ' + esc(r.model) + ' · ' + fmt(r.latency_ms) + ' ms · confidence ' + conf +
                ' · <b>0</b> tokens sent to cloud';
            details = accordion('Confidence & escalation',
                'Confidence: <b>' + conf + '</b>' +
                (minConf != null ? ' (threshold ' + Math.round(minConf * 100) + '%)' : '') +
                (r.escalated ? '<div>Escalated: ' + esc(r.escalation_reason) + '</div>' : '<div class="muted">Not escalated.</div>'));
        } else {
            metrics = '<b>Model:</b> ' + esc(r.model) + ' · ' + fmt(r.latency_ms) + ' ms · saved ' + pct + '% tokens';
            details =
                accordion('Token savings',
                    'Sent ' + fmt(r.sent_tokens) + ' of ' + fmt(r.original_tokens) + ' tokens (saved ' + fmt(r.tokens_saved) +
                    ', ' + pct + '%) · ' + esc(r.secrets_redacted) + ' secret(s) masked' +
                    (r.cost_usd != null ? ' · <b>Cost:</b> $' + Number(r.cost_usd).toFixed(5) : '')) +
                accordion('Context sent · ' + files.length + ' file' + (files.length === 1 ? '' : 's'),
                    (r.repo_path ? '<div class="muted">from <code>' + esc(r.repo_path) + '</code></div>' : '') + fileList(files));
        }
        details += routing;
        details += complexityReason(r);
        details += contextDetails(r);

        const el = turn.pendingEl;
        el.innerHTML =
            routeBadge(isLocal) +
            '<div class="metrics">' + metrics + '</div>' +
            (r.escalated ? '<div class="warn">Escalated: ' + esc(r.escalation_reason) + '</div>' : '') +
            (r.is_mock ? '<div class="warn">⚠️ Mock answer — not a real cloud call. ' + esc(r.cloud_fallback_reason || 'CLOUD_PROVIDER=mock') + '</div>' : '') +
            (lowConf ? '<div class="warn">The local model wasn\'t confident about this answer.</div>' : '') +
            '<div class="answer"></div>' +
            details +
            (trace ? accordion('Pipeline trace', '<ul>' + trace + '</ul>') : '') +
            (isLocal ? '<div class="choices"><button data-route="cloud" data-id="' + id + '" class="' + (lowConf ? '' : 'secondary') + '">' +
                (lowConf ? '☁️ Retry with global LLM' : 'Not satisfied? ☁️ Use global LLM') + '</button></div>' : '');
        fillMarkdown(el.querySelector('.answer'), r.answer);
        turn.pendingEl = null;
    }

    function renderCancelled(id) {
        const turn = turns.get(id);
        const el = turn.pendingEl || turn.el;
        // Before the analysis arrives the whole turn is retried; after it, only the run.
        const retry = turn.pendingEl
            ? '<button class="secondary" data-retry="run" data-id="' + id + '" data-retry-route="' + esc(turn.route) + '">↻ Retry</button>'
            : '<button class="secondary" data-retry="analyze" data-id="' + id + '">↻ Retry</button>';
        el.innerHTML = '<div class="cancelled">⏹ Stopped.</div><div class="choices">' + retry + '</div>';
        partialAnswer(el, endStream(turn));
        turn.pendingEl = null;
    }

    // ---- Status ---------------------------------------------------------------------------

    function renderStatus(m) {
        const el = document.getElementById('status');
        if (m.starting) {
            el.innerHTML = '<span class="spinner">Starting LocalPilot backend…</span> (' + esc(m.apiUrl) + ')';
            return;
        }
        if (!m.online) {
            const retrying = m.retrying ? ' <span class="spinner">retrying…</span>' : '';
            el.innerHTML = '<span class="offline">● Backend offline</span> (' + esc(m.apiUrl) + ')' + retrying +
                '<div class="muted">' + esc(m.error || '') + '</div>' +
                '<div class="choices"><button id="retryStatus" class="secondary">Retry now</button></div>';
            return;
        }
        const h = m.health, s = m.stats;
        const provider = esc(h.cloud_provider);
        const model = h.cloud_model ? ' <code>' + esc(h.cloud_model) + '</code>' : '';
        let cloudState;
        if (h.cloud_valid) {
            cloudState = '<code>' + provider + '</code>' + model + ' ✓';
        } else if (h.cloud_provider === 'mock') {
            cloudState = '<code>mock</code>';
        } else {
            cloudState = '<code>' + provider + '</code>' + model + ' <span class="warn">⚠ ' +
                esc(h.cloud_detail || 'not configured') + '</span>';
        }
        const keyBtn = (!h.cloud_valid && h.cloud_provider !== 'mock')
            ? '<div class="choices"><button id="setApiKey" class="secondary">🔑 Set API key</button></div>'
            : '';
        const spend = (s.total_cost_usd != null && s.total_cost_usd > 0)
            ? '<span>Spend: <b>$' + Number(s.total_cost_usd).toFixed(4) + '</b></span>' : '';
        el.innerHTML = '● API online · cloud: ' + cloudState + ' · local: <code>' +
            esc(h.local_model) + '</code> · Ollama ' + (h.ollama ? '✅' : '❌') + keyBtn +
            '<div class="stats"><span>🖥️ Local: <b>' + esc(s.local_requests) + '</b></span><span>☁️ Cloud: <b>' +
            esc(s.cloud_requests) + '</b></span><span>Tokens saved: <b>' + fmt(s.total_tokens_saved) + '</b> (' +
            esc(s.cloud_token_reduction_pct) + '%)</span>' + spend + '</div>';
        const sys = document.getElementById('systemInfo');
        if (sys && h.system) {
            const g = h.system;
            sys.textContent = 'Host: ' + g.total_ram_gb + ' GB RAM' +
                (g.swap_total_gb ? ' + ' + g.swap_total_gb + ' GB swap' : '') +
                (g.has_gpu ? ' · ' + g.gpu_vram_gb + ' GB VRAM' : '') +
                ' · ' + g.cpu_count + ' CPU' + (g.cpu_count === 1 ? '' : 's');
        }
    }

    // ---- Cloud provider / model / key picker ------------------------------------------------

    function cloudModelLabel(m) {
        let label = m.display || m.id;
        if (m.context) label += ' · ' + Math.round(m.context / 1000) + 'k';
        if (m.in != null && m.out != null) label += ' · $' + m.in + '/$' + m.out;
        if (m.note) label += ' — ' + m.note;
        return label;
    }

    function renderCloud(info) {
        if (!info) return;
        cloudProviderSel.innerHTML = CLOUD_PROVIDERS
            .map(p => '<option value="' + esc(p) + '">' + esc(p) + '</option>').join('');
        cloudProviderSel.value = info.provider;
        cloudBaseUrlRow.hidden = info.provider !== 'custom';
        if (info.base_url) cloudBaseUrlInput.value = info.base_url;

        const models = info.models || [];
        const option = m => '<option value="' + esc(m.id) + '">' + esc(cloudModelLabel(m)) + '</option>';
        const rec = models.filter(m => m.recommended);
        const rest = models.filter(m => !m.recommended);
        let opts = '';
        if (rec.length) opts += '<optgroup label="Recommended">' + rec.map(option).join('') + '</optgroup>';
        if (rest.length) opts += '<optgroup label="All">' + rest.map(option).join('') + '</optgroup>';
        cloudModelSel.innerHTML = opts || '<option value="">(no models — check key/base URL)</option>';
        const current = info.current || info.default;
        if (current && models.some(m => m.id === current)) cloudModelSel.value = current;

        const cur = models.find(m => m.id === current);
        let state = info.valid ? 'key ✓' : (info.detail ? '⚠ ' + info.detail : 'key not set');
        state += ' · model: ' + (current || '—');
        if (cur && cur.in != null) state += ' · $' + cur.in + '/' + cur.out + ' per 1M';
        cloudStateEl.textContent = state;
    }

    // ---- Events ---------------------------------------------------------------------------

    document.getElementById('examples').innerHTML = EXAMPLES
        .map(p => '<button class="example" data-example="' + esc(p) + '">' + esc(p) + '</button>').join('');

    sendBtn.addEventListener('click', () => (busy ? stop() : send()));
    modelSelect.addEventListener('change', () => {
        const value = modelSelect.value;
        const rec = recommendedModels.find(m => m.name === value);
        const installed = installedModels.includes(value);
        const state = document.getElementById('modelState');
        loadingModel = installed && value ? value : null;
        if (state) state.textContent = installed || !value ? 'switching…' : 'checking download…';
        modelSelect.disabled = true;
        vscode.postMessage({
            type: 'selectLocalModel',
            model: value,
            installed,
            size_gb: rec ? rec.size_gb : null
        });
    });
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    cloudProviderSel.addEventListener('change', () => {
        vscode.postMessage({ type: 'selectCloudProvider', provider: cloudProviderSel.value });
    });
    cloudModelSel.addEventListener('change', () => {
        vscode.postMessage({ type: 'selectCloudModel', model: cloudModelSel.value });
    });
    cloudBaseUrlInput.addEventListener('change', () => {
        vscode.postMessage({ type: 'saveCloudBaseUrl', baseUrl: cloudBaseUrlInput.value });
    });
    cloudKeyInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); document.getElementById('saveCloudKey').click(); }
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && busy) { e.preventDefault(); stop(); }
    });
    document.addEventListener('click', e => {
        const link = e.target.closest('a[href]');
        if (link) {
            // Webviews can't navigate; the extension opens http(s) links in the browser.
            e.preventDefault();
            vscode.postMessage({ type: 'openLink', href: link.getAttribute('href') });
            return;
        }
        const target = e.target.closest('button');
        if (!target) return;
        if (target.id === 'retryStatus') { vscode.postMessage({ type: 'refreshStatus' }); return; }
        if (target.id === 'setApiKey') { vscode.postMessage({ type: 'setApiKey' }); return; }
        if (target.id === 'saveCloudKey') {
            vscode.postMessage({ type: 'saveCloudKey', provider: cloudProviderSel.value, key: cloudKeyInput.value });
            cloudKeyInput.value = '';
            return;
        }
        if (target.id === 'clearCloudKey') {
            vscode.postMessage({ type: 'clearCloudKey', provider: cloudProviderSel.value });
            return;
        }
        if (target.id === 'testCloud') { vscode.postMessage({ type: 'testCloud' }); return; }
        if (target.dataset.example) {
            input.value = target.dataset.example;
            input.focus();
            return;
        }
        if (target.dataset.codeAction) {
            const block = target.closest('.code-block');
            const code = block.querySelector('code').textContent.replace(/\n$/, '');
            if (target.dataset.codeAction === 'copy') {
                vscode.postMessage({ type: 'copy', text: code });
                const label = target.textContent;
                target.textContent = '✓ Copied';
                setTimeout(() => { target.textContent = label; }, 1500);
            } else {
                vscode.postMessage({ type: 'insert', code, language: block.dataset.lang || '' });
            }
            return;
        }
        if (busy) return;
        const id = Number(target.dataset.id);
        if (target.dataset.retry === 'analyze' && turns.has(id)) {
            turns.get(id).el = target.closest('.msg');
            analyze(id);
        } else if (target.dataset.retry === 'run') {
            run(id, target.dataset.retryRoute, target.closest('.msg'));
        } else if (target.dataset.route) {
            target.dataset.used = '1';
            run(id, target.dataset.route);
        }
    });

    window.addEventListener('message', event => {
        const m = event.data;
        const turn = turns.get(m.id);
        // A late reply for a turn that was cleared from the view: drop it.
        if (['analysis', 'stream', 'result', 'error', 'cancelled'].includes(m.type) && m.id !== undefined && !turn) {
            if (m.id === activeId && m.type !== 'stream') setBusy(false);
            return;
        }
        switch (m.type) {
            case 'cloudModels':
                renderCloud(m.info);
                break;
            case 'modelLoading': {
                loadingModel = m.model || null;
                const state = document.getElementById('modelState');
                if (state && m.model) {
                    state.innerHTML = '<span class="spinner">loading ' + esc(m.model) + '…</span>';
                }
                break;
            }
            case 'status':
                if (m.health) {
                    renderModelSelect(
                        m.health.local_models,
                        m.health.local_recommended,
                        m.health.local_pulling,
                        m.health.local_model,
                        m.localModel,
                        m.health.local_loaded
                    );
                }
                renderStatus(m);
                break;
            case 'context':
                document.getElementById('contextHint').textContent = 'Context: ' + m.info;
                break;
            case 'clear':
                clearChat();
                break;
            case 'analysis':
                turn.req = m.req;
                turn.analysis = m.analysis;
                renderAnalysis(m.id, m.analysis, m.contextInfo);
                setBusy(false);
                if (m.analysis.decision !== 'cloud') {
                    // Simple (or borderline) task: stay local automatically; the user can still go global after
                    turn.el.querySelector('button[data-route="local"]').dataset.used = '1';
                    run(m.id, 'local');
                }
                break;
            case 'stream':
                onStream(m.id, m.event, m.data);
                break;
            case 'result':
                renderResult(m.id, m.result);
                setBusy(false);
                break;
            case 'cancelled':
                renderCancelled(m.id);
                setBusy(false);
                break;
            case 'error': {
                const el = turn ? (turn.pendingEl || turn.el) : addCard('');
                el.innerHTML = '<div class="error">⚠️ ' + esc(m.message) + '</div>';
                if (turn) {
                    partialAnswer(el, endStream(turn));
                    turn.pendingEl = null;
                }
                setBusy(false);
                break;
            }
        }
    });

    updateEmptyState();
    vscode.postMessage({ type: 'ready' });
}());
