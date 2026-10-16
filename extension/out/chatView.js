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
exports.currentText = currentText;
exports.applyChange = applyChange;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
const inlineDiff_1 = require("./inlineDiff");
// Read-only documents for the agent's diffs: tokenguard-agent:/<path>?turn=<id>&run=<run id>&side=original|proposed
const AGENT_DIFF_SCHEME = 'tokenguard-agent';
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
    _cloudSig = ''; // provider|baseUrl signature last fetched for /cloud/models
    _inFlight = new Map(); // turn id -> request (for Stop)
    // turn id -> the agent's proposed changes, kept here (not in the page) until accepted/rejected
    _agentRuns = new Map();
    _inline = new inlineDiff_1.InlineDiffManager();
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
        }), vscode.window.onDidChangeTextEditorSelection(() => this._postContextSoon()), vscode.workspace.onDidChangeWorkspaceFolders(() => this._postContextSoon()), this._inline, vscode.workspace.registerTextDocumentContentProvider(AGENT_DIFF_SCHEME, {
            provideTextDocumentContent: uri => this._agentDiffContent(uri)
        }));
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
        // Lazy activation: nothing spawns when VS Code opens. The backend and Ollama start
        // here instead — the first time the sidebar is actually shown — and stop on deactivate.
        void this._host.backend()?.ensureRunning();
        void this._host.ollama?.()?.ensureRunning();
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'ready':
                    this._postContext();
                    await this._postStatus();
                    void this._postCloudModels();
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
                case 'agentRun':
                    await this._runAgent(data.id, String(data.prompt ?? ''), data.route === 'local' ? 'local' : 'cloud', Array.isArray(data.history) ? data.history : []);
                    break;
                case 'agentApprove':
                    await this._agentApprove(data);
                    break;
                case 'agentDiff':
                    await this._agentDiff(data.id, String(data.path ?? ''));
                    break;
                case 'agentAccept':
                    await this._agentAccept(data.id, (data.paths ?? []));
                    break;
                case 'agentReject':
                    await this._agentReject(data.id, (data.paths ?? []));
                    break;
                case 'agentReview':
                    await this._agentReview(data.id, String(data.path ?? ''), true);
                    break;
                case 'agentOpen':
                    await this._agentOpen(data.id, String(data.path ?? ''));
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
                    // RAM check: warn before loading a model bigger than this machine holds comfortably.
                    if (model && !(await this._warnOnFit(model))) {
                        await this._postStatus(); // declined: reset the dropdown to the saved value
                        break;
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
                case 'selectCloudProvider': {
                    const config = vscode.workspace.getConfiguration('tokenguard');
                    await config.update('cloudProvider', (data.provider || 'deepseek'), vscode.ConfigurationTarget.Global);
                    // A model belongs to one provider; clear it so the backend uses this provider's default.
                    await config.update('cloudModel', '', vscode.ConfigurationTarget.Global);
                    await this._refreshCloud();
                    break;
                }
                case 'selectCloudModel':
                    await vscode.workspace.getConfiguration('tokenguard')
                        .update('cloudModel', (data.model || ''), vscode.ConfigurationTarget.Global);
                    await this._postStatus();
                    void this._postCloudModels(); // refresh the price/current line from the cache
                    break;
                case 'saveCloudKey': {
                    const provider = (data.provider || this._host.cloudProvider());
                    const key = String(data.key || '').trim();
                    await this._host.storeApiKey(provider, key);
                    if (key) {
                        void vscode.window.showInformationMessage(`TokenGuard: ${provider} API key saved to SecretStorage.`);
                    }
                    await this._refreshCloud();
                    break;
                }
                case 'clearCloudKey': {
                    const provider = (data.provider || this._host.cloudProvider());
                    await this._host.storeApiKey(provider, '');
                    void vscode.window.showInformationMessage(`TokenGuard: cleared the ${provider} API key.`);
                    await this._refreshCloud();
                    break;
                }
                case 'saveCloudBaseUrl':
                    await vscode.workspace.getConfiguration('tokenguard')
                        .update('customBaseUrl', String(data.baseUrl || '').trim(), vscode.ConfigurationTarget.Global);
                    await this._refreshCloud();
                    break;
                case 'testCloud':
                    await this._host.api('GET', '/health?force=true', undefined, 15_000).catch(() => undefined);
                    await this._postCloudModels(true);
                    await this._postStatus();
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
    /** "TokenGuard: Clear Chat": stop any request and show the empty state again. */
    clearChat() {
        this._cancelAll();
        this._agentRuns.clear();
        void this._inline.rejectAll(); // put files under inline review back as they were
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
            this._post({ type: 'status', online: false, starting: true, apiUrl, localModel, progress: backend?.progress });
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
            const sig = `${this._host.cloudProvider()}|${this._host.customBaseUrl()}`;
            if (sig !== this._cloudSig) {
                this._cloudSig = sig;
                void this._postCloudModels();
            }
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
    /** Cloud provider, key or base URL changed: refetch the model list and the status card. */
    async _refreshCloud() {
        this._cloudSig = '';
        await this._postCloudModels(true);
        await this._postStatus();
    }
    /** Fetch the provider's model list (backend-cached) and hand it to the sidebar picker. */
    async _postCloudModels(force = false) {
        if (!force && !vscode.workspace.getConfiguration('tokenguard').get('cloudModelsAutoFetch', true)) {
            return;
        }
        try {
            const info = await this._host.api('GET', `/cloud/models${force ? '?force=true' : ''}`, undefined, 10_000);
            this._post({ type: 'cloudModels', info });
        }
        catch {
            // ignore: the status card already reports cloud problems
        }
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
    /** RAM check for a local model: resolve true when loading may proceed (after any warning). */
    async _warnOnFit(model) {
        let info;
        try {
            info = await this._host.api('GET', '/models', undefined, 10_000);
        }
        catch {
            return true; // backend unreachable: don't block the switch
        }
        const f = info?.fit?.[model];
        if (!f || f.comfortable !== false) {
            return true;
        }
        const host = `this machine (${info.system?.total_ram_gb ?? '?'} GB RAM` +
            (info.system?.swap_total_gb ? ` + ${info.system.swap_total_gb} GB swap` : '') +
            (info.system?.has_gpu ? ` + ${info.system.gpu_vram_gb} GB VRAM` : '') + ')';
        if (f.fits === false) {
            const choice = await vscode.window.showWarningMessage(`"${model}" wants ~${f.min_ram_gb} GB RAM/VRAM — more than ${host}. Ollama will likely swap heavily or fail.`, { modal: true }, 'Load anyway');
            return choice === 'Load anyway';
        }
        await vscode.window.showWarningMessage(`"${model}" (~${f.size_gb} GB) doesn't fit in ${host} comfortably — it will use swap, so answers get slower.`);
        return true;
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
            const result = await this._host.apiStream('/chat/stream', { ...req, force_route: route }, (event, data) => {
                if (!controller.signal.aborted) { // Stop pressed: forward nothing more
                    this._post({ type: 'stream', id, event, data });
                }
            }, STREAM_IDLE_TIMEOUT_MS, controller.signal);
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
    // ---- Agent mode ------------------------------------------------------------------------
    /** Run an agent task. Its edits come back staged; nothing is written until the user accepts. */
    async _runAgent(id, prompt, route, history) {
        const editor = this._editor();
        const { req } = buildRequest(prompt, editor);
        const activeFile = editor && req.repo_path && editor.document.uri.scheme === 'file'
            ? path.relative(req.repo_path, editor.document.uri.fsPath)
            : null;
        const controller = this._track(id);
        try {
            const result = await this._host.apiStream('/agent/stream', { ...req, route, history, active_file: activeFile && !activeFile.startsWith('..') ? activeFile : null }, (event, data) => {
                if (!controller.signal.aborted) {
                    this._post({ type: 'agentEvent', id, event, data });
                }
            }, STREAM_IDLE_TIMEOUT_MS, controller.signal);
            const changes = new Map();
            for (const change of result.changes) {
                changes.set(change.path, { ...change, state: 'pending' });
            }
            this._agentRuns.set(id, { runId: result.run_id, root: result.repo_path, changes });
            // The page only needs the stats; full contents stay here for the diff view and Accept.
            const summary = result.changes.map(({ original: _o, proposed: _p, ...rest }) => rest);
            this._post({ type: 'agentResult', id, result: { ...result, changes: summary } });
            // Show modified and new files inline, Cursor-style (new files are created so they appear
            // in the tree; rejecting removes them again). Deleted files wait for "Review in file",
            // so nothing is removed on disk before the user decides.
            if (vscode.workspace.getConfiguration('tokenguard').get('agentInlineReview', true)) {
                for (const change of result.changes.filter(c => c.status === 'modified' || c.status === 'added')) {
                    await this._agentReview(id, change.path, false);
                }
            }
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
    async _agentApprove(data) {
        try {
            await this._host.api('POST', '/agent/approve', {
                run_id: data.runId, request_id: data.requestId, approved: !!data.approved, always: !!data.always
            }, 10_000);
        }
        catch (err) {
            void vscode.window.showWarningMessage(`TokenGuard: could not answer the agent: ${err.message}`);
        }
    }
    _agentDiffContent(uri) {
        const params = new URLSearchParams(uri.query);
        const change = this._agentRuns.get(Number(params.get('turn')))?.changes.get(uri.path.replace(/^\//, ''));
        if (!change) {
            return '';
        }
        return (params.get('side') === 'original' ? change.original : change.proposed) ?? '';
    }
    /** Open the agent's proposal for one file in VS Code's diff editor. */
    async _agentDiff(id, file) {
        const run = this._agentRuns.get(id);
        const change = run?.changes.get(file);
        if (!run || !change) {
            return;
        }
        // The run id keeps URIs unique: VS Code caches a virtual document by URI, and turn ids
        // restart when the sidebar reloads.
        const uri = (side) => vscode.Uri.from({
            scheme: AGENT_DIFF_SCHEME, path: '/' + file, query: `turn=${id}&run=${run.runId}&side=${side}`
        });
        const label = { added: 'new file', deleted: 'deleted', modified: 'proposed' }[change.status];
        await vscode.commands.executeCommand('vscode.diff', uri('original'), uri('proposed'), `${path.basename(file)} (agent: ${label})`, { preview: true });
    }
    /** Show a proposed change inline in its file. `ask`: confirm a conflict instead of skipping it. */
    async _agentReview(id, file, ask) {
        const run = this._agentRuns.get(id);
        const change = run?.changes.get(file);
        if (!run || !change || change.state !== 'pending') {
            return;
        }
        const uri = vscode.Uri.file(path.join(run.root, file));
        if (change.inline && this._inline.isReviewing(uri)) {
            await vscode.window.showTextDocument(uri, { preview: false });
            return;
        }
        // With auto save on, the buffer (old and new lines together) would be written to disk mid-review.
        // A new file has no old lines, so this only applies when it already existed.
        if (change.original !== null
            && vscode.workspace.getConfiguration('files', uri).get('autoSave', 'off') !== 'off') {
            if (!ask) {
                return;
            }
            const choice = await vscode.window.showWarningMessage('Auto Save is on, so the file would be saved with both the old (red) and new (green) lines until every '
                + 'change is decided. Review in the file anyway? (Diff shows the change without touching the file.)', { modal: true }, 'Review in file');
            if (choice !== 'Review in file') {
                return;
            }
        }
        const current = await currentText(uri);
        if (current !== (change.original ?? undefined)) {
            if (!ask) {
                return; // changed since the agent read it: leave it to the sidebar buttons
            }
            const choice = await vscode.window.showWarningMessage(`${file} has changed since the agent read it. Review the agent's version against the current file anyway?`, { modal: true }, 'Review');
            if (choice !== 'Review') {
                return;
            }
        }
        try {
            await this._inline.start(uri, current ?? null, change.proposed, outcome => this._onInlineDone(id, file, outcome));
            change.inline = true;
            this._post({ type: 'agentReviewing', id, path: file });
        }
        catch (err) {
            void vscode.window.showWarningMessage(`TokenGuard: ${err.message}`);
        }
    }
    /** An inline review finished (every block decided, or the editor was closed). */
    _onInlineDone(id, file, outcome) {
        const change = this._agentRuns.get(id)?.changes.get(file);
        if (change) {
            change.inline = false;
            change.state = outcome === 'rejected' ? 'rejected' : outcome === 'cancelled' ? 'pending' : 'accepted';
        }
        this._post({ type: 'agentApplied', id, results: [{ path: file, state: outcome }] });
    }
    async _agentOpen(id, file) {
        const run = this._agentRuns.get(id);
        if (!run) {
            return;
        }
        const change = run.changes.get(file);
        if (change?.inline) {
            await vscode.window.showTextDocument(vscode.Uri.file(path.join(run.root, file)), { preview: false });
            return;
        }
        if (change && change.state === 'pending') {
            await this._agentDiff(id, file); // not applied yet: the proposal is what there is to see
            return;
        }
        const uri = vscode.Uri.file(path.join(run.root, file));
        if (fs.existsSync(uri.fsPath)) {
            await vscode.window.showTextDocument(uri);
        }
    }
    /** Write the accepted proposals into the workspace (as undoable editor edits, then saved). */
    async _agentAccept(id, files) {
        const run = this._agentRuns.get(id);
        if (!run) {
            return;
        }
        const results = [];
        for (const file of files) {
            const change = run.changes.get(file);
            if (!change || change.state !== 'pending') {
                continue;
            }
            const uri = vscode.Uri.file(path.join(run.root, file));
            if (change.inline && this._inline.isReviewing(uri)) {
                await this._inline.resolveAll(uri, true); // reported through _onInlineDone
                continue;
            }
            try {
                const current = await currentText(uri);
                // For a modified/deleted file, a different current text means concurrent edits:
                // ask. A new file has nothing to compare against (cancelled inline reviews leave an
                // empty shell), and accepting it means writing the proposal, so no prompt.
                if (change.status !== 'added' && current !== (change.original ?? undefined)) {
                    const choice = await vscode.window.showWarningMessage(`${file} has changed since the agent read it. Replace it with the agent's version anyway?`, { modal: true }, 'Replace');
                    if (choice !== 'Replace') {
                        results.push({ path: file, state: 'pending', reason: 'changed since the agent read it' });
                        continue;
                    }
                }
                await applyChange(uri, change.proposed, current !== undefined);
                change.state = 'accepted';
                results.push({ path: file, state: 'accepted' });
            }
            catch (err) {
                results.push({ path: file, state: 'pending', reason: err.message });
            }
        }
        this._post({ type: 'agentApplied', id, results });
    }
    async _agentReject(id, files) {
        const run = this._agentRuns.get(id);
        const results = [];
        for (const file of files) {
            const change = run?.changes.get(file);
            if (run && change?.inline) {
                await this._inline.resolveAll(vscode.Uri.file(path.join(run.root, file)), false);
                continue;
            }
            if (change && change.state === 'pending') {
                change.state = 'rejected'; // nothing was written, so there is nothing to undo
                results.push({ path: file, state: 'rejected' });
            }
        }
        this._post({ type: 'agentApplied', id, results });
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
/** A file's text as the user sees it: the open (possibly unsaved) editor, else disk; undefined if missing. */
async function currentText(uri) {
    const open = vscode.workspace.textDocuments.find(d => d.uri.toString() === uri.toString());
    if (open) {
        return open.getText();
    }
    try {
        return new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
    }
    catch {
        return undefined;
    }
}
/** Replace, create or delete a file through a WorkspaceEdit (undoable), then save it. */
async function applyChange(uri, proposed, exists) {
    const edit = new vscode.WorkspaceEdit();
    if (proposed === null) {
        edit.deleteFile(uri, { ignoreIfNotExists: true });
        if (!(await vscode.workspace.applyEdit(edit))) {
            throw new Error('VS Code refused the delete');
        }
        return;
    }
    if (exists) {
        const doc = await vscode.workspace.openTextDocument(uri);
        edit.replace(uri, new vscode.Range(doc.positionAt(0), doc.positionAt(doc.getText().length)), proposed);
    }
    else {
        // New file (e.g. from write_file/move_file): make sure its folder exists first, because
        // WorkspaceEdit.createFile does not create parent directories.
        await vscode.workspace.fs.createDirectory(vscode.Uri.file(path.dirname(uri.fsPath)));
        edit.createFile(uri, { overwrite: true });
        edit.insert(uri, new vscode.Position(0, 0), proposed);
    }
    if (!(await vscode.workspace.applyEdit(edit))) {
        if (exists) {
            throw new Error('VS Code refused the edit (is the file read-only?)');
        }
        // Fall back to writing the new file directly if the edit was refused.
        await vscode.workspace.fs.writeFile(uri, new TextEncoder().encode(proposed));
    }
    const doc = await vscode.workspace.openTextDocument(uri);
    if (doc.isDirty && !(await doc.save())) {
        throw new Error('applied, but could not save');
    }
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