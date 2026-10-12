import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import type { BackendManager } from './backend';
import { InlineDiffManager, ReviewOutcome } from './inlineDiff';

// The LocalPilot sidebar. The page itself lives in media/chat/ (index.html, main.css, main.js);
// this class serves it and relays its messages to the backend:
//   POST /analyze  -> routing decision only, no model called
//   POST /chat/stream -> runs the pipeline (force_route picks the local or global (cloud) LLM),
//                        streaming the answer as Server-Sent Events
//   POST /agent/stream -> agent mode: the model explores, edits and runs commands in a tool loop;
//                         its edits come back as proposed changes the user accepts or rejects here
//   POST /agent/approve -> answers the agent's request to run a command
// The flow mirrors scripts/streamlit_ui.py: analyze first, run simple tasks locally,
// and ask the user before a complex task goes to the global LLM.

type Route = 'local' | 'cloud';

interface ChatRequest {
    prompt: string;
    code: string;
    repo_path: string | null;
    detail?: 'brief' | 'detailed'; // local answer length; 'detailed' = the "More detail" button
}

/** A file the agent proposes to change. original/proposed are null for a missing/deleted file. */
interface AgentChange {
    path: string;
    status: 'added' | 'modified' | 'deleted';
    original: string | null;
    proposed: string | null;
    additions: number;
    deletions: number;
    diff: string;
    state: 'pending' | 'accepted' | 'rejected';
    inline?: boolean; // being reviewed inline in its editor
}

interface AgentRunResult {
    run_id: string;
    repo_path: string;
    changes: AgentChange[];
    [key: string]: unknown;
}

// Read-only documents for the agent's diffs: tokenguard-agent:/<path>?turn=<id>&run=<run id>&side=original|proposed
const AGENT_DIFF_SCHEME = 'tokenguard-agent';

const ANALYZE_TIMEOUT_MS = 60_000;
// Idle, not total: the backend streams tokens and sends a heartbeat every 15s while a model loads.
const STREAM_IDLE_TIMEOUT_MS = 60_000;

// Code-fence names (as highlight.js reports them) that differ from VS Code language ids.
const LANGUAGE_ALIASES: Record<string, string> = {
    js: 'javascript', jsx: 'javascriptreact', ts: 'typescript', tsx: 'typescriptreact',
    py: 'python', sh: 'shellscript', bash: 'shellscript', shell: 'shellscript', zsh: 'shellscript',
    yml: 'yaml', 'c++': 'cpp', cs: 'csharp', 'c#': 'csharp', rs: 'rust', rb: 'ruby',
    md: 'markdown', golang: 'go', kt: 'kotlin', xml: 'html'
};

/** What the view needs from the extension; extension.ts owns the backend and settings. */
export interface ChatViewHost {
    backend(): BackendManager | undefined;
    api<T>(method: 'GET' | 'POST', path: string, body: unknown, timeoutMs: number, signal?: AbortSignal): Promise<T>;
    apiStream<T>(path: string, body: unknown, onEvent: (event: string, data: any) => void, idleTimeoutMs: number, signal?: AbortSignal): Promise<T>;
    apiUrl(): string;
    localModel(): string;
    cloudProvider(): string;
    customBaseUrl(): string;
    /** Save (or, with an empty key, delete) a provider's API key in SecretStorage. */
    storeApiKey(provider: string, key: string): Promise<void>;
    switchLocalModel(model: string): Promise<void>;
    downloadLocalModel(model: string): Promise<boolean>;
}

export class LocalPilotViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
    private _view?: vscode.WebviewView;
    private _retryTimer?: NodeJS.Timeout;
    private _retryDelayMs = 1_000;
    private _preloadedFor?: string;      // model we've already asked the backend to load
    private _preloadSuppressed = false;  // user explicitly unloaded: don't auto-reload until they pick again
    private _cloudSig = '';              // provider|baseUrl signature last fetched for /cloud/models
    private readonly _inFlight = new Map<number, AbortController>(); // turn id -> request (for Stop)
    // turn id -> the agent's proposed changes, kept here (not in the page) until accepted/rejected
    private readonly _agentRuns = new Map<number, { runId: string; root: string; changes: Map<string, AgentChange> }>();
    private readonly _inline = new InlineDiffManager();
    // The sidebar can take focus from the editor, so remember the last one for "Insert at cursor".
    private _lastEditor = vscode.window.activeTextEditor;
    private _contextTimer?: NodeJS.Timeout;
    private readonly _disposables: vscode.Disposable[] = [];

    constructor(private readonly _extensionUri: vscode.Uri, private readonly _host: ChatViewHost) {
        this._disposables.push(
            vscode.window.onDidChangeActiveTextEditor(editor => {
                if (editor) {
                    this._lastEditor = editor;
                }
                this._postContextSoon();
            }),
            vscode.window.onDidChangeTextEditorSelection(() => this._postContextSoon()),
            vscode.workspace.onDidChangeWorkspaceFolders(() => this._postContextSoon()),
            this._inline,
            vscode.workspace.registerTextDocumentContentProvider(AGENT_DIFF_SCHEME, {
                provideTextDocumentContent: uri => this._agentDiffContent(uri)
            })
        );
    }

    public dispose() {
        this._cancelAll();
        this._clearRetry();
        clearTimeout(this._contextTimer);
        this._disposables.forEach(d => d.dispose());
    }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ) {
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
                    await this._runAgent(data.id, String(data.prompt ?? ''), data.route === 'local' ? 'local' : 'cloud',
                        Array.isArray(data.history) ? data.history : []);
                    break;
                case 'agentApprove':
                    await this._agentApprove(data);
                    break;
                case 'agentDiff':
                    await this._agentDiff(data.id, String(data.path ?? ''));
                    break;
                case 'agentAccept':
                    await this._agentAccept(data.id, (data.paths ?? []) as string[]);
                    break;
                case 'agentReject':
                    await this._agentReject(data.id, (data.paths ?? []) as string[]);
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
                    const model = (data.model || '') as string;
                    if (model && data.installed === false) {
                        const size = data.size_gb ? ` (~${data.size_gb} GB)` : '';
                        const choice = await vscode.window.showInformationMessage(
                            `"${model}" isn't downloaded yet${size}. Download it now?`, { modal: false }, 'Download');
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
                case 'selectCloudProvider': {
                    const config = vscode.workspace.getConfiguration('tokenguard');
                    await config.update('cloudProvider', (data.provider || 'deepseek') as string, vscode.ConfigurationTarget.Global);
                    // A model belongs to one provider; clear it so the backend uses this provider's default.
                    await config.update('cloudModel', '', vscode.ConfigurationTarget.Global);
                    await this._refreshCloud();
                    break;
                }
                case 'selectCloudModel':
                    await vscode.workspace.getConfiguration('tokenguard')
                        .update('cloudModel', (data.model || '') as string, vscode.ConfigurationTarget.Global);
                    await this._postStatus();
                    void this._postCloudModels(); // refresh the price/current line from the cache
                    break;
                case 'saveCloudKey': {
                    const provider = (data.provider || this._host.cloudProvider()) as string;
                    const key = String(data.key || '').trim();
                    await this._host.storeApiKey(provider, key);
                    if (key) {
                        void vscode.window.showInformationMessage(`TokenGuard: ${provider} API key saved to SecretStorage.`);
                    }
                    await this._refreshCloud();
                    break;
                }
                case 'clearCloudKey': {
                    const provider = (data.provider || this._host.cloudProvider()) as string;
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

    private _post(message: unknown) {
        void this._view?.webview.postMessage(message);
    }

    public refreshStatus() {
        void this._postStatus();
    }

    /** "TokenGuard: Clear Chat": stop any request and show the empty state again. */
    public clearChat() {
        this._cancelAll();
        this._agentRuns.clear();
        void this._inline.rejectAll(); // put files under inline review back as they were
        this._post({ type: 'clear' });
    }

    /** After an explicit unload, don't auto-reload until the user picks a model again. */
    public suppressPreload() {
        this._preloadSuppressed = true;
    }

    private async _postStatus() {
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
            const sig = `${this._host.cloudProvider()}|${this._host.customBaseUrl()}`;
            if (sig !== this._cloudSig) {
                this._cloudSig = sig;
                void this._postCloudModels();
            }
        } catch (err) {
            this._post({
                type: 'status', online: false, retrying: true,
                error: (err as Error).message, apiUrl, localModel
            });
            void backend?.ensureRunning(); // nudge a (re)start; harmless if it's already healthy
            this._scheduleRetry();
        }
    }

    /** Poll again shortly (with a small backoff) until the backend answers. */
    private _scheduleRetry(fixedDelayMs?: number) {
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

    private _clearRetry() {
        if (this._retryTimer) {
            clearTimeout(this._retryTimer);
            this._retryTimer = undefined;
        }
        this._retryDelayMs = 1_000;
    }

    /** Cloud provider, key or base URL changed: refetch the model list and the status card. */
    private async _refreshCloud() {
        this._cloudSig = '';
        await this._postCloudModels(true);
        await this._postStatus();
    }

    /** Fetch the provider's model list (backend-cached) and hand it to the sidebar picker. */
    private async _postCloudModels(force = false) {
        if (!force && !vscode.workspace.getConfiguration('tokenguard').get<boolean>('cloudModelsAutoFetch', true)) {
            return;
        }
        try {
            const info = await this._host.api('GET', `/cloud/models${force ? '?force=true' : ''}`, undefined, 10_000);
            this._post({ type: 'cloudModels', info });
        } catch {
            // ignore: the status card already reports cloud problems
        }
    }

    /** Load the selected local model once the backend + Ollama are up, so it isn't shown idle. */
    private async _maybePreload(health: any) {
        if (this._preloadSuppressed || !vscode.workspace.getConfiguration('tokenguard').get<boolean>('preloadLocalModel', true)) {
            return;
        }
        if (!health?.ollama) {
            return; // wait until Ollama is reachable, then a later refresh will load it
        }
        const selected: string | undefined = health.local_model;
        const installed: string[] = health.local_models || [];
        if (!selected || !installed.includes(selected)) {
            return; // nothing selected, or the model isn't pulled
        }
        if (health.local_loaded === selected || this._preloadedFor === selected) {
            return;
        }
        this._preloadedFor = selected;
        this._post({ type: 'modelLoading', model: selected }); // "loading…" while Ollama loads it
        await this._host.switchLocalModel(selected);
        this._post({ type: 'modelLoading', model: null });     // clear the spinner either way
        void this._postStatus(); // reflect "loaded in memory" in the sidebar
    }

    /** Track a turn's request so the Stop button can abort it. */
    private _track(id: number): AbortController {
        this._inFlight.get(id)?.abort();
        const controller = new AbortController();
        this._inFlight.set(id, controller);
        return controller;
    }

    private _untrack(id: number, controller: AbortController) {
        if (this._inFlight.get(id) === controller) {
            this._inFlight.delete(id);
        }
    }

    private _cancelAll() {
        this._inFlight.forEach(c => c.abort());
        this._inFlight.clear();
    }

    /** Step 1: ask the backend where this prompt should go, without calling any model. */
    private async _analyze(id: number, prompt: string) {
        const { req, contextInfo } = buildRequest(prompt, this._editor());
        const controller = this._track(id);
        try {
            const analysis = await this._host.api('POST', '/analyze', req, ANALYZE_TIMEOUT_MS, controller.signal);
            this._post({ type: 'analysis', id, req, analysis, contextInfo });
        } catch (err) {
            this._post(controller.signal.aborted
                ? { type: 'cancelled', id }
                : { type: 'error', id, message: (err as Error).message });
        } finally {
            this._untrack(id, controller);
        }
    }

    /** Step 2: run the pipeline on the route the user (or the analysis) picked. */
    private async _run(id: number, req: ChatRequest, route: Route) {
        // Aborting closes the connection; the backend notices and cancels the model call.
        const controller = this._track(id);
        try {
            // start / delta / reset events render the answer while it's generated; 'result' finalizes it.
            const result = await this._host.apiStream('/chat/stream', { ...req, force_route: route },
                (event, data) => {
                    if (!controller.signal.aborted) { // Stop pressed: forward nothing more
                        this._post({ type: 'stream', id, event, data });
                    }
                },
                STREAM_IDLE_TIMEOUT_MS, controller.signal);
            this._post({ type: 'result', id, route, result });
        } catch (err) {
            this._post(controller.signal.aborted
                ? { type: 'cancelled', id }
                : { type: 'error', id, message: (err as Error).message });
        } finally {
            this._untrack(id, controller);
        }
        await this._postStatus();
    }

    // ---- Agent mode ------------------------------------------------------------------------

    /** Run an agent task. Its edits come back staged; nothing is written until the user accepts. */
    private async _runAgent(id: number, prompt: string, route: Route, history: unknown[]) {
        const editor = this._editor();
        const { req } = buildRequest(prompt, editor);
        const activeFile = editor && req.repo_path && editor.document.uri.scheme === 'file'
            ? path.relative(req.repo_path, editor.document.uri.fsPath)
            : null;
        const controller = this._track(id);
        try {
            const result = await this._host.apiStream<AgentRunResult>('/agent/stream',
                { ...req, route, history, active_file: activeFile && !activeFile.startsWith('..') ? activeFile : null },
                (event, data) => {
                    if (!controller.signal.aborted) {
                        this._post({ type: 'agentEvent', id, event, data });
                    }
                },
                STREAM_IDLE_TIMEOUT_MS, controller.signal);
            const changes = new Map<string, AgentChange>();
            for (const change of result.changes) {
                changes.set(change.path, { ...change, state: 'pending' });
            }
            this._agentRuns.set(id, { runId: result.run_id, root: result.repo_path, changes });
            // The page only needs the stats; full contents stay here for the diff view and Accept.
            const summary = result.changes.map(({ original: _o, proposed: _p, ...rest }) => rest);
            this._post({ type: 'agentResult', id, result: { ...result, changes: summary } });
            // Show modified files inline, Cursor-style. Added/deleted files wait for "Review in file",
            // so nothing is created or removed on disk before the user decides.
            if (vscode.workspace.getConfiguration('tokenguard').get<boolean>('agentInlineReview', true)) {
                for (const change of result.changes.filter(c => c.status === 'modified')) {
                    await this._agentReview(id, change.path, false);
                }
            }
        } catch (err) {
            this._post(controller.signal.aborted
                ? { type: 'cancelled', id }
                : { type: 'error', id, message: (err as Error).message });
        } finally {
            this._untrack(id, controller);
        }
    }

    private async _agentApprove(data: any) {
        try {
            await this._host.api('POST', '/agent/approve', {
                run_id: data.runId, request_id: data.requestId, approved: !!data.approved, always: !!data.always
            }, 10_000);
        } catch (err) {
            void vscode.window.showWarningMessage(`TokenGuard: could not answer the agent: ${(err as Error).message}`);
        }
    }

    private _agentDiffContent(uri: vscode.Uri): string {
        const params = new URLSearchParams(uri.query);
        const change = this._agentRuns.get(Number(params.get('turn')))?.changes.get(uri.path.replace(/^\//, ''));
        if (!change) {
            return '';
        }
        return (params.get('side') === 'original' ? change.original : change.proposed) ?? '';
    }

    /** Open the agent's proposal for one file in VS Code's diff editor. */
    private async _agentDiff(id: number, file: string) {
        const run = this._agentRuns.get(id);
        const change = run?.changes.get(file);
        if (!run || !change) {
            return;
        }
        // The run id keeps URIs unique: VS Code caches a virtual document by URI, and turn ids
        // restart when the sidebar reloads.
        const uri = (side: string) => vscode.Uri.from({
            scheme: AGENT_DIFF_SCHEME, path: '/' + file, query: `turn=${id}&run=${run.runId}&side=${side}`
        });
        const label = { added: 'new file', deleted: 'deleted', modified: 'proposed' }[change.status];
        await vscode.commands.executeCommand('vscode.diff', uri('original'), uri('proposed'),
            `${path.basename(file)} (agent: ${label})`, { preview: true });
    }

    /** Show a proposed change inline in its file. `ask`: confirm a conflict instead of skipping it. */
    private async _agentReview(id: number, file: string, ask: boolean) {
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
        if (vscode.workspace.getConfiguration('files', uri).get<string>('autoSave', 'off') !== 'off') {
            if (!ask) {
                return;
            }
            const choice = await vscode.window.showWarningMessage(
                'Auto Save is on, so the file would be saved with both the old (red) and new (green) lines until every '
                + 'change is decided. Review in the file anyway? (Diff shows the change without touching the file.)',
                { modal: true }, 'Review in file');
            if (choice !== 'Review in file') {
                return;
            }
        }
        const current = await currentText(uri);
        if (current !== (change.original ?? undefined)) {
            if (!ask) {
                return; // changed since the agent read it: leave it to the sidebar buttons
            }
            const choice = await vscode.window.showWarningMessage(
                `${file} has changed since the agent read it. Review the agent's version against the current file anyway?`,
                { modal: true }, 'Review');
            if (choice !== 'Review') {
                return;
            }
        }
        try {
            await this._inline.start(uri, current ?? null, change.proposed, outcome => this._onInlineDone(id, file, outcome));
            change.inline = true;
            this._post({ type: 'agentReviewing', id, path: file });
        } catch (err) {
            void vscode.window.showWarningMessage(`TokenGuard: ${(err as Error).message}`);
        }
    }

    /** An inline review finished (every block decided, or the editor was closed). */
    private _onInlineDone(id: number, file: string, outcome: ReviewOutcome) {
        const change = this._agentRuns.get(id)?.changes.get(file);
        if (change) {
            change.inline = false;
            change.state = outcome === 'rejected' ? 'rejected' : outcome === 'cancelled' ? 'pending' : 'accepted';
        }
        this._post({ type: 'agentApplied', id, results: [{ path: file, state: outcome }] });
    }

    private async _agentOpen(id: number, file: string) {
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
    private async _agentAccept(id: number, files: string[]) {
        const run = this._agentRuns.get(id);
        if (!run) {
            return;
        }
        const results: { path: string; state: string; reason?: string }[] = [];
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
                if (current !== (change.original ?? undefined)) {
                    const choice = await vscode.window.showWarningMessage(
                        `${file} has changed since the agent read it. Replace it with the agent's version anyway?`,
                        { modal: true }, 'Replace');
                    if (choice !== 'Replace') {
                        results.push({ path: file, state: 'pending', reason: 'changed since the agent read it' });
                        continue;
                    }
                }
                await applyChange(uri, change.proposed, current !== undefined);
                change.state = 'accepted';
                results.push({ path: file, state: 'accepted' });
            } catch (err) {
                results.push({ path: file, state: 'pending', reason: (err as Error).message });
            }
        }
        this._post({ type: 'agentApplied', id, results });
    }

    private async _agentReject(id: number, files: string[]) {
        const run = this._agentRuns.get(id);
        const results: { path: string; state: string }[] = [];
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
    private _editor(): vscode.TextEditor | undefined {
        const active = vscode.window.activeTextEditor;
        if (active) {
            return active;
        }
        const last = this._lastEditor;
        return last && vscode.window.visibleTextEditors.includes(last) ? last : undefined;
    }

    /** Tell the empty state what context a prompt would carry (debounced: selections change a lot). */
    private _postContextSoon() {
        clearTimeout(this._contextTimer);
        this._contextTimer = setTimeout(() => this._postContext(), 250);
    }

    private _postContext() {
        this._post({ type: 'context', info: buildRequest('', this._editor()).contextInfo });
    }

    /** "Insert" on a code block: replace the selection in the editor, or open a new untitled file. */
    private async _insertAtCursor(code: string, language: string) {
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

    private async _openLink(href: string) {
        if (/^https?:\/\//i.test(href)) {
            await vscode.env.openExternal(vscode.Uri.parse(href));
        }
    }

    /** media/chat/index.html with its {{placeholders}} filled in for this webview. */
    private _getHtmlForWebview(webview: vscode.Webview): string {
        const media = vscode.Uri.joinPath(this._extensionUri, 'media');
        const template = fs.readFileSync(vscode.Uri.joinPath(media, 'chat', 'index.html').fsPath, 'utf8');
        const values: Record<string, string> = {
            cspSource: webview.cspSource,
            nonce: getNonce(),
            mediaUri: webview.asWebviewUri(media).toString()
        };
        return template.replace(/\{\{(\w+)\}\}/g, (match, key: string) => values[key] ?? match);
    }
}

/** The prompt's context: current editor selection (if any) + the open workspace folder as the repo. */
function buildRequest(prompt: string, editor: vscode.TextEditor | undefined): { req: ChatRequest; contextInfo: string } {
    let code = '';
    const info: string[] = [];
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
async function currentText(uri: vscode.Uri): Promise<string | undefined> {
    const open = vscode.workspace.textDocuments.find(d => d.uri.toString() === uri.toString());
    if (open) {
        return open.getText();
    }
    try {
        return new TextDecoder().decode(await vscode.workspace.fs.readFile(uri));
    } catch {
        return undefined;
    }
}

/** Replace, create or delete a file through a WorkspaceEdit (undoable), then save it. */
async function applyChange(uri: vscode.Uri, proposed: string | null, exists: boolean) {
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
    } else {
        edit.createFile(uri, { overwrite: true });
        edit.insert(uri, new vscode.Position(0, 0), proposed);
    }
    if (!(await vscode.workspace.applyEdit(edit))) {
        throw new Error('VS Code refused the edit (is the file read-only?)');
    }
    const doc = await vscode.workspace.openTextDocument(uri);
    if (doc.isDirty && !(await doc.save())) {
        throw new Error('applied, but could not save');
    }
}

async function toLanguageId(lang: string): Promise<string> {
    const id = LANGUAGE_ALIASES[lang] ?? lang;
    return id && (await vscode.languages.getLanguages()).includes(id) ? id : 'plaintext';
}

function getNonce(): string {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let nonce = '';
    for (let i = 0; i < 32; i++) {
        nonce += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return nonce;
}
