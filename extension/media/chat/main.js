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
    const tutEl = document.getElementById('tut');
    const scroller = document.getElementById('scroller');
    const cloudProviderSel = document.getElementById('cloudProvider');
    const cloudModelSel = document.getElementById('cloudModel');
    const cloudBaseUrlRow = document.getElementById('cloudBaseUrlRow');
    const cloudBaseUrlInput = document.getElementById('cloudBaseUrl');
    const cloudKeyInput = document.getElementById('cloudKey');
    const cloudStateEl = document.getElementById('cloudState');
    const modeSelect = document.getElementById('mode');
    const CLOUD_PROVIDERS = ['deepseek', 'anthropic', 'openai', 'custom', 'mock'];

    let nextId = 1;
    let busy = false;               // one request at a time, like the Streamlit UI
    let activeId = null;            // turn whose request is in flight (what Stop cancels)
    let stopGuard = null;           // Stop's safety timer: force-unblock if 'cancelled' is lost
    const STOP_GUARD_MS = 15000;
    const turns = new Map();        // id -> { prompt, req, analysis, el, pendingEl, stream } (agent: { agent, runId, ... })
    let agentHistory = [];          // finished agent tasks { prompt, summary }, sent along for follow-ups

    const PLACEHOLDERS = {
        ask: 'Ask LocalPilot about your repository… (Enter to send, Shift+Enter for newline)',
        agent: 'Describe the change you want. The agent reads the code, proposes edits for you to review, and runs tests…'
    };
    const savedState = (() => { try { return vscode.getState() || {}; } catch { return {}; } })();
    function applyMode(mode) {
        modeSelect.value = ['ask', 'agent-cloud', 'agent-local'].includes(mode) ? mode : 'ask';
        input.placeholder = modeSelect.value === 'ask' ? PLACEHOLDERS.ask : PLACEHOLDERS.agent;
    }
    applyMode(savedState.mode);

    const EXAMPLES = [
        'Explain what the selected code does',
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
    let fitInfo = {};          // model name -> {fits, comfortable, size_gb, min_ram_gb} (RAM-fit info)
    let loadingModel = null;   // model currently being loaded into memory

    // "45%" for a downloading model, or "" when Ollama hasn't reported progress yet.
    function pullTag(name) {
        const p = pullProgress[name];
        return p && p.state === 'pulling' && p.percent > 0 && p.percent < 100 ? ' ' + Math.round(p.percent) + '%' : '';
    }

    // Label suffix for a model the sidebar should flag as bigger than this machine holds well.
    function fitTag(name) {
        const f = fitInfo[name];
        if (!f) return '';
        if (f.fits === false) return ' — ⚠ too big for this machine';
        if (f.comfortable === false) return ' — may use swap';
        return '';
    }

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
            installed.forEach(m => opts.push('<option value="' + esc(m) + '">' + esc(m) + fitTag(m) + '</option>'));
            opts.push('</optgroup>');
        }
        if (recs.length) {
            opts.push('<optgroup label="Recommended — needs download">');
            recs.forEach(m => {
                const size = m.size_gb ? ' — ' + m.size_gb + ' GB' : '';
                const warn = m.comfortable === false ? ' (may use swap)' : '';
                const downloading = inFlight.includes(m.name) ? ' — downloading' + pullTag(m.name) + '…' : '';
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
            const lf = loaded && fitInfo[loaded];
            if (loadingModel === null && lf) {
                if (lf.fits === false) {
                    text += ' <span class="ramwarn">⚠ needs ~' + lf.min_ram_gb + ' GB RAM/VRAM — too big for this machine</span>';
                } else if (lf.comfortable === false) {
                    text += ' <span class="ramwarn">⚠ may use swap — answers slower</span>';
                }
            }
            if (inFlight.length) {
                text += ' · downloading: ' + esc(inFlight.map(n => n + pullTag(n)).join(', '));
            }
            state.innerHTML = text;
        }
    }

    // ---- Ollama server, first-run download, progress, model manager -----------------------

    let ollamaState = null;     // { state, error } from the extension's OllamaManager
    let lastHealth = null;      // latest /health payload (null while the backend is offline)
    let pullProgress = {};      // model -> { state, status, completed, total, percent }
    let setupDismissed = !!(vscode.getState() || {}).setupDismissed;

    function fmtBytes(n) {
        if (!n) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
        return (n / Math.pow(1024, i)).toFixed(i >= 3 ? 2 : 0) + ' ' + units[i];
    }

    function renderOllama() {
        const el = document.getElementById('ollamaState');
        if (!el || !ollamaState) return;
        const err = esc(ollamaState.error || '');
        switch (ollamaState.state) {
            case 'checking':
                el.innerHTML = '<span class="spinner">Checking Ollama server…</span>';
                break;
            case 'starting':
                el.innerHTML = '<span class="spinner">Ollama server was not running — starting it…</span>';
                break;
            case 'running':
                el.innerHTML = '<span class="ok">●</span> Ollama server started by TokenGuard';
                break;
            case 'external':
                el.innerHTML = '<span class="ok">●</span> Ollama server is running';
                break;
            case 'missing':
                el.innerHTML = '<span class="offline">● Ollama is not installed</span>' +
                    (err ? '<div class="muted">' + err + '</div>' : '') +
                    '<div class="choices"><button data-model-action="install-ollama">Install Ollama</button>' +
                    '<button class="secondary" data-model-action="start-ollama">Check again</button></div>';
                break;
            case 'installing':
                el.innerHTML = '<span class="spinner">Installing Ollama… follow the "Install Ollama" terminal</span>';
                break;
            case 'failed':
                el.innerHTML = '<span class="offline">● Could not start Ollama</span><div class="muted">' + err + '</div>' +
                    '<div class="choices"><button class="secondary" data-model-action="start-ollama">Try again</button>' +
                    '<button class="secondary" data-model-action="ollama-log">Show log</button></div>';
                break;
            default: // stopped (auto-start disabled)
                el.innerHTML = '<span class="offline">● Ollama server is not running</span>' +
                    (err ? '<div class="muted">' + err + '</div>' : '') +
                    '<div class="choices"><button class="secondary" data-model-action="start-ollama">Start Ollama</button></div>';
        }
        renderTopBar(); // the top row's dot and attention strip reflect Ollama problems
    }

    // A download the user just asked for shows its bar immediately, before the backend reports it.
    function optimisticPull() {
        return { state: 'pulling', status: 'starting…', completed: 0, total: 0, percent: 0, since: Date.now() };
    }

    function withOptimistic(reported) {
        const merged = { ...reported };
        Object.keys(pullProgress).forEach(name => {
            const p = pullProgress[name];
            if (p.since && !merged[name] && Date.now() - p.since < 10000) merged[name] = p;
        });
        return merged;
    }

    function activeDownloads() {
        return Object.keys(pullProgress).filter(name => pullProgress[name].state === 'pulling');
    }

    function installedNow(name) {
        const installed = (lastHealth && lastHealth.local_models) || [];
        return installed.includes(name) || installed.includes(name + ':latest');
    }

    function renderDownloads() {
        const el = document.getElementById('downloads');
        if (!el) return;
        const rows = [];
        Object.keys(pullProgress).forEach(name => {
            const p = pullProgress[name];
            if (p.state === 'pulling') {
                const known = p.total > 0;
                const pct = known ? Math.min(100, p.percent || 0) : 0;
                const detail = known ? pct.toFixed(0) + '% · ' + fmtBytes(p.completed) + ' / ' + fmtBytes(p.total) : '';
                rows.push('<div class="download"><div class="download-head"><span>⬇ <code>' + esc(name) + '</code></span>' +
                    '<button class="secondary" data-model-action="cancel" data-model="' + esc(name) + '">Cancel</button></div>' +
                    '<div class="progress' + (known ? '' : ' indeterminate') + '"><div style="width:' + pct + '%"></div></div>' +
                    '<div class="muted">' + esc(detail) + (detail && p.status ? ' · ' : '') + esc(p.status || 'starting…') + '</div></div>');
            } else if (p.state && p.state.startsWith('error') && !installedNow(name)) {
                rows.push('<div class="download failed"><div class="download-head"><span>✗ <code>' + esc(name) + '</code> failed</span>' +
                    '<button class="secondary" data-model-action="download" data-model="' + esc(name) + '">Retry</button></div>' +
                    '<div class="muted">' + esc(p.state.slice(6).trim()) + '</div></div>');
            }
        });
        el.innerHTML = rows.join('');
    }

    // No model on disk yet: ask before downloading several GB.
    function renderSetup() {
        const el = document.getElementById('setupCard');
        if (!el) return;
        const h = lastHealth;
        const show = h && h.ollama && !(h.local_models || []).length && !activeDownloads().length && !setupDismissed;
        el.hidden = !show;
        if (!show) return;
        const recs = h.local_recommended || [];
        if (!recs.length) {
            el.innerHTML = '<p><b>No local model installed.</b> None of the recommended models fit this machine\'s memory. ' +
                'You can still pull one with <code>ollama pull &lt;model&gt;</code>; cloud answers work without it.</p>' +
                '<div class="choices"><button class="secondary" data-model-action="setup-dismiss">Dismiss</button></div>';
            return;
        }
        const options = recs.map((m, i) => '<option value="' + esc(m.name) + '"' + (i === 0 ? ' selected' : '') + '>' +
            esc(m.name) + ' — ~' + esc(m.size_gb) + ' GB' + (m.comfortable === false ? ' (may use swap)' : '') + '</option>').join('');
        el.innerHTML = '<p><b>No local model installed.</b> TokenGuard answers simple questions with a local model so your code ' +
            'stays on your machine. Download one now?</p>' +
            '<select id="setupModel">' + options + '</select>' +
            '<div class="muted" id="setupNote">' + esc(recs[0].description || recs[0].note || '') + '</div>' +
            '<div class="choices"><button data-model-action="setup-download">Download</button>' +
            '<button class="secondary" data-model-action="setup-dismiss">Not now</button></div>';
    }

    function renderModelsList() {
        const el = document.getElementById('modelsList');
        if (!el) return;
        const h = lastHealth;
        if (!h) {
            el.innerHTML = '<div class="muted">Waiting for the backend…</div>';
            return;
        }
        if (!h.ollama) {
            el.innerHTML = '<div class="muted">Ollama is not reachable.</div>';
            return;
        }
        const sizes = h.local_sizes || {};
        const installed = h.local_models || [];
        const pulling = activeDownloads();
        const total = installed.reduce((sum, m) => sum + (sizes[m] || 0), 0);
        const rows = ['<div class="model-group">Installed' + (installed.length ? ' · ' + fmtBytes(total) + ' on disk' : '') + '</div>'];
        if (!installed.length) rows.push('<div class="muted">No models downloaded yet.</div>');
        installed.forEach(m => {
            const tag = m === h.local_loaded ? ' <span class="muted">(loaded)</span>' : '';
            rows.push('<div class="model-item"><span class="name" title="' + esc(m) + '"><code>' + esc(m) + '</code>' + tag + '</span>' +
                '<span class="size">' + esc(fmtBytes(sizes[m])) + '</span>' +
                '<button class="secondary danger" title="Delete from disk" data-model-action="delete" data-model="' + esc(m) +
                '" data-size="' + (sizes[m] || 0) + '">Delete</button></div>');
        });
        const recs = (h.local_recommended || []);
        if (recs.length) {
            rows.push('<div class="model-group">Available to download</div>');
            recs.forEach(m => {
                const busy = pulling.includes(m.name);
                rows.push('<div class="model-item"><span class="name" title="' + esc(m.description || m.name) + '"><code>' + esc(m.name) + '</code></span>' +
                    '<span class="size">~' + esc(m.size_gb) + ' GB</span>' +
                    '<button class="secondary" data-model-action="download" data-model="' + esc(m.name) + '"' +
                    (busy ? ' disabled>Downloading…' : '>Download') + '</button></div>');
            });
        }
        el.innerHTML = rows.join('');
    }

    function onModelAction(target) {
        const action = target.dataset.modelAction;
        const model = target.dataset.model || '';
        switch (action) {
            case 'start-ollama':
                ollamaState = { state: 'checking' };
                renderOllama();
                vscode.postMessage({ type: 'startOllama' });
                break;
            case 'install-ollama':
                vscode.postMessage({ type: 'installOllama' });
                break;
            case 'ollama-log':
                vscode.postMessage({ type: 'showOllamaLog' });
                break;
            case 'setup-download': {
                const sel = document.getElementById('setupModel');
                const name = sel ? sel.value : '';
                if (!name) return;
                // Permission given here: download, then make it the active local model.
                pullProgress[name] = optimisticPull();
                renderDownloads();
                renderSetup();
                vscode.postMessage({ type: 'selectLocalModel', model: name, installed: false, confirmed: true });
                break;
            }
            case 'setup-dismiss':
                setupDismissed = true;
                try { vscode.setState({ ...(vscode.getState() || {}), setupDismissed: true }); } catch { /* ignore */ }
                renderSetup();
                renderTopBar();
                break;
            case 'download':
                pullProgress[model] = optimisticPull();
                renderDownloads();
                renderModelsList();
                vscode.postMessage({ type: 'downloadModel', model });
                break;
            case 'cancel':
                target.disabled = true;
                target.textContent = 'Cancelling…';
                vscode.postMessage({ type: 'cancelDownload', model });
                break;
            case 'delete':
                vscode.postMessage({ type: 'deleteModel', model, size: Number(target.dataset.size) || 0 });
                break;
        }
    }

    // ---- Turns ----------------------------------------------------------------------------

    function setBusy(value, id) {
        busy = value;
        activeId = value ? id : null;
        if (!value) {
            clearTimeout(stopGuard); // the turn finished normally: the Stop safety net can rest
        }
        sendBtn.disabled = false;
        sendBtn.classList.toggle('stop', value);
        sendBtn.textContent = value ? '■ Stop' : 'Send Request';
        sendBtn.title = value ? 'Stop generating (Esc)' : 'Send (Enter)';
        document.querySelectorAll('button[data-route], button[data-retry]').forEach(b => {
            b.disabled = value || b.dataset.used === '1';
        });
    }

    // What a turn is doing: 'analyze' / 'run' while a request is out, 'done' once its card is
    // finalized. Stop and 'cancelled' use it to avoid replacing an already-finished card.
    function turnPhase(turn, phase) {
        if (turn) turn.phase = phase;
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
        if (modeSelect.value !== 'ask') {
            sendAgent(prompt, modeSelect.value === 'agent-local' ? 'local' : 'cloud');
            input.value = '';
            return;
        }
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
        turn.stopped = false;
        turnPhase(turn, 'analyze');
        setBusy(true, id);
        vscode.postMessage({ type: 'sendPrompt', id, prompt: turn.prompt });
    }

    /** Run the turn on `route`, rendering into `el` (a retried card) or a new card.
     *  `detail` is the local answer length: 'brief' (default) or 'detailed' ("More detail"). */
    function run(id, route, el, detail = 'brief') {
        const turn = turns.get(id);
        if (!turn || busy) return;
        const label = route === 'local'
            ? (detail === 'detailed' ? 'local LLM for a detailed explanation' : 'local LLM')
            : 'global (cloud) LLM';
        turn.pendingEl = el || addCard('');
        turn.pendingEl.innerHTML = '<span class="spinner">Asking the ' + label + '…</span>';
        turn.route = route;
        turn.detail = detail;
        turn.stopped = false;
        turnPhase(turn, 'run');
        endStream(turn);
        setBusy(true, id);
        vscode.postMessage({ type: 'run', id, req: { ...turn.req, detail }, route });
    }

    function stop() {
        if (!busy || activeId === null) return;
        const id = activeId;
        const turn = turns.get(id);
        if (turn && turn.phase === 'done') {
            // The card already rendered its final state; only the busy flag got stuck.
            // Just unblock: never stop on, or replace, a finished answer.
            setBusy(false);
            return;
        }
        sendBtn.disabled = true;
        sendBtn.textContent = 'Stopping…';
        // Freeze the answer now: whatever is still in flight from the model is dropped,
        // rather than rendering on until the cancellation round-trip completes.
        if (turn) {
            turn.stopped = true;
            turn.stoppedText = endStream(turn);
            const answer = turn.pendingEl && turn.pendingEl.querySelector('.answer');
            if (answer) fillMarkdown(answer, turn.stoppedText);
            const spinner = turn.pendingEl && turn.pendingEl.querySelector('.metrics .spinner');
            if (spinner) spinner.textContent = 'stopping…';
        }
        vscode.postMessage({ type: 'cancel', id });
        // Safety net: normally the extension answers with 'cancelled' immediately. If that
        // round-trip is lost or hangs, unblock locally so the next question can still be asked.
        clearTimeout(stopGuard);
        stopGuard = setTimeout(() => {
            if (busy && activeId === id) {
                if (turn && turn.phase !== 'done') renderCancelled(id);
                else turnPhase(turn, 'done');
                setBusy(false);
            }
        }, STOP_GUARD_MS);
    }

    function clearChat() {
        if (busy && activeId !== null) {
            vscode.postMessage({ type: 'cancel', id: activeId });
        }
        turns.clear();
        agentHistory = [];
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
            (a.repo_path && !files.length
                ? '<div class="warn">⚠ No context files were retrieved from the repo — the answer may be generic.</div>'
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
        if (!el || turn.stopped) return;
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
        const conf = r.confidence == null ? 'n/a' : Math.round(r.confidence * 100) + '%';
        const lowConf = isLocal && !!r.low_confidence;  // decided by the backend (confidence_threshold)
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
        let actions = '';
        if (isLocal) {
            if (r.more_available) {
                actions += '<button data-route="local" data-detail="detailed" data-id="' + id + '">📖 More detailed explanation</button>';
            }
            actions += '<button data-route="cloud" data-id="' + id + '" class="' + (lowConf ? '' : 'secondary') + '">' +
                (lowConf ? '☁️ Retry with global LLM' : 'Not satisfied? ☁️ Use global LLM') + '</button>';
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
            (r.truncated ? '<div class="muted">✂️ The answer reached its length limit.</div>' : '') +
            details +
            (trace ? accordion('Pipeline trace', '<ul>' + trace + '</ul>') : '') +
            (actions ? '<div class="choices">' + actions + '</div>' : '');
        fillMarkdown(el.querySelector('.answer'), r.answer);
        turn.pendingEl = null;
        turnPhase(turn, 'done');
    }

    function renderCancelled(id) {
        const turn = turns.get(id);
        const el = turn.pendingEl || turn.el;
        // Before the analysis arrives the whole turn is retried; after it, only the run.
        const retry = turn.pendingEl
            ? '<button class="secondary" data-retry="run" data-id="' + id + '" data-retry-route="' + esc(turn.route) +
                '" data-detail="' + esc(turn.detail || 'brief') + '">↻ Retry</button>'
            : '<button class="secondary" data-retry="analyze" data-id="' + id + '">↻ Retry</button>';
        el.innerHTML = '<div class="cancelled">⏹ Stopped.</div><div class="choices">' + retry + '</div>';
        const text = endStream(turn) || turn.stoppedText || '';
        turn.stopped = false;
        turn.stoppedText = '';
        partialAnswer(el, text);
        turn.pendingEl = null;
        turnPhase(turn, 'done');
    }

    // ---- Agent mode -----------------------------------------------------------------------
    // The agent works in steps (read, search, edit, run) shown live in its card. Its edits are
    // staged by the backend and listed at the end for review: Diff opens VS Code's diff editor,
    // Accept writes the file, Reject drops it. Commands wait for Run / Skip in the card.

    function sendAgent(prompt, route) {
        const id = nextId++;
        addCard('<b>You:</b> ' + esc(prompt), 'msg user');
        const el = addCard(
            '<span class="badge agent">🛠 Agent · ' + (route === 'local' ? 'local LLM' : 'global LLM') + '</span>' +
            '<div class="metrics agent-metrics"><span class="spinner">Starting the agent…</span></div>' +
            '<details class="acc agent-log" open><summary>Steps</summary><div class="acc-body agent-steps"></div></details>' +
            '<div class="agent-live muted"></div>' +
            '<div class="agent-final"></div>', 'msg agent');
        turns.set(id, { prompt, el, agent: true, route, stepCount: 0, phase: 'run' });
        setBusy(true, id);
        vscode.postMessage({ type: 'agentRun', id, prompt, route, history: agentHistory.slice(-3) });
    }

    function code(s) { return '<code>' + esc(s) + '</code>'; }

    function stepLabel(name, a) {
        a = a || {};
        switch (name) {
            case 'read_file':
                return 'Read ' + code(a.path) + (a.start_line || a.end_line
                    ? ' <span class="muted">lines ' + esc(a.start_line || 1) + '–' + esc(a.end_line || 'end') + '</span>' : '');
            case 'list_files': return 'List ' + code(a.path || '.') + (a.recursive ? ' <span class="muted">(recursive)</span>' : '');
            case 'search': return 'Search ' + code(a.pattern) + (a.glob ? ' in ' + code(a.glob) : '') +
                (a.path && a.path !== '.' ? ' under ' + code(a.path) : '');
            case 'edit_file': return 'Edit ' + code(a.path);
            case 'write_file': return 'Write ' + code(a.path);
            case 'delete_file': return 'Delete ' + code(a.path);
            case 'run_command': return 'Run ' + code(a.command) + (a.cwd && a.cwd !== '.' ? ' in ' + code(a.cwd) : '');
            default: return esc(name);
        }
    }

    function agentStepsEl(turn) { return turn.el.querySelector('.agent-steps'); }

    function appendStep(turn, html, cls) {
        const div = document.createElement('div');
        div.className = cls;
        div.innerHTML = html;
        agentStepsEl(turn).appendChild(div);
        return div;
    }

    function setAgentStatus(turn, html) {
        turn.el.querySelector('.agent-metrics').innerHTML = html;
    }

    function onAgentEvent(id, event, data) {
        const turn = turns.get(id);
        if (!turn || turn.stopped) return;
        switch (event) {
            case 'start':
                turn.runId = data.run_id;
                turn.model = data.model;
                setAgentStatus(turn, '<b>Model:</b> ' + esc(data.model) + ' · <span class="spinner">working…</span>');
                if (data.warning) {
                    turn.el.querySelector('.agent-metrics').insertAdjacentHTML('afterend', '<div class="warn">⚠️ ' + esc(data.warning) + '</div>');
                }
                break;
            case 'progress':
                // A slow local step: show that tokens are still coming.
                setAgentStatus(turn, '<b>Model:</b> ' + esc(turn.model || '') +
                    ' · <span class="spinner">generating… ' + fmt(data.tokens) + ' tokens</span>');
                break;
            case 'notice':
                appendStep(turn, '<span class="warn">⚠ ' + esc(data.text) + '</span>', 'agent-step');
                break;
            case 'thought': {
                const div = appendStep(turn, '', 'agent-thought answer');
                fillMarkdown(div, data.text);
                break;
            }
            case 'tool_call':
                turn.stepCount++;
                appendStep(turn, '<span class="step-icon spinner">…</span> ' + stepLabel(data.name, data.args), 'agent-step')
                    .dataset.call = data.call_id;
                break;
            case 'tool_result': {
                setAgentStatus(turn, '<b>Model:</b> ' + esc(turn.model || '') + ' · <span class="spinner">working…</span>');
                const step = agentStepsEl(turn).querySelector('.agent-step[data-call="' + CSS.escape(data.call_id) + '"]');
                if (!step) break;
                const icon = step.querySelector('.step-icon');
                icon.className = 'step-icon ' + (data.ok ? 'ok' : 'fail');
                icon.textContent = data.ok ? '✓' : '✗';
                if (data.output) {
                    const exit = data.name === 'run_command' && /\[exit code (-?\d+)\]/.exec(data.output);
                    const label = exit ? 'output · exit ' + exit[1] : (data.ok ? 'output' : 'error');
                    if (exit && exit[1] !== '0') icon.className = 'step-icon fail';
                    step.insertAdjacentHTML('beforeend', accordion(esc(label), '<pre class="tool-out">' + esc(data.output) + '</pre>'));
                }
                break;
            }
            case 'changes': {
                const list = data.changes || [];
                turn.el.querySelector('.agent-live').innerHTML = list.length
                    ? 'Staged so far: ' + list.map(c => code(c.path) + ' <span class="adds">+' + esc(c.additions) +
                        '</span> <span class="dels">−' + esc(c.deletions) + '</span>').join(', ')
                    : '';
                break;
            }
            case 'approval':
                if (data.kind === 'sensitive_files') {
                    const files = data.files || [];
                    const card = appendStep(turn,
                        '<div><b>Copy files that may hold secrets into the sandbox?</b> <span class="muted">(the sandbox copies the whole workspace, gitignored files included, to run commands; these look like credentials)</span></div>' +
                        '<pre class="tool-out">' + files.map(esc).join('\n') + '</pre>' +
                        '<div class="choices">' +
                            '<button data-approve="run" data-id="' + id + '" data-request="' + esc(data.request_id) + '">Include</button>' +
                            '<button class="secondary" data-approve="skip" data-id="' + id + '" data-request="' + esc(data.request_id) + '" title="Run commands without these files">Leave out</button>' +
                        '</div>', 'agent-approval');
                    card.dataset.request = data.request_id;
                    card.dataset.kind = data.kind;
                    setAgentStatus(turn, '<b>Model:</b> ' + esc(turn.model || '') + ' · <span class="warn">waiting for your approval</span>');
                    break;
                }
                appendStep(turn,
                    '<div><b>Run this command?</b> <span class="muted">(in a sandbox copy of the workspace with the proposed changes; your files are not touched)</span></div>' +
                    '<pre class="tool-out">' + esc(data.command) + '</pre>' +
                    (data.cwd && data.cwd !== '.' ? '<div class="muted">in ' + code(data.cwd) + '</div>' : '') +
                    '<div class="choices">' +
                        '<button data-approve="run" data-id="' + id + '" data-request="' + esc(data.request_id) + '">▶ Run</button>' +
                        '<button class="secondary" data-approve="always" data-id="' + id + '" data-request="' + esc(data.request_id) + '" title="Run this and every further command in this task without asking">Run all for this task</button>' +
                        '<button class="secondary" data-approve="skip" data-id="' + id + '" data-request="' + esc(data.request_id) + '">Skip</button>' +
                    '</div>', 'agent-approval').dataset.request = data.request_id;
                setAgentStatus(turn, '<b>Model:</b> ' + esc(turn.model || '') + ' · <span class="warn">waiting for your approval</span>');
                break;
            case 'approval_resolved': {
                const card = agentStepsEl(turn).querySelector('.agent-approval[data-request="' + CSS.escape(data.request_id) + '"]');
                const choices = card && card.querySelector('.choices');
                const labels = card && card.dataset.kind === 'sensitive_files' ? ['✓ Included', '✗ Left out'] : ['✓ Approved', '✗ Skipped'];
                if (choices) choices.outerHTML = '<div class="muted">' + labels[data.approved ? 0 : 1] + '</div>';
                setAgentStatus(turn, '<b>Model:</b> ' + esc(turn.model || '') + ' · <span class="spinner">working…</span>');
                break;
            }
        }
    }

    function changeRow(id, c) {
        const letter = { added: 'A', modified: 'M', deleted: 'D' }[c.status] || '?';
        return '<div class="change-row" data-path="' + esc(c.path) + '">' +
            '<span class="chg-status ' + esc(c.status) + '" title="' + esc(c.status) + '">' + letter + '</span>' +
            '<a href="#" class="chg-path" data-agent="open" data-id="' + id + '" data-path="' + esc(c.path) + '" title="Show the diff">' + esc(c.path) + '</a>' +
            '<span class="adds">+' + esc(c.additions) + '</span><span class="dels">−' + esc(c.deletions) + '</span>' +
            '<span class="row-actions">' +
                '<button class="icon-btn" data-agent="review" data-id="' + id + '" data-path="' + esc(c.path) + '" title="Show the change in the file itself, with Accept / Reject per change">Review in file</button>' +
                '<button class="icon-btn" data-agent="diff" data-id="' + id + '" data-path="' + esc(c.path) + '" title="Open in the side-by-side diff editor">Diff</button>' +
                '<button class="icon-btn accept" data-agent="accept" data-id="' + id + '" data-path="' + esc(c.path) + '" title="Write this change to the file">✓ Accept</button>' +
                '<button class="icon-btn reject" data-agent="reject" data-id="' + id + '" data-path="' + esc(c.path) + '" title="Discard this change">✗ Reject</button>' +
            '</span></div>';
    }

    /** Run the same task again on the other model (e.g. after a small local model gave up). */
    function retryButton(id, turn) {
        const other = turn.route === 'local' ? 'cloud' : 'local';
        return '<div class="choices"><button class="secondary" data-agent="retry" data-route="' + other + '" data-id="' + id + '">' +
            (other === 'cloud' ? '☁️ Retry with global LLM' : '🖥️ Retry with local LLM') + '</button></div>';
    }

    function renderAgentResult(id, r) {
        const turn = turns.get(id);
        const changes = r.changes || [];
        const commands = r.commands || [];
        const log = turn.el.querySelector('.agent-log');
        log.open = false;
        log.querySelector('summary').textContent = 'Steps · ' + turn.stepCount + ' tool call' + (turn.stepCount === 1 ? '' : 's');
        turn.el.querySelector('.agent-live').innerHTML = '';
        setAgentStatus(turn, '<b>Model:</b> ' + esc(r.model) + ' · ' + esc(r.steps) + ' step' + (r.steps === 1 ? '' : 's') +
            ' · ' + fmt(r.latency_ms) + ' ms' +
            (r.route === 'cloud'
                ? ' · ' + fmt(r.input_tokens) + ' in / ' + fmt(r.output_tokens) + ' out tokens' +
                  (r.secrets_masked ? ' · ' + esc(r.secrets_masked) + ' secret(s) masked' : '')
                : ' · <b>0</b> tokens sent to cloud'));
        const cmdList = commands.map(c => '<li>' + code(c.command) + ' — ' +
            (c.declined ? 'skipped' : (c.exit_code === 0 ? '✓ exit 0' : '✗ exit ' + esc(c.exit_code))) + '</li>').join('');
        const panel = changes.length
            ? '<div class="agent-changes">' +
                '<div class="agent-changes-head"><b>Proposed changes · ' + changes.length + ' file' + (changes.length === 1 ? '' : 's') + '</b>' +
                    '<span class="row-actions">' +
                        '<button class="icon-btn accept" data-agent="accept-all" data-id="' + id + '">✓ Accept all</button>' +
                        '<button class="icon-btn reject" data-agent="reject-all" data-id="' + id + '">✗ Reject all</button>' +
                    '</span></div>' +
                changes.map(c => changeRow(id, c)).join('') +
                '<div class="muted">Nothing is saved until you accept. Changed files open with the edits marked in place ' +
                    '(red = removed, green = added) and Accept / Reject above each change.</div>' +
              '</div>'
            : '<div class="muted">No file changes proposed.</div>';
        const final = turn.el.querySelector('.agent-final');
        final.innerHTML =
            (r.is_mock ? '<div class="warn">⚠️ Mock agent: no real model ran.</div>' : '') +
            (r.stopped_reason === 'max_steps' ? '<div class="warn">The agent hit its step limit before finishing.</div>' : '') +
            (r.stopped_reason === 'invalid_tool_calls' ? '<div class="warn">The model could not make valid tool calls.</div>' : '') +
            '<div class="answer"></div>' + panel +
            (cmdList ? accordion('Commands run · ' + commands.length, '<ul>' + cmdList + '</ul>') : '') +
            retryButton(id, turn);
        fillMarkdown(final.querySelector('.answer'), r.summary || '(no summary)');
        turn.changePaths = changes.map(c => c.path);
        turnPhase(turn, 'done');
        agentHistory.push({ prompt: turn.prompt, summary: String(r.summary || '').slice(0, 1500) });
    }

    function renderAgentApplied(id, results) {
        const turn = turns.get(id);
        (results || []).forEach(res => {
            const row = turn.el.querySelector('.change-row[data-path="' + CSS.escape(res.path) + '"]');
            if (!row) return;
            const actions = row.querySelector('.row-actions');
            const label = row.querySelector('.chg-reviewing');
            if (label) label.remove();
            if (res.state === 'accepted' || res.state === 'partial') {
                actions.innerHTML = '<span class="ok">' + (res.state === 'partial' ? '✓ Partly accepted' : '✓ Accepted') + '</span>';
            } else if (res.state === 'cancelled') {
                row.insertAdjacentHTML('beforeend', '<div class="muted row-note">Review closed without a decision; still pending.</div>');
            } else if (res.state === 'rejected') {
                actions.innerHTML = '<span class="muted">✗ Rejected</span>';
                row.classList.add('rejected');
            } else if (res.reason) {
                row.insertAdjacentHTML('beforeend', '<div class="warn row-note">Not applied: ' + esc(res.reason) + '</div>');
            }
        });
        const pending = turn.el.querySelectorAll('.change-row .row-actions button[data-agent="accept"]').length;
        if (!pending) {
            const head = turn.el.querySelector('.agent-changes-head .row-actions');
            if (head) head.innerHTML = '';
        }
    }

    function renderAgentStopped(id, message) {
        const turn = turns.get(id);
        turn.stopped = false;
        turnPhase(turn, 'done');
        turn.el.querySelectorAll('.agent-steps .step-icon.spinner').forEach(i => { i.className = 'step-icon fail'; i.textContent = '■'; });
        turn.el.querySelectorAll('.agent-approval .choices').forEach(c => { c.outerHTML = '<div class="muted">not answered</div>'; });
        setAgentStatus(turn, '<b>Model:</b> ' + esc(turn.model || '?'));
        turn.el.querySelector('.agent-final').innerHTML = (message
            ? '<div class="error">⚠️ ' + esc(message) + '</div><div class="muted">No files were changed.</div>'
            : '<div class="cancelled">⏹ Stopped. No files were changed.</div>') + retryButton(id, turn);
    }

    function onAgentButton(target) {
        const id = Number(target.dataset.id);
        const turn = turns.get(id);
        if (!turn) return;
        if (target.dataset.approve) {
            target.closest('.choices').querySelectorAll('button').forEach(b => { b.disabled = true; });
            vscode.postMessage({
                type: 'agentApprove', id, runId: turn.runId, requestId: target.dataset.request,
                approved: target.dataset.approve !== 'skip', always: target.dataset.approve === 'always'
            });
            return;
        }
        const action = target.dataset.agent;
        if (action === 'retry') {
            if (!busy) sendAgent(turn.prompt, target.dataset.route);
            return;
        }
        const pending = () => Array.from(turn.el.querySelectorAll('.change-row'))
            .filter(r => r.querySelector('button[data-agent="accept"]')).map(r => r.dataset.path);
        if (action === 'diff') vscode.postMessage({ type: 'agentDiff', id, path: target.dataset.path });
        else if (action === 'review') vscode.postMessage({ type: 'agentReview', id, path: target.dataset.path });
        else if (action === 'open') vscode.postMessage({ type: 'agentOpen', id, path: target.dataset.path });
        else if (action === 'accept') vscode.postMessage({ type: 'agentAccept', id, paths: [target.dataset.path] });
        else if (action === 'reject') vscode.postMessage({ type: 'agentReject', id, paths: [target.dataset.path] });
        else if (action === 'accept-all') vscode.postMessage({ type: 'agentAccept', id, paths: pending() });
        else if (action === 'reject-all') vscode.postMessage({ type: 'agentReject', id, paths: pending() });
    }

    // ---- Status ---------------------------------------------------------------------------
    // One compact row: a colored dot, a short status line and a gear button that opens the
    // settings popover. Anything that needs action (offline backend, Ollama problems, missing
    // API key, startup progress) is rendered into the #attention strip below it, so the calm
    // case leaves just the dot + model names.

    let lastStatusMsg = null;   // last 'status' message; renderTopBar re-renders from it

    function ollamaProblem() {
        // The Ollama states that keep the user from running a local model at all; null = fine/busy.
        const s = ollamaState && ollamaState.state;
        return s === 'missing' || s === 'failed' || s === 'stopped' ? s : null;
    }

    /** The gear's red dot: show it whenever the settings panel holds something to fix. */
    function settingsNeedsAttention(m) {
        if (m && (m.starting || !m.online)) return true;
        if (ollamaProblem() || (ollamaState && ollamaState.state === 'installing')) return true;
        const h = m && m.health;
        if (h && !h.cloud_valid && h.cloud_provider !== 'mock') return true;
        if (!activeDownloads().length) {
            const show = !!lastHealth && lastHealth.ollama && !(lastHealth.local_models || []).length;
            if (show && !setupDismissed) return true;
        }
        return false;
    }

    function dotClass(m) {
        if (m && m.starting) return 'pulse';
        if (!m || !m.online || ollamaProblem()) return 'bad';
        if (loadingModel) return 'pulse';
        return 'ok';
    }

    function renderTopBar() {
        const m = lastStatusMsg;
        const dot = document.getElementById('statusDot');
        const text = document.getElementById('statusText');
        const panel = document.getElementById('settingsPanel');
        const attention = document.getElementById('attention');
        const badge = document.getElementById('settingsBadge');
        if (!dot || !text || !attention) return;

        const at = [];  // attention rows (HTML strings)
        if (!m) {
            dot.className = 'status-dot pulse';
            text.innerHTML = '<span class="spinner">Connecting to LocalPilot backend…</span>';
        } else if (m.starting) {
            dot.className = 'status-dot pulse';
            text.innerHTML = '<span class="spinner">Starting LocalPilot backend…</span>';
            const p = m.progress;
            const pct = p ? Math.max(0, Math.min(100, Math.round(p.percent))) : 0;
            const bar = pct > 0
                ? '<div class="progress"><div class="progress-fill" style="width:' + pct + '%"></div></div>'
                : '<div class="progress indeterminate"><div class="progress-fill"></div></div>';
            at.push(bar + (p && p.step ? '<div class="muted">' + (pct > 0 ? pct + '% · ' : '') + esc(p.step) + '</div>' : ''));
        } else if (!m.online) {
            dot.className = 'status-dot bad';
            text.textContent = 'Backend offline' + (m.retrying ? ' — retrying…' : '');
            at.push('<div class="muted">' + esc(m.error || '') + '</div>' +
                '<div class="choices"><button id="retryStatus" class="secondary">Retry now</button></div>');
        } else {
            const h = m.health;
            dot.className = 'status-dot ' + dotClass(m);
            let bits = ['local: <code>' + esc(h.local_model || '—') + '</code>'];
            if (loadingModel) bits.push('<span class="spinner">loading ' + esc(loadingModel) + '…</span>');
            bits.push('cloud: <code>' + esc(h.cloud_provider) + '</code>' +
                (h.cloud_valid ? ' ✓' : ' <span class="warn">⚠</span>'));
            text.innerHTML = bits.join(' · ');
            const problem = ollamaProblem();
            if (problem) {
                const err = ollamaState.error ? '<div class="muted">' + esc(ollamaState.error) + '</div>' : '';
                if (problem === 'missing') {
                    at.push('<div><span class="offline">● Ollama is not installed</span></div>' + err +
                        '<div class="choices"><button data-model-action="install-ollama">Install Ollama</button>' +
                        '<button class="secondary" data-model-action="start-ollama">Check again</button></div>');
                } else if (problem === 'failed') {
                    at.push('<div><span class="offline">● Could not start Ollama</span></div>' + err +
                        '<div class="choices"><button class="secondary" data-model-action="start-ollama">Try again</button>' +
                        '<button class="secondary" data-model-action="ollama-log">Show log</button></div>');
                } else {
                    at.push('<div><span class="offline">● Ollama server is not running</span></div>' + err +
                        '<div class="choices"><button class="secondary" data-model-action="start-ollama">Start Ollama</button></div>');
                }
            } else if (ollamaState && ollamaState.state === 'installing') {
                at.push('<div><span class="spinner">Installing Ollama… follow the "Install Ollama" terminal</span></div>');
            }
            if (!h.cloud_valid && h.cloud_provider !== 'mock') {
                at.push('<div><span class="warn">⚠ Cloud key not set for <code>' + esc(h.cloud_provider) + '</code></span>' +
                    '<div class="muted">' + esc(h.cloud_detail || 'not configured') + '</div>' +
                    '<div class="choices"><button id="setApiKey" class="secondary">🔑 Set API key</button></div></div>');
            }
            const stats = m.stats;
            const spend = (stats.total_cost_usd != null && stats.total_cost_usd > 0)
                ? '<span>Spend: <b>$' + Number(stats.total_cost_usd).toFixed(4) + '</b></span>' : '';
            const statsBar = document.getElementById('statsBar');
            if (statsBar) statsBar.innerHTML = '<span>🖥️ Local: <b>' + esc(stats.local_requests) + '</b></span>' +
                '<span>☁️ Cloud: <b>' + esc(stats.cloud_requests) + '</b></span>' +
                '<span>Tokens saved: <b>' + fmt(stats.total_tokens_saved) + '</b> (' +
                esc(stats.cloud_token_reduction_pct) + '%)</span>' + spend;
        }
        attention.innerHTML = at.join('');
        attention.hidden = !at.length;
        if (badge) badge.hidden = !settingsNeedsAttention(m);
        if (panel && !panel.hidden) placePanel();
    }

    function renderStatus(m) {
        lastStatusMsg = m;
        renderTopBar();
        const sys = document.getElementById('systemInfo');
        if (sys && m.health && m.health.system) {
            const g = m.health.system;
            // "used / total" when the backend reports available/free memory, else just the total.
            const usage = (total, free) => {
                const t = Number(total) || 0;
                if (free == null) return t + ' GB';
                const used = Math.max(0, Math.round((t - Number(free)) * 10) / 10);
                return used + ' / ' + t + ' GB';
            };
            sys.textContent = 'Host: ' + usage(g.total_ram_gb, g.available_ram_gb) + ' RAM' +
                (g.swap_total_gb ? ' + ' + usage(g.swap_total_gb, g.swap_free_gb) + ' swap' : '') +
                (g.has_gpu ? ' · ' + g.gpu_vram_gb + ' GB VRAM' : '') +
                ' · ' + g.cpu_count + ' CPU' + (g.cpu_count === 1 ? '' : 's');
            sys.title = 'used / total · refreshes about every 30s';
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

    // First-run tutorial: a 3-step walkthrough (Next/Back/Skip), dismissed permanently on either
    // path — the flag persists in the webview state, like setupDismissed.
    if (savedState.tutorialDone) {
        tutEl.remove();
    } else {
        const tutLast = tutEl.querySelectorAll('.tut-step').length - 1;
        let tutStep = 0;
        const tutBack = document.getElementById('tutBack');
        const tutNext = document.getElementById('tutNext');
        const tutNum = document.getElementById('tutNum');

        function tutShow() {
            tutEl.querySelectorAll('.tut-step').forEach((el, i) => { el.hidden = i !== tutStep; });
            if (tutNum) tutNum.textContent = String(tutStep + 1);
            if (tutBack) tutBack.disabled = tutStep === 0;
            if (tutNext) tutNext.textContent = tutStep === tutLast ? 'Finish' : 'Next';
        }
        const tutEnd = () => {
            tutEl.remove();
            try {
                vscode.setState({ ...(vscode.getState() || {}), tutorialDone: true });
            } catch { /* ignore */ }
        };

        tutEl.hidden = false;
        tutShow();
        if (tutNext) tutNext.addEventListener('click', () => {
            if (tutStep >= tutLast) tutEnd();
            else { tutStep++; tutShow(); }
        });
        if (tutBack) tutBack.addEventListener('click', () => {
            if (tutStep > 0) { tutStep--; tutShow(); }
        });
        const tutSkip = document.getElementById('tutSkip');
        if (tutSkip) tutSkip.addEventListener('click', tutEnd);
    }

    sendBtn.addEventListener('click', () => (busy ? stop() : send()));
    modelSelect.addEventListener('change', () => {
        const value = modelSelect.value;
        const rec = recommendedModels.find(m => m.name === value);
        const installed = installedModels.includes(value);
        const state = document.getElementById('modelState');
        loadingModel = installed && value ? value : null;
        if (state) state.textContent = installed || !value ? 'switching…' : 'checking download…';
        renderTopBar();
        modelSelect.disabled = true;
        vscode.postMessage({
            type: 'selectLocalModel',
            model: value,
            installed,
            size_gb: rec ? rec.size_gb : null
        });
    });
    document.addEventListener('change', e => {
        if (e.target.id !== 'setupModel') return;
        const rec = (lastHealth && lastHealth.local_recommended || []).find(m => m.name === e.target.value);
        const note = document.getElementById('setupNote');
        if (note) note.textContent = rec ? (rec.description || rec.note || '') : '';
    });
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    modeSelect.addEventListener('change', () => {
        applyMode(modeSelect.value);
        try { vscode.setState({ ...(vscode.getState() || {}), mode: modeSelect.value }); } catch { /* ignore */ }
        input.focus();
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
    // Settings popover: opens from the gear button, floats over the chat, and closes on
    // Escape / outside click. Holds cloud config, the local model picker and the model manager.
    const settingsBtn = document.getElementById('settingsBtn');
    const settingsPanel = document.getElementById('settingsPanel');
    function placePanel() {
        if (settingsPanel.hidden) return;
        const bar = settingsBtn.getBoundingClientRect();
        const top = bar.bottom + 4;
        settingsPanel.style.top = top + 'px';
        settingsPanel.style.right = window.innerWidth - bar.right + 'px';
        settingsPanel.style.maxHeight = Math.max(160, window.innerHeight - top - 8) + 'px';
    }
    settingsBtn.addEventListener('click', () => {
        settingsPanel.hidden = !settingsPanel.hidden;
        if (!settingsPanel.hidden) {
            placePanel();
            renderModelsList(); // fresh list every time the panel opens
        }
    });
    window.addEventListener('resize', placePanel);
    document.addEventListener('mousedown', e => {
        if (!settingsPanel.hidden && !settingsPanel.contains(e.target) &&
            e.target !== settingsBtn && !settingsBtn.contains(e.target)) {
            settingsPanel.hidden = true;
        }
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !settingsPanel.hidden) {
            settingsPanel.hidden = true;
            e.stopPropagation();
        }
    }, true);

    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && busy) { e.preventDefault(); stop(); }
    });
    document.addEventListener('click', e => {
        const agentEl = e.target.closest('[data-agent], button[data-approve]');
        if (agentEl) {
            e.preventDefault();
            onAgentButton(agentEl);
            return;
        }
        const link = e.target.closest('a[href]');
        if (link) {
            // Webviews can't navigate; the extension opens http(s) links in the browser.
            e.preventDefault();
            vscode.postMessage({ type: 'openLink', href: link.getAttribute('href') });
            return;
        }
        const target = e.target.closest('button');
        if (!target) return;
        if (target.dataset.modelAction) { onModelAction(target); return; }
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
            run(id, target.dataset.retryRoute, target.closest('.msg'), target.dataset.detail);
        } else if (target.dataset.route) {
            target.dataset.used = '1';
            run(id, target.dataset.route, undefined, target.dataset.detail);
        }
    });

    window.addEventListener('message', event => {
        const m = event.data;
        const turn = turns.get(m.id);
        // A late reply for a turn that was cleared from the view: drop it.
        if (['analysis', 'stream', 'result', 'error', 'cancelled', 'agentEvent', 'agentResult', 'agentApplied', 'agentReviewing'].includes(m.type)
            && m.id !== undefined && !turn) {
            if (m.id === activeId && m.type !== 'stream') setBusy(false);
            return;
        }
        try {
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
                renderTopBar();
                break;
            }
            case 'ollama':
                ollamaState = { state: m.state, error: m.error };
                renderOllama();
                break;
            case 'pullProgress':
                pullProgress = withOptimistic(m.progress || {});
                renderDownloads();
                renderModelsList();
                renderSetup();
                renderTopBar();
                break;
            case 'status':
                lastHealth = m.online ? m.health : null;
                fitInfo = m.health?.fit || {};
                if (m.health) {
                    pullProgress = withOptimistic(m.health.local_pull_progress || {});
                }
                renderDownloads();
                renderModelsList();
                renderSetup();
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
                if (turn.stopped) { // Stop raced the analysis: don't start a run behind the user's back
                    renderCancelled(m.id);
                    setBusy(false);
                    break;
                }
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
                // Finished just as Stop was pressed: honor the Stop, keep what was shown.
                if (turn.stopped) renderCancelled(m.id);
                else renderResult(m.id, m.result);
                setBusy(false);
                break;
            case 'agentEvent':
                onAgentEvent(m.id, m.event, m.data);
                break;
            case 'agentResult':
                if (turn.stopped) renderAgentStopped(m.id);
                else renderAgentResult(m.id, m.result);
                setBusy(false);
                break;
            case 'agentApplied':
                renderAgentApplied(m.id, m.results);
                break;
            case 'agentReviewing': {
                const row = turn.el.querySelector('.change-row[data-path="' + CSS.escape(m.path) + '"]');
                if (row && !row.querySelector('.chg-reviewing')) {
                    row.querySelector('.chg-path').insertAdjacentHTML('afterend', '<span class="chg-reviewing" title="Open in the editor with the changes marked">● in editor</span>');
                    row.querySelectorAll('.row-note').forEach(n => n.remove());
                }
                break;
            }
            case 'cancelled':
                // A 'done' turn only had its busy flag stuck: keep the finished card.
                if (turn.phase !== 'done') {
                    if (turn.agent) renderAgentStopped(m.id);
                    else renderCancelled(m.id);
                }
                setBusy(false);
                break;
             case 'error': {
                if (turn && turn.agent) {
                    renderAgentStopped(m.id, turn.stopped ? '' : m.message);
                    setBusy(false);
                    break;
                }
                if (turn && turn.stopped) { // an error from a run the user already stopped
                    renderCancelled(m.id);
                    setBusy(false);
                    break;
                }
                const el = turn ? (turn.pendingEl || turn.el) : addCard('');
                el.innerHTML = '<div class="error">⚠️ ' + esc(m.message) + '</div>';
                if (turn) {
                    partialAnswer(el, endStream(turn));
                    turn.pendingEl = null;
                    turnPhase(turn, 'done');
                }
                setBusy(false);
                break;
            }
            }
        } catch (err) {
            // A broken response must never leave the UI busy (or half-rendered) forever.
            addCard('<div class="error">⚠️ Could not render the response: ' + esc(err && err.message || err) +
                '</div>');
            setBusy(false);
        }
    });

    // Resizable composer: drag the handle between the conversation and the composer.
    // Size persists in localStorage so the layout survives a reload.
    function initResizers() {
        const container = document.querySelector('.container');
        if (!container) return;
        const pane = { el: document.querySelector('.bottom'), dir: -1, key: 'lp.pane.bottom' };
        const MIN = 56;
        const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
        const maxHeight = () => Math.max(MIN, container.getBoundingClientRect().height - MIN);
        const setHeight = h => {
            const height = clamp(h, MIN, maxHeight());
            pane.el.style.height = height + 'px';
            pane.el.style.flex = 'none';
            try { localStorage.setItem(pane.key, String(height)); } catch { /* storage may be off */ }
        };
        const handle = document.getElementById('bottomResizer');
        if (!handle || !pane.el) return;
        try {
            const saved = Number(localStorage.getItem(pane.key));
            if (saved) pane.el.style.height = clamp(saved, MIN, maxHeight()) + 'px';
        } catch { /* ignore */ }
        handle.addEventListener('mousedown', e => {
            e.preventDefault();
            const startY = e.clientY;
            const startH = pane.el.getBoundingClientRect().height;
            handle.classList.add('active');
            document.body.classList.add('resizing');
            const onMove = ev => setHeight(startH + pane.dir * (ev.clientY - startY));
            const onUp = () => {
                handle.classList.remove('active');
                document.body.classList.remove('resizing');
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup', onUp);
            };
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onUp);
        });
        handle.addEventListener('keydown', e => {
            if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
            e.preventDefault();
            const step = (e.key === 'ArrowDown' ? 1 : -1) * pane.dir * 24;
            setHeight(pane.el.getBoundingClientRect().height + step);
        });
    }

    initResizers();
    updateEmptyState();
    vscode.postMessage({ type: 'ready' });
}());
