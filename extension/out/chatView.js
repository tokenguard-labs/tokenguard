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
exports.LocalPilotViewProvider = void 0;
const fs = __importStar(require("fs"));
const vscode = __importStar(require("vscode"));
const ANALYZE_TIMEOUT_MS = 60_000;
// Idle, not total: the backend streams tokens and sends a heartbeat every 15s while a model loads.
const STREAM_IDLE_TIMEOUT_MS = 60_000;
// Code-fence names (as highlight.js reports them) that differ from VS Code language ids.
const LANGUAGE_ALIASES = {
    js: 'javascript', jsx: 'javascriptreact', ts: 'typescript', tsx: 'typescriptreact',
    py: 'python', sh: 'shellscript', bash: 'shellscript', shell: 'shellscript', zsh: 'shellscript',
    yml: 'yaml', 'c++': 'cpp', cs: 'csharp', 'c#': 'csharp', rs: 'rust', rb: 'ruby',
    md: 'markdown', golang: 'go', kt: 'kotlin', xml: 'html'
};
class LocalPilotViewProvider {
    _extensionUri;
    _host;
    _view;
    _retryTimer;
    _retryDelayMs = 1_000;
    _preloadedFor; // model we've already asked the backend to load
    _preloadSuppressed = false; // user explicitly unloaded: don't auto-reload until they pick again
    _inFlight = new Map(); // turn id -> request (for Stop)
    // The sidebar can take focus from the editor, so remember the last one for "Insert at cursor".
    _lastEditor = vscode.window.activeTextEditor;
    _contextTimer;
    _disposables = [];
    constructor(_extensionUri, _host) {
        this._extensionUri = _extensionUri;
        this._host = _host;
        this._disposables.push(vscode.window.onDidChangeActiveTextEditor(editor => {
            if (editor) {
                this._lastEditor = editor;
            }
            this._postContextSoon();
        }), vscode.window.onDidChangeTextEditorSelection(() => this._postContextSoon()), vscode.workspace.onDidChangeWorkspaceFolders(() => this._postContextSoon()));
    }
    dispose() {
        this._cancelAll();
        this._clearRetry();
        clearTimeout(this._contextTimer);
        this._disposables.forEach(d => d.dispose());
    }
    resolveWebviewView(webviewView, _context, _token) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this._extensionUri, 'media')]
        };
        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);
        webviewView.onDidDispose(() => {
            this._cancelAll();
            this._clearRetry();
            this._view = undefined;
        });
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'ready':
                    this._postContext();
                    await this._postStatus();
                    break;
                case 'refreshStatus':
                    await this._host.backend()?.ensureRunning();
                    await this._postStatus();
                    break;
                case 'sendPrompt':
                    await this._analyze(data.id, data.prompt);
                    break;
                case 'run':
                    await this._run(data.id, data.req, data.route);
                    break;
                case 'cancel':
                    this._inFlight.get(data.id)?.abort();
                    break;
                case 'copy':
                    await vscode.env.clipboard.writeText(String(data.text ?? ''));
                    break;
                case 'insert':
                    await this._insertAtCursor(String(data.code ?? ''), String(data.language ?? ''));
                    break;
                case 'openLink':
                    await this._openLink(String(data.href ?? ''));
                    break;
                case 'setApiKey':
                    await vscode.commands.executeCommand('tokenguard.setApiKey');
                    break;
                case 'selectLocalModel': {
                    const model = (data.model || '');
                    if (model && data.installed === false) {
                        const size = data.size_gb ? ` (~${data.size_gb} GB)` : '';
                        const choice = await vscode.window.showInformationMessage(`"${model}" isn't downloaded yet${size}. Download it now?`, { modal: false }, 'Download');
                        if (choice !== 'Download') {
                            await this._postStatus(); // reset the dropdown to the saved value
                            break;
                        }
                        if (!(await this._host.downloadLocalModel(model))) {
                            await this._postStatus();
                            break;
                        }
                    }
                    await vscode.workspace.getConfiguration('tokenguard')
                        .update('localModel', model, vscode.ConfigurationTarget.Global);
                    this._preloadSuppressed = false;
                    this._preloadedFor = model || undefined;
                    // Unload the old local model and warm up the new one so only one is resident.
                    await this._host.switchLocalModel(model);
                    this._post({ type: 'modelLoading', model: null });
                    await this._postStatus();
                    break;
                }
            }
        });
    }
    _post(message) {
        void this._view?.webview.postMessage(message);
    }
    refreshStatus() {
        void this._postStatus();
    }
    /** "TokenGuard: Clear Chat": stop any request and show the empty state again. */
    clearChat() {
        this._cancelAll();
        this._post({ type: 'clear' });
    }
    /** After an explicit unload, don't auto-reload until the user picks a model again. */
    suppressPreload() {
        this._preloadSuppressed = true;
    }
    async _postStatus() {
        const backend = this._host.backend();
        const apiUrl = this._host.apiUrl();
        const localModel = this._host.localModel();
        const state = backend?.state;
        if (state === 'failed') {
            this._post({
                type: 'status', online: false, retrying: true,
                error: backend?.lastError || 'backend failed to start', apiUrl, localModel
            });
            this._scheduleRetry(5_000); // keep retrying, but don't spin the "starting" spinner
            return;
        }
        // Backend still booting (or not started yet): show the spinner and keep polling instead
        // of flashing "offline" — the first /health can fail before uvicorn finishes importing.
        if (state === 'starting' || state === 'stopped' || state === undefined) {
            this._post({ type: 'status', online: false, starting: true, apiUrl, localModel });
            void backend?.ensureRunning();
            this._scheduleRetry();
            return;
        }
        try {
            const [health, stats] = await Promise.all([
                this._host.api('GET', '/health', undefined, 5_000),
                this._host.api('GET', '/stats', undefined, 5_000)
            ]);
            this._clearRetry();
            this._post({ type: 'status', online: true, health, stats, apiUrl, localModel });
            void this._maybePreload(health);
        }
        catch (err) {
            this._post({
                type: 'status', online: false, retrying: true,
                error: err.message, apiUrl, localModel
            });
            void backend?.ensureRunning(); // nudge a (re)start; harmless if it's already healthy
            this._scheduleRetry();
        }
    }
    /** Poll again shortly (with a small backoff) until the backend answers. */
    _scheduleRetry(fixedDelayMs) {
        if (this._retryTimer || !this._view) {
            return; // no view (sidebar closed) or a retry is already queued
        }
        const delay = fixedDelayMs ?? this._retryDelayMs;
        this._retryTimer = setTimeout(() => {
            this._retryTimer = undefined;
            void this._postStatus();
        }, delay);
        if (fixedDelayMs === undefined) {
            this._retryDelayMs = Math.min(this._retryDelayMs + 500, 4_000);
        }
    }
    _clearRetry() {
        if (this._retryTimer) {
            clearTimeout(this._retryTimer);
            this._retryTimer = undefined;
        }
        this._retryDelayMs = 1_000;
    }
    /** Load the selected local model once the backend + Ollama are up, so it isn't shown idle. */
    async _maybePreload(health) {
        if (this._preloadSuppressed || !vscode.workspace.getConfiguration('tokenguard').get('preloadLocalModel', true)) {
            return;
        }
        if (!health?.ollama) {
            return; // wait until Ollama is reachable, then a later refresh will load it
        }
        const selected = health.local_model;
        const installed = health.local_models || [];
        if (!selected || !installed.includes(selected)) {
            return; // nothing selected, or the model isn't pulled
        }
        if (health.local_loaded === selected || this._preloadedFor === selected) {
            return;
        }
        this._preloadedFor = selected;
        this._post({ type: 'modelLoading', model: selected }); // "loading…" while Ollama loads it
        await this._host.switchLocalModel(selected);
        this._post({ type: 'modelLoading', model: null }); // clear the spinner either way
        void this._postStatus(); // reflect "loaded in memory" in the sidebar
    }
    /** Track a turn's request so the Stop button can abort it. */
    _track(id) {
        this._inFlight.get(id)?.abort();
        const controller = new AbortController();
        this._inFlight.set(id, controller);
        return controller;
    }
    _untrack(id, controller) {
        if (this._inFlight.get(id) === controller) {
            this._inFlight.delete(id);
        }
    }
    _cancelAll() {
        this._inFlight.forEach(c => c.abort());
        this._inFlight.clear();
    }
    /** Step 1: ask the backend where this prompt should go, without calling any model. */
    async _analyze(id, prompt) {
        const { req, contextInfo } = buildRequest(prompt, this._editor());
        const controller = this._track(id);
        try {
            const analysis = await this._host.api('POST', '/analyze', req, ANALYZE_TIMEOUT_MS, controller.signal);
            this._post({ type: 'analysis', id, req, analysis, contextInfo });
        }
        catch (err) {
            this._post(controller.signal.aborted
                ? { type: 'cancelled', id }
                : { type: 'error', id, message: err.message });
        }
        finally {
            this._untrack(id, controller);
        }
    }
    /** Step 2: run the pipeline on the route the user (or the analysis) picked. */
    async _run(id, req, route) {
        // Aborting closes the connection; the backend notices and cancels the model call.
        const controller = this._track(id);
        try {
            // start / delta / reset events render the answer while it's generated; 'result' finalizes it.
            const result = await this._host.apiStream('/chat/stream', { ...req, force_route: route }, (event, data) => this._post({ type: 'stream', id, event, data }), STREAM_IDLE_TIMEOUT_MS, controller.signal);
            this._post({ type: 'result', id, route, result });
        }
        catch (err) {
            this._post(controller.signal.aborted
                ? { type: 'cancelled', id }
                : { type: 'error', id, message: err.message });
        }
        finally {
            this._untrack(id, controller);
        }
        await this._postStatus();
    }
    /** The active text editor, or the last one if the sidebar has focus and it's still open. */
    _editor() {
        const active = vscode.window.activeTextEditor;
        if (active) {
            return active;
        }
        const last = this._lastEditor;
        return last && vscode.window.visibleTextEditors.includes(last) ? last : undefined;
    }
    /** Tell the empty state what context a prompt would carry (debounced: selections change a lot). */
    _postContextSoon() {
        clearTimeout(this._contextTimer);
        this._contextTimer = setTimeout(() => this._postContext(), 250);
    }
    _postContext() {
        this._post({ type: 'context', info: buildRequest('', this._editor()).contextInfo });
    }
    /** "Insert" on a code block: replace the selection in the editor, or open a new untitled file. */
    async _insertAtCursor(code, language) {
        const editor = this._editor();
        if (editor) {
            const shown = await vscode.window.showTextDocument(editor.document, editor.viewColumn);
            const ok = await shown.edit(edit => edit.replace(shown.selection, code));
            if (!ok) {
                void vscode.window.showWarningMessage('TokenGuard: could not insert into this editor (is it read-only?).');
            }
            return;
        }
        const doc = await vscode.workspace.openTextDocument({ content: code, language: await toLanguageId(language) });
        await vscode.window.showTextDocument(doc);
    }
    async _openLink(href) {
        if (/^https?:\/\//i.test(href)) {
            await vscode.env.openExternal(vscode.Uri.parse(href));
        }
    }
    /** media/chat/index.html with its {{placeholders}} filled in for this webview. */
    _getHtmlForWebview(webview) {
        const media = vscode.Uri.joinPath(this._extensionUri, 'media');
        const template = fs.readFileSync(vscode.Uri.joinPath(media, 'chat', 'index.html').fsPath, 'utf8');
        const values = {
            cspSource: webview.cspSource,
            nonce: getNonce(),
            mediaUri: webview.asWebviewUri(media).toString()
        };
        return template.replace(/\{\{(\w+)\}\}/g, (match, key) => values[key] ?? match);
    }
}
exports.LocalPilotViewProvider = LocalPilotViewProvider;
/** The prompt's context: current editor selection (if any) + the open workspace folder as the repo. */
function buildRequest(prompt, editor) {
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
async function toLanguageId(lang) {
    const id = LANGUAGE_ALIASES[lang] ?? lang;
    return id && (await vscode.languages.getLanguages()).includes(id) ? id : 'plaintext';
}
function getNonce() {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let nonce = '';
    for (let i = 0; i < 32; i++) {
        nonce += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return nonce;
}
//# sourceMappingURL=chatView.js.map