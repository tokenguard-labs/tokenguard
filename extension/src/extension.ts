import * as vscode from 'vscode';
import { BackendManager } from './backend';
import { OllamaManager } from './ollama';

// Talks to the LocalPilot backend (backend/main.py):
//   POST /analyze  -> routing decision only, no model called
//   POST /chat     -> runs the pipeline; force_route picks the local or global (cloud) LLM
//   GET  /health, /stats
// The flow mirrors scripts/streamlit_ui.py: analyze first, run simple tasks locally,
// and ask the user before a complex task goes to the global LLM.

type Route = 'local' | 'cloud';

interface ChatRequest {
    prompt: string;
    code: string;
    repo_path: string | null;
}

const ANALYZE_TIMEOUT_MS = 60_000;
const CHAT_TIMEOUT_MS = 600_000; // local 7B models can be slow on a cold start

// Providers that accept an API key (mock needs none). Keys live in VS Code SecretStorage.
const KEY_PROVIDERS = ['deepseek', 'anthropic', 'openai'] as const;
type KeyProvider = typeof KEY_PROVIDERS[number];
const SECRET_PREFIX = 'tokenguard.apiKey.';

let backend: BackendManager | undefined;
let ollama: OllamaManager | undefined;
let viewProvider: LocalPilotViewProvider | undefined;
let extensionContext: vscode.ExtensionContext;

export function activate(context: vscode.ExtensionContext) {
    console.log('⚡ LocalPilot extension activated successfully!');
    extensionContext = context;

    const output = vscode.window.createOutputChannel('LocalPilot Backend');
    backend = new BackendManager(context.extensionPath, output, apiUrl);
    ollama = new OllamaManager(context.extensionPath, output, ollamaUrl);
    context.subscriptions.push(output, backend, ollama);

    viewProvider = new LocalPilotViewProvider();
    context.subscriptions.push(
        backend.onDidChangeState(() => viewProvider?.refreshStatus()),
        // When Ollama comes up (we may have just started it), refresh so the selected model loads.
        ollama.onDidChangeState(() => viewProvider?.refreshStatus())
    );

    // Last-resort cleanup if VS Code exits without calling deactivate().
    process.on('exit', () => {
        backend?.killSync();
        ollama?.killSync();
    });

    // Re-check provider/model when the user edits the VS Code settings.
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(e => {
        if (e.affectsConfiguration('tokenguard.cloudProvider') || e.affectsConfiguration('tokenguard.cloudModel')
            || e.affectsConfiguration('tokenguard.localModel')) {
            viewProvider?.refreshStatus();
        }
    }));

    // Register Webview Provider
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('localpilot.chatView', viewProvider, {
            webviewOptions: { retainContextWhenHidden: true }
        })
    );

    // Register a Command to force-open the view via Command Palette (Ctrl+Shift+P)
    context.subscriptions.push(
        vscode.commands.registerCommand('tokenguard.openChat', () => {
            vscode.commands.executeCommand('localpilot.chatView.focus');
        }),
        vscode.commands.registerCommand('tokenguard.restartBackend', async () => {
            await backend?.restart();
            if (backend?.state === 'external') {
                void vscode.window.showInformationMessage(
                    `LocalPilot backend at ${apiUrl()} was started outside VS Code; restart it there.`);
            }
        }),
        vscode.commands.registerCommand('tokenguard.showBackendLog', () => output.show()),
        vscode.commands.registerCommand('tokenguard.setApiKey', () => setApiKey()),
        vscode.commands.registerCommand('tokenguard.clearApiKey', () => clearApiKey()),
        vscode.commands.registerCommand('tokenguard.pullLocalModel', () => pullLocalModelCommand()),
        vscode.commands.registerCommand('tokenguard.stopLocalModels', async () => {
            const freed = await ollama?.unloadModels();
            viewProvider?.suppressPreload();
            void vscode.window.showInformationMessage(
                freed && freed.length ? `TokenGuard: unloaded ${freed.join(', ')}.` : 'TokenGuard: no local models were loaded.');
            await viewProvider?.refreshStatus();
        })
    );

    // Spawn the backend now so it is warm by the time the sidebar asks for status.
    void backend.ensureRunning();
    // Start Ollama if needed (it is stopped again on exit when we started it).
    void ollama.ensureRunning();

    // Open the container so the activity bar entry is selected on launch.
    void vscode.commands.executeCommand('workbench.view.extension.localpilot-sidebar');
}

export function deactivate(): Promise<void> | undefined {
    // Stop everything this extension started: backend, local model memory, and our Ollama.
    return Promise.all([backend?.stop(), ollama?.stop()]).then(() => undefined);
}

function apiUrl(): string {
    const url = vscode.workspace.getConfiguration('tokenguard').get<string>('apiUrl') || 'http://localhost:8000';
    return url.replace(/\/+$/, '');
}

function ollamaUrl(): string {
    const url = vscode.workspace.getConfiguration('tokenguard').get<string>('ollamaUrl') || 'http://localhost:11434';
    return url.replace(/\/+$/, '');
}

function cloudProvider(): string {
    return vscode.workspace.getConfiguration('tokenguard').get<string>('cloudProvider') || 'deepseek';
}

function cloudModel(): string {
    return (vscode.workspace.getConfiguration('tokenguard').get<string>('cloudModel') || '').trim();
}

function localModel(): string {
    return (vscode.workspace.getConfiguration('tokenguard').get<string>('localModel') || '').trim();
}

function secretKey(provider: string): string {
    return `${SECRET_PREFIX}${provider}`;
}

async function getApiKey(provider: string): Promise<string | undefined> {
    return extensionContext.secrets.get(secretKey(provider));
}

/** LLM settings forwarded to the backend on every request. The key never touches disk. */
async function llmHeaders(): Promise<Record<string, string>> {
    const provider = cloudProvider();
    const headers: Record<string, string> = { 'X-LLM-Provider': provider };
    const model = cloudModel();
    if (model) {
        headers['X-LLM-Model'] = model;
    }
    const local = localModel();
    if (local) {
        headers['X-Local-Model'] = local;
    }
    const key = await getApiKey(provider);
    if (key) {
        headers['X-LLM-Api-Key'] = key;
    }
    return headers;
}

/** Ask the backend to unload the previous local model and load the selected one (one at a time). */
async function switchLocalModel(model: string): Promise<void> {
    try {
        await fetch(`${apiUrl()}/local-model`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: model || null, preload: true }),
            signal: AbortSignal.timeout(60_000)
        });
    } catch {
        // Best effort: the backend re-enforces one-model-at-a-time on the next local prompt.
    }
}

interface RecommendedModel {
    name: string;
    params: string;
    size_gb: number;
    min_ram_gb: number;
    comfortable: boolean;
    note: string;
    tags: string[];
    description: string;
}

interface ModelsInfo {
    local_model: string;
    local_loaded: string | null;
    available: string[];
    recommended: RecommendedModel[];
    pulling: string[];
    system: Record<string, number | boolean>;
    ollama: boolean;
}

async function fetchModels(): Promise<ModelsInfo> {
    return api<ModelsInfo>('GET', '/models', undefined, 10_000);
}

/** Download a model with `ollama pull` (via the backend), polling until it is installed. */
async function downloadLocalModel(model: string): Promise<boolean> {
    let info: ModelsInfo;
    try {
        info = await fetchModels();
    } catch (err) {
        void vscode.window.showErrorMessage(`TokenGuard: backend unreachable. ${(err as Error).message}`);
        return false;
    }
    if (info.available.includes(model)) {
        return true;
    }
    const rec = info.recommended.find(m => m.name === model);
    const size = rec && rec.size_gb ? ` (~${rec.size_gb} GB)` : '';
    return vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `TokenGuard: downloading ${model}${size}`, cancellable: true },
        async (_progress, token) => {
            try {
                await api('POST', '/models/pull', { model }, 15_000);
            } catch (err) {
                void vscode.window.showErrorMessage(`TokenGuard: could not start download. ${(err as Error).message}`);
                return false;
            }
            while (!token.isCancellationRequested) {
                await new Promise(r => setTimeout(r, 2_000));
                try {
                    if ((await fetchModels()).available.includes(model)) {
                        void vscode.window.showInformationMessage(`TokenGuard: ${model} is ready.`);
                        return true;
                    }
                } catch {
                    // keep polling; the pull continues on the backend
                }
            }
            return false; // cancelled: the backend pull keeps running in the background
        }
    );
}

/** "TokenGuard: Download Local Model" — pick a recommended model and pull it. */
async function pullLocalModelCommand(): Promise<void> {
    let info: ModelsInfo;
    try {
        info = await fetchModels();
    } catch (err) {
        void vscode.window.showErrorMessage(`TokenGuard: backend unreachable. ${(err as Error).message}`);
        return;
    }
    const items = [
        ...info.recommended.map(m => ({
            label: m.name,
            description: `~${m.size_gb} GB · ${m.comfortable ? 'fits in RAM' : 'may use swap (slower)'}`,
            detail: m.description
        })),
        ...info.available.map(name => ({ label: name, description: 'already installed' }))
    ];
    if (!items.length) {
        void vscode.window.showInformationMessage('TokenGuard: no models available to download.');
        return;
    }
    const picked = await vscode.window.showQuickPick(items, {
        title: 'TokenGuard: Download Local Model',
        placeHolder: 'Pick a model to pull with Ollama'
    });
    if (!picked) {
        return;
    }
    if (info.available.includes(picked.label)) {
        void vscode.window.showInformationMessage(`TokenGuard: ${picked.label} is already installed.`);
        return;
    }
    await downloadLocalModel(picked.label);
    await viewProvider?.refreshStatus();
}

/** "TokenGuard: Set Cloud API Key" — pick a provider and store its key in SecretStorage. */
async function setApiKey(): Promise<void> {
    const configured = cloudProvider();
    const picks = KEY_PROVIDERS.map(p => ({
        label: p,
        description: p === configured ? 'current tokenguard.cloudProvider' : undefined
    }));
    const picked = await vscode.window.showQuickPick(picks, {
        title: 'TokenGuard: Set Cloud API Key',
        placeHolder: `Provider to store a key for (current: ${configured})`
    });
    if (!picked) {
        return;
    }
    const provider = picked.label as KeyProvider;
    const existing = await getApiKey(provider);
    const value = await vscode.window.showInputBox({
        title: `TokenGuard: ${provider} API key`,
        prompt: existing
            ? 'Enter a new key to replace the stored one, or leave empty to remove it.'
            : 'Stored in VS Code SecretStorage — never written to backend files.',
        placeHolder: existing ? '•••••••• (leave empty to clear)' : 'paste your API key',
        password: true,
        ignoreFocusOut: true
    });
    if (value === undefined) {
        return; // cancelled
    }
    if (value.trim() === '') {
        await extensionContext.secrets.delete(secretKey(provider));
        void vscode.window.showInformationMessage(`TokenGuard: cleared the ${provider} API key.`);
    } else {
        await extensionContext.secrets.store(secretKey(provider), value.trim());
        void vscode.window.showInformationMessage(`TokenGuard: ${provider} API key saved to SecretStorage.`);
    }
    if (provider !== configured) {
        await vscode.workspace.getConfiguration('tokenguard')
            .update('cloudProvider', provider, vscode.ConfigurationTarget.Global);
    }
    await viewProvider?.refreshStatus();
}

/** "TokenGuard: Clear Cloud API Key" — remove the key for the active provider. */
async function clearApiKey(): Promise<void> {
    const provider = cloudProvider();
    if (!(await getApiKey(provider))) {
        void vscode.window.showInformationMessage(`TokenGuard: no stored API key for ${provider}.`);
        return;
    }
    const choice = await vscode.window.showWarningMessage(
        `Remove the stored ${provider} API key from SecretStorage?`, { modal: true }, 'Remove');
    if (choice !== 'Remove') {
        return;
    }
    await extensionContext.secrets.delete(secretKey(provider));
    void vscode.window.showInformationMessage(`TokenGuard: cleared the ${provider} API key.`);
    await viewProvider?.refreshStatus();
}

async function api<T>(method: 'GET' | 'POST', path: string, body: unknown, timeoutMs: number): Promise<T> {
    if (backend?.state === 'starting') {
        await backend.ensureRunning();
    }
    let resp: Response;
    try {
        resp = await fetch(`${apiUrl()}${path}`, {
            method,
            headers: { 'Content-Type': 'application/json', ...(await llmHeaders()) },
            body: body === undefined ? undefined : JSON.stringify(body),
            signal: AbortSignal.timeout(timeoutMs)
        });
    } catch (err) {
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
        } catch {
            // plain-text error body
        }
        throw new Error(`Backend error ${resp.status}: ${detail}`);
    }
    return (await resp.json()) as T;
}

/** The prompt's context: current editor selection (if any) + the open workspace folder as the repo. */
function buildRequest(prompt: string): { req: ChatRequest; contextInfo: string } {
    const editor = vscode.window.activeTextEditor;
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

class LocalPilotViewProvider implements vscode.WebviewViewProvider {
    private _view?: vscode.WebviewView;
    private _retryTimer?: NodeJS.Timeout;
    private _retryDelayMs = 1_000;
    private _preloadedFor?: string;      // model we've already asked the backend to load
    private _preloadSuppressed = false;  // user explicitly unloaded: don't auto-reload until they pick again

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ) {
        this._view = webviewView;
        webviewView.webview.options = { enableScripts: true };
        webviewView.webview.html = this._getHtmlForWebview();
        webviewView.onDidDispose(() => {
            this._clearRetry();
            this._view = undefined;
        });

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
                        if (!(await downloadLocalModel(model))) {
                            await this._postStatus();
                            break;
                        }
                    }
                    await vscode.workspace.getConfiguration('tokenguard')
                        .update('localModel', model, vscode.ConfigurationTarget.Global);
                    this._preloadSuppressed = false;
                    this._preloadedFor = model || undefined;
                    // Unload the old local model and warm up the new one so only one is resident.
                    await switchLocalModel(model);
                    this._post({ type: 'modelLoading', model: null });
                    await this._postStatus();
                    break;
                }
            }
        });
    }

    private _post(message: unknown) {
        void this._view?.webview.postMessage(message);
    }

    public refreshStatus() {
        void this._postStatus();
    }

    /** After an explicit unload, don't auto-reload until the user picks a model again. */
    public suppressPreload() {
        this._preloadSuppressed = true;
    }

    private async _postStatus() {
        const state = backend?.state;
        if (state === 'failed') {
            this._post({
                type: 'status', online: false, retrying: true,
                error: backend?.lastError || 'backend failed to start', apiUrl: apiUrl(), localModel: localModel()
            });
            this._scheduleRetry(5_000); // keep retrying, but don't spin the "starting" spinner
            return;
        }
        // Backend still booting (or not started yet): show the spinner and keep polling instead
        // of flashing "offline" — the first /health can fail before uvicorn finishes importing.
        if (state === 'starting' || state === 'stopped' || state === undefined) {
            this._post({ type: 'status', online: false, starting: true, apiUrl: apiUrl(), localModel: localModel() });
            void backend?.ensureRunning();
            this._scheduleRetry();
            return;
        }
        try {
            const [health, stats] = await Promise.all([
                api('GET', '/health', undefined, 5_000),
                api('GET', '/stats', undefined, 5_000)
            ]);
            this._clearRetry();
            this._post({ type: 'status', online: true, health, stats, apiUrl: apiUrl(), localModel: localModel() });
            void this._maybePreload(health);
        } catch (err) {
            this._post({
                type: 'status', online: false, retrying: true,
                error: (err as Error).message, apiUrl: apiUrl(), localModel: localModel()
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
        await switchLocalModel(selected);
        this._post({ type: 'modelLoading', model: null });     // clear the spinner either way
        void this._postStatus(); // reflect "loaded in memory" in the sidebar
    }

    /** Step 1: ask the backend where this prompt should go, without calling any model. */
    private async _analyze(id: number, prompt: string) {
        const { req, contextInfo } = buildRequest(prompt);
        try {
            const analysis = await api('POST', '/analyze', req, ANALYZE_TIMEOUT_MS);
            this._post({ type: 'analysis', id, req, analysis, contextInfo });
        } catch (err) {
            this._post({ type: 'error', id, message: (err as Error).message });
        }
    }

    /** Step 2: run the pipeline on the route the user (or the analysis) picked. */
    private async _run(id: number, req: ChatRequest, route: Route) {
        try {
            const result = await api('POST', '/chat', { ...req, force_route: route }, CHAT_TIMEOUT_MS);
            this._post({ type: 'result', id, route, result });
        } catch (err) {
            this._post({ type: 'error', id, message: (err as Error).message });
        }
        await this._postStatus();
    }

    private _getHtmlForWebview(): string {
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
                .model-row { display: flex; align-items: center; gap: 8px; font-size: 11px; }
                .model-row label { opacity: 0.85; white-space: nowrap; }
                select {
                    flex: 1;
                    min-width: 0;
                    background: var(--vscode-dropdown-background);
                    color: var(--vscode-dropdown-foreground);
                    border: 1px solid var(--vscode-dropdown-border);
                    border-radius: 4px;
                    padding: 4px 6px;
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
                <div class="model-row">
                    <label for="localModel">Local model</label>
                    <select id="localModel" title="Ollama model used for local inference"></select>
                </div>
                <div id="modelState" class="muted"></div>
                <div id="systemInfo" class="muted"></div>
                <textarea id="prompt" placeholder="Ask LocalPilot about your repository… (Enter to send, Shift+Enter for newline)"></textarea>
                <button id="send">Send Request</button>
                <div id="history" class="chat-history"></div>
            </div>

            <script nonce="${nonce}">
                const vscode = acquireVsCodeApi();
                const history = document.getElementById('history');
                const input = document.getElementById('prompt');
                const sendBtn = document.getElementById('send');
                const modelSelect = document.getElementById('localModel');

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
                            const busy = inFlight.includes(m.name) ? ' — downloading…' : '';
                            opts.push('<option value="' + esc(m.name) + '">' + esc(m.name) + size + warn + busy + '</option>');
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
                        (r.is_mock ? '<div class="warn">⚠️ Mock answer — not a real cloud call. ' + esc(r.cloud_fallback_reason || 'CLOUD_PROVIDER=mock') + '</div>' : '') +
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
                        const retrying = m.retrying ? ' <span class="spinner">retrying…</span>' : '';
                        el.innerHTML = '<span class="offline">● Backend offline</span> (' + esc(m.apiUrl) + ')' + retrying +
                            '<div class="muted">' + esc(m.error || '') + '</div>' +
                            '<div class="choices"><button id="retryStatus" class="secondary">Retry now</button></div>';
                        return;
                    }
                    const h = m.health, s = m.stats;
                    const provider = esc(h.cloud_provider);
                    let cloudState;
                    if (h.cloud_valid) {
                        cloudState = '<code>' + provider + '</code> ✓';
                    } else if (h.cloud_provider === 'mock') {
                        cloudState = '<code>mock</code>';
                    } else {
                        cloudState = '<code>' + provider + '</code> <span class="warn">⚠ ' +
                            esc(h.cloud_detail || 'not configured') + '</span>';
                    }
                    const keyBtn = (!h.cloud_valid && h.cloud_provider !== 'mock')
                        ? '<div class="choices"><button id="setApiKey" class="secondary">🔑 Set API key</button></div>'
                        : '';
                    el.innerHTML = '● API online · cloud: ' + cloudState + ' · local: <code>' +
                        esc(h.local_model) + '</code> · Ollama ' + (h.ollama ? '✅' : '❌') + keyBtn +
                        '<div class="stats"><span>🖥️ Local: <b>' + s.local_requests + '</b></span><span>☁️ Cloud: <b>' +
                        s.cloud_requests + '</b></span><span>Tokens saved: <b>' + fmt(s.total_tokens_saved) + '</b> (' +
                        s.cloud_token_reduction_pct + '%)</span></div>';
                    const sys = document.getElementById('systemInfo');
                    if (sys && h.system) {
                        const g = h.system;
                        sys.textContent = 'Host: ' + g.total_ram_gb + ' GB RAM' +
                            (g.swap_total_gb ? ' + ' + g.swap_total_gb + ' GB swap' : '') +
                            (g.has_gpu ? ' · ' + g.gpu_vram_gb + ' GB VRAM' : '') +
                            ' · ' + g.cpu_count + ' CPU' + (g.cpu_count === 1 ? '' : 's');
                    }
                }

                sendBtn.addEventListener('click', send);
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
                document.addEventListener('click', e => {
                    const target = e.target.closest('button');
                    if (!target) return;
                    if (target.id === 'retryStatus') { vscode.postMessage({ type: 'refreshStatus' }); return; }
                    if (target.id === 'setApiKey') { vscode.postMessage({ type: 'setApiKey' }); return; }
                    if (target.dataset.route && !busy) {
                        target.dataset.used = '1';
                        run(Number(target.dataset.id), target.dataset.route);
                    }
                });

                window.addEventListener('message', event => {
                    const m = event.data;
                    const turn = turns.get(m.id);
                    switch (m.type) {
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

function getNonce(): string {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let nonce = '';
    for (let i = 0; i < 32; i++) {
        nonce += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return nonce;
}
