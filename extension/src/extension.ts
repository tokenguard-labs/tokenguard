import * as vscode from 'vscode';
import { BackendManager } from './backend';
import { LocalPilotViewProvider } from './chatView';
import { OllamaManager } from './ollama';

// Activation, commands, settings and the HTTP client for the LocalPilot backend
// (backend/main.py). The sidebar chat lives in chatView.ts + media/chat/.

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

    viewProvider = new LocalPilotViewProvider(context.extensionUri, {
        backend: () => backend,
        api,
        apiUrl,
        localModel,
        switchLocalModel,
        downloadLocalModel
    });
    context.subscriptions.push(
        viewProvider,
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
        vscode.commands.registerCommand('tokenguard.clearChat', () => viewProvider?.clearChat()),
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

async function api<T>(
    method: 'GET' | 'POST', path: string, body: unknown, timeoutMs: number, signal?: AbortSignal
): Promise<T> {
    if (backend?.state === 'starting') {
        await backend.ensureRunning();
    }
    // One controller for both the timeout and the caller's cancel (the sidebar's Stop button).
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
    }, timeoutMs);
    const onAbort = () => controller.abort();
    if (signal?.aborted) {
        controller.abort();
    }
    signal?.addEventListener('abort', onAbort, { once: true });
    try {
        let resp: Response;
        try {
            resp = await fetch(`${apiUrl()}${path}`, {
                method,
                headers: { 'Content-Type': 'application/json', ...(await llmHeaders()) },
                body: body === undefined ? undefined : JSON.stringify(body),
                signal: controller.signal
            });
        } catch (err) {
            if (signal?.aborted) {
                throw err; // cancelled by the caller, who already knows why
            }
            const reason = timedOut ? 'timed out' : 'is not reachable';
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
    } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
    }
}
