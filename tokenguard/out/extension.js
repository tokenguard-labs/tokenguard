"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const backend_1 = require("./backend");
const ANALYZE_TIMEOUT_MS = 60_000;
const CHAT_TIMEOUT_MS = 600_000; // local 7B models can be slow on a cold start
let backend;
function activate(context) {
    console.log('⚡ LocalPilot extension activated successfully!');
    const output = vscode.window.createOutputChannel('LocalPilot Backend');
    backend = new backend_1.BackendManager(context.extensionPath, output, apiUrl);
    context.subscriptions.push(output, backend);
    const provider = new LocalPilotViewProvider();
    context.subscriptions.push(backend.onDidChangeState(() => provider.refreshStatus()));
    // Register Webview Provider
    context.subscriptions.push(vscode.window.registerWebviewViewProvider('localpilot.chatView', provider, {
        webviewOptions: { retainContextWhenHidden: true }
    }));
    // Register a Command to force-open the view via Command Palette (Ctrl+Shift+P)
    context.subscriptions.push(vscode.commands.registerCommand('tokenguard.openChat', () => {
        vscode.commands.executeCommand('localpilot.chatView.focus');
    }), vscode.commands.registerCommand('tokenguard.restartBackend', async () => {
        await backend?.restart();
        if (backend?.state === 'external') {
            void vscode.window.showInformationMessage(`LocalPilot backend at ${apiUrl()} was started outside VS Code; restart it there.`);
        }
    }), vscode.commands.registerCommand('tokenguard.showBackendLog', () => output.show()));
    // Spawn the backend now so it is warm by the time the sidebar asks for status.
    void backend.ensureRunning();
    // Open the container so the activity bar entry is selected on launch.
    void vscode.commands.executeCommand('workbench.view.extension.localpilot-sidebar');
}
function deactivate() {
    return backend?.stop();
}
function apiUrl() {
    const url = vscode.workspace.getConfiguration('tokenguard').get('apiUrl') || 'http://localhost:8000';
    return url.replace(/\/+$/, '');
}
async function api(method, path, body, timeoutMs) {
    if (backend?.state === 'starting') {
        await backend.ensureRunning();
    }
    let resp;
    try {
        resp = await fetch(`${apiUrl()}${path}`, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
            signal: AbortSignal.timeout(timeoutMs)
        });
    }
    catch (err) {
        const reason = err instanceof Error && err.name === 'TimeoutError' ? 'timed out' : 'is not reachable';
        const hint = backend?.lastError
            ? `Auto-start failed: ${backend.lastError}`
            : 'Run "TokenGuard: Restart Backend" or start it with `make backend` in the repo root.';
        throw new Error(`LocalPilot backend at ${apiUrl()} ${reason}. ${hint}`);
    }
    if (!resp.ok) {
        let detail = await resp.text();
        try {
            detail = JSON.parse(detail).detail ?? detail;
        }
        catch {
            // plain-text error body
        }
        throw new Error(`Backend error ${resp.status}: ${detail}`);
    }
    return (await resp.json());
}
/** The prompt's context: current editor selection (if any) + the open workspace folder as the repo. */
function buildRequest(prompt) {
    const editor = vscode.window.activeTextEditor;
    let code = '';
    const info = [];
    if (editor && !editor.selection.isEmpty) {
        code = editor.document.getText(editor.selection);
        const lines = editor.selection.end.line - editor.selection.start.line + 1;
        info.push(`${lines} selected line(s) from ${vscode.workspace.asRelativePath(editor.document.uri)}`);
    }
    // Search the workspace folder that holds the active file (multi-root safe); a path typed
    // in the prompt overrides this on the backend.
    const folder = (editor && vscode.workspace.getWorkspaceFolder(editor.document.uri))
        || vscode.workspace.workspaceFolders?.[0];
    const repoPath = folder && folder.uri.scheme === 'file' ? folder.uri.fsPath : null;
    if (folder && repoPath) {
        info.push(`workspace "${folder.name}"`);
    }
    return {
        req: { prompt, code, repo_path: repoPath },
        contextInfo: info.length ? info.join(' + ') : 'no code context (open a folder, select code, or paste a path in the prompt)'
    };
}
class LocalPilotViewProvider {
    _view;
    resolveWebviewView(webviewView, _context, _token) {
        this._view = webviewView;
        webviewView.webview.options = { enableScripts: true };
        webviewView.webview.html = this._getHtmlForWebview();
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'ready':
                    await this._postStatus();
                    break;
                case 'refreshStatus':
                    await backend?.ensureRunning();
                    await this._postStatus();
                    break;
                case 'sendPrompt':
                    await this._analyze(data.id, data.prompt);
                    break;
                case 'run':
                    await this._run(data.id, data.req, data.route);
                    break;
            }
        });
    }
    _post(message) {
        void this._view?.webview.postMessage(message);
    }
    refreshStatus() {
        void this._postStatus();
    }
    async _postStatus() {
        if (backend?.state === 'starting') {
            this._post({ type: 'status', online: false, starting: true, apiUrl: apiUrl() });
            return; // the state change to running/failed triggers another refresh
        }
        try {
            const [health, stats] = await Promise.all([
                api('GET', '/health', undefined, 5_000),
                api('GET', '/stats', undefined, 5_000)
            ]);
            this._post({ type: 'status', online: true, health, stats, apiUrl: apiUrl() });
        }
        catch (err) {
            this._post({ type: 'status', online: false, error: err.message, apiUrl: apiUrl() });
        }
    }
    /** Step 1: ask the backend where this prompt should go, without calling any model. */
    async _analyze(id, prompt) {
        const { req, contextInfo } = buildRequest(prompt);
        try {
            const analysis = await api('POST', '/analyze', req, ANALYZE_TIMEOUT_MS);
            this._post({ type: 'analysis', id, req, analysis, contextInfo });
        }
        catch (err) {
            this._post({ type: 'error', id, message: err.message });
        }
    }
    /** Step 2: run the pipeline on the route the user (or the analysis) picked. */
    async _run(id, req, route) {
        try {
            const result = await api('POST', '/chat', { ...req, force_route: route }, CHAT_TIMEOUT_MS);
            this._post({ type: 'result', id, route, result });
        }
        catch (err) {
            this._post({ type: 'error', id, message: err.message });
        }
        await this._postStatus();
    }
    _getHtmlForWebview() {
        const nonce = getNonce();
        return `<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
            <style>
                body {
                    font-family: var(--vscode-font-family);
                    padding: 10px;
                    color: var(--vscode-foreground);
                    background-color: var(--vscode-sideBar-background);
                }
                .container { display: flex; flex-direction: column; gap: 10px; }
                textarea {
                    width: 100%;
                    height: 70px;
                    background: var(--vscode-input-background);
                    color: var(--vscode-input-foreground);
                    border: 1px solid var(--vscode-input-border);
                    border-radius: 4px;
                    padding: 8px;
                    box-sizing: border-box;
                    resize: vertical;
                    font-family: inherit;
                }
                button {
                    width: 100%;
                    padding: 8px;
                    background: var(--vscode-button-background);
                    color: var(--vscode-button-foreground);
                    border: none;
                    border-radius: 4px;
                    font-weight: bold;
                    cursor: pointer;
                }
                button:hover { background: var(--vscode-button-hoverBackground); }
                button:disabled { opacity: 0.5; cursor: default; }
                button.secondary {
                    background: var(--vscode-button-secondaryBackground);
                    color: var(--vscode-button-secondaryForeground);
                }
                button.secondary:hover { background: var(--vscode-button-secondaryHoverBackground); }
                .choices { display: flex; gap: 6px; margin-top: 8px; }
                .status {
                    font-size: 11px;
                    padding: 6px 8px;
                    border-radius: 4px;
                    background: var(--vscode-editor-inactiveSelectionBackground);
                }
                .status .offline { color: var(--vscode-errorForeground); }
                .stats { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 4px; opacity: 0.85; }
                .chat-history { margin-top: 10px; display: flex; flex-direction: column; gap: 10px; }
                .msg {
                    padding: 10px;
                    background: var(--vscode-editor-inactiveSelectionBackground);
                    border-radius: 6px;
                    font-size: 12px;
                }
                .msg.user { white-space: pre-wrap; }
                .badge {
                    display: inline-block;
                    padding: 3px 8px;
                    border-radius: 12px;
                    font-weight: bold;
                    font-size: 10px;
                    margin-bottom: 6px;
                    text-transform: uppercase;
                }
                .local { background: #1b4332; color: #52b788; border: 1px solid #52b788; }
                .cloud { background: #0d3b66; color: #4ea8de; border: 1px solid #4ea8de; }
                .local_first { background: #4a3b00; color: #e9c46a; border: 1px solid #e9c46a; }
                .metrics {
                    font-size: 11px;
                    opacity: 0.85;
                    margin-bottom: 8px;
                    padding-bottom: 4px;
                    border-bottom: 1px dashed var(--vscode-widget-border);
                }
                .warn { color: var(--vscode-editorWarning-foreground); margin: 6px 0; }
                .error { color: var(--vscode-errorForeground); white-space: pre-wrap; }
                .muted { opacity: 0.75; font-size: 11px; }
                ul { margin: 4px 0; padding-left: 16px; }
                details { margin-top: 6px; font-size: 11px; }
                summary { cursor: pointer; opacity: 0.85; }
                .answer { margin: 0; line-height: 1.45; }
                .answer pre {
                    background: var(--vscode-textCodeBlock-background);
                    padding: 8px;
                    border-radius: 4px;
                    overflow-x: auto;
                    white-space: pre;
                }
                .answer code { font-family: var(--vscode-editor-font-family); }
                .answer p { margin: 0 0 6px 0; white-space: pre-wrap; }
                .spinner { opacity: 0.8; font-style: italic; }
            </style>
        </head>
        <body>
            <div class="container">
                <div id="status" class="status">Connecting to LocalPilot backend…</div>
                <textarea id="prompt" placeholder="Ask LocalPilot about your repository… (Enter to send, Shift+Enter for newline)"></textarea>
                <button id="send">Send Request</button>
                <div id="history" class="chat-history"></div>
            </div>

            <script nonce="${nonce}">
                const vscode = acquireVsCodeApi();
                const history = document.getElementById('history');
                const input = document.getElementById('prompt');
                const sendBtn = document.getElementById('send');

                let nextId = 1;
                let busy = false;               // one request at a time, like the Streamlit UI
                const turns = new Map();        // id -> { req, analysis, el, pendingEl }

                function esc(s) {
                    return String(s ?? '').replace(/[&<>"']/g, c => ({
                        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
                    })[c]);
                }

                // Minimal markdown: fenced code blocks, inline code, bold. Everything is escaped first.
                function renderMarkdown(text) {
                    const parts = String(text ?? '').split(/\`\`\`[\\w+-]*\\n?/);
                    return parts.map((part, i) => {
                        if (i % 2 === 1) return '<pre><code>' + esc(part.replace(/\\n$/, '')) + '</code></pre>';
                        const html = esc(part)
                            .replace(/\`([^\`\\n]+)\`/g, '<code>$1</code>')
                            .replace(/\\*\\*([^*\\n]+)\\*\\*/g, '<b>$1</b>');
                        return html.trim() ? '<p>' + html.trim() + '</p>' : '';
                    }).join('');
                }

                function fmt(n) { return Number(n ?? 0).toLocaleString(); }

                function setBusy(value) {
                    busy = value;
                    sendBtn.disabled = value;
                    document.querySelectorAll('button[data-route]').forEach(b => { b.disabled = value || b.dataset.used === '1'; });
                }

                function addCard(html, cls = 'msg') {
                    const div = document.createElement('div');
                    div.className = cls;
                    div.innerHTML = html;
                    history.appendChild(div);
                    div.scrollIntoView({ behavior: 'smooth', block: 'end' });
                    return div;
                }

                function send() {
                    const prompt = input.value.trim();
                    if (!prompt || busy) return;
                    const id = nextId++;
                    addCard('<b>You:</b> ' + esc(prompt), 'msg user');
                    const el = addCard('<span class="spinner">Analyzing prompt (local vs global)…</span>');
                    turns.set(id, { el });
                    setBusy(true);
                    vscode.postMessage({ type: 'sendPrompt', id, prompt });
                    input.value = '';
                }

                function run(id, route) {
                    const turn = turns.get(id);
                    if (!turn || busy) return;
                    const label = route === 'local' ? 'local LLM' : 'global (cloud) LLM';
                    turn.pendingEl = addCard('<span class="spinner">Asking the ' + label + '…</span>');
                    setBusy(true);
                    vscode.postMessage({ type: 'run', id, req: turn.req, route });
                }

                function decisionLabel(d) {
                    return d === 'cloud' ? '🔵 Complex → global LLM recommended'
                         : d === 'local_first' ? '🟡 Medium → local first'
                         : '🟢 Simple → local LLM';
                }

                function renderAnalysis(id, a, contextInfo) {
                    const turn = turns.get(id);
                    const reasons = (a.reasons || []).map(r => '<li>' + esc(r) + '</li>').join('');
                    const files = (a.files_selected || []).map(f => '<code>' + esc(f) + '</code>').join(', ');
                    const isCloud = a.decision === 'cloud';
                    turn.el.innerHTML =
                        '<span class="badge ' + esc(a.decision) + '">' + decisionLabel(a.decision) + '</span>' +
                        '<div class="metrics"><b>Complexity:</b> ' + esc(a.complexity_score) +
                        ' · <b>Task:</b> ' + esc(a.task_type) +
                        ' · <b>Optimizer:</b> ' + esc(a.context_optimizer) + '</div>' +
                        '<div class="muted">Repo searched: ' + (a.repo_path ? '<code>' + esc(a.repo_path) + '</code>' : 'none') +
                        ' · from ' + esc((a.context_sources || []).join('; ') || contextInfo) + '</div>' +
                        '<ul>' + reasons + '</ul>' +
                        '<div>Global would send ~<b>' + fmt(a.estimated_cloud_tokens) + '</b> tokens instead of ' +
                        fmt(a.original_tokens) + ' for the full context' +
                        (a.secrets_redacted ? ', with <b>' + a.secrets_redacted + '</b> secret(s) masked' : '') +
                        '. Local sends nothing off your machine.</div>' +
                        (files ? '<details open><summary>Files selected by context compression (' + a.files_selected.length + ')</summary>' + files + '</details>' : '') +
                        '<div class="choices">' +
                            '<button data-route="local" data-id="' + id + '" class="' + (isCloud ? 'secondary' : '') + '">🖥️ Use local LLM</button>' +
                            '<button data-route="cloud" data-id="' + id + '" class="' + (isCloud ? '' : 'secondary') + '">☁️ Use global LLM' + (isCloud ? ' (recommended)' : '') + '</button>' +
                        '</div>';
                }

                function renderResult(id, r) {
                    const turn = turns.get(id);
                    const isLocal = r.route === 'local';
                    let metrics;
                    if (isLocal) {
                        metrics = '<b>Model:</b> ' + esc(r.model) + ' · <b>Confidence:</b> ' +
                            (r.confidence == null ? 'n/a' : Math.round(r.confidence * 100) + '%') +
                            ' · ' + fmt(r.latency_ms) + ' ms · <b>0</b> tokens sent to cloud';
                    } else {
                        const pct = r.original_tokens ? Math.round(100 * r.tokens_saved / r.original_tokens) : 0;
                        metrics = '<b>Model:</b> ' + esc(r.model) + ' · ' + fmt(r.latency_ms) + ' ms<br>' +
                            '<b>Token savings:</b> sent ' + fmt(r.sent_tokens) + ' of ' + fmt(r.original_tokens) +
                            ' (saved ' + fmt(r.tokens_saved) + ', ' + pct + '%) · ' + r.secrets_redacted + ' secret(s) masked';
                    }
                    const lowConf = isLocal && r.confidence != null && r.confidence < turn.analysis.min_local_confidence;
                    const files = (r.files_selected || []).map(f => '<code>' + esc(f) + '</code>').join(', ');
                    const trace = (r.trace || []).map(t => '<li>' + esc(t) + '</li>').join('');
                    turn.pendingEl.innerHTML =
                        '<span class="badge ' + (isLocal ? 'local' : 'cloud') + '">' +
                            (isLocal ? '🟢 Local inference' : '🔵 Global (cloud) LLM') + '</span>' +
                        '<div class="metrics">' + metrics + '</div>' +
                        (r.escalated ? '<div class="warn">Escalated: ' + esc(r.escalation_reason) + '</div>' : '') +
                        (r.cloud_fallback_reason ? '<div class="warn">⚠️ Global LLM unavailable, showing the mock answer. ' + esc(r.cloud_fallback_reason) + '</div>' : '') +
                        (lowConf ? '<div class="warn">The local model wasn\\'t confident about this answer.</div>' : '') +
                        '<div class="answer">' + renderMarkdown(r.answer) + '</div>' +
                        (!isLocal ? '<details' + (files ? ' open' : '') + '><summary>Context sent after compression (' + (r.files_selected || []).length + ' files)</summary>' +
                            (r.repo_path ? '<div class="muted">from <code>' + esc(r.repo_path) + '</code></div>' : '') + (files || 'no repo files') + '</details>' : '') +
                        (trace ? '<details><summary>Pipeline trace</summary><ul>' + trace + '</ul></details>' : '') +
                        (isLocal ? '<div class="choices"><button data-route="cloud" data-id="' + id + '" class="' + (lowConf ? '' : 'secondary') + '">' +
                            (lowConf ? '☁️ Retry with global LLM' : 'Not satisfied? ☁️ Use global LLM') + '</button></div>' : '');
                    turn.pendingEl = null;
                }

                function renderStatus(m) {
                    const el = document.getElementById('status');
                    if (m.starting) {
                        el.innerHTML = '<span class="spinner">Starting LocalPilot backend…</span> (' + esc(m.apiUrl) + ')';
                        return;
                    }
                    if (!m.online) {
                        el.innerHTML = '<span class="offline">● Backend offline</span> (' + esc(m.apiUrl) + ')<div class="muted">' +
                            esc(m.error) + '</div><div class="choices"><button id="retryStatus" class="secondary">Retry connection</button></div>';
                        return;
                    }
                    const h = m.health, s = m.stats;
                    el.innerHTML = '● API online · cloud: <code>' + esc(h.cloud_provider) + '</code> · local: <code>' +
                        esc(h.local_model) + '</code> · Ollama ' + (h.ollama ? '✅' : '❌') +
                        '<div class="stats"><span>🖥️ Local: <b>' + s.local_requests + '</b></span><span>☁️ Cloud: <b>' +
                        s.cloud_requests + '</b></span><span>Tokens saved: <b>' + fmt(s.total_tokens_saved) + '</b> (' +
                        s.cloud_token_reduction_pct + '%)</span></div>';
                }

                sendBtn.addEventListener('click', send);
                input.addEventListener('keydown', e => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
                });
                document.addEventListener('click', e => {
                    const target = e.target.closest('button');
                    if (!target) return;
                    if (target.id === 'retryStatus') { vscode.postMessage({ type: 'refreshStatus' }); return; }
                    if (target.dataset.route && !busy) {
                        target.dataset.used = '1';
                        run(Number(target.dataset.id), target.dataset.route);
                    }
                });

                window.addEventListener('message', event => {
                    const m = event.data;
                    const turn = turns.get(m.id);
                    switch (m.type) {
                        case 'status':
                            renderStatus(m);
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
                        case 'result':
                            renderResult(m.id, m.result);
                            setBusy(false);
                            break;
                        case 'error': {
                            const el = turn ? (turn.pendingEl || turn.el) : addCard('');
                            el.innerHTML = '<div class="error">⚠️ ' + esc(m.message) + '</div>';
                            if (turn) turn.pendingEl = null;
                            setBusy(false);
                            break;
                        }
                    }
                });

                vscode.postMessage({ type: 'ready' });
            </script>
        </body>
        </html>`;
    }
}
function getNonce() {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let nonce = '';
    for (let i = 0; i < 32; i++) {
        nonce += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return nonce;
}
//# sourceMappingURL=extension.js.map