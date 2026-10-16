import * as vscode from 'vscode';
import { BackendManager } from './backend';
import { LocalPilotViewProvider } from './chatView';
import { OllamaManager } from './ollama';

// Activation, commands, settings and the HTTP client for the LocalPilot backend
// (backend/main.py). The sidebar chat lives in chatView.ts + media/chat/.

// Providers that accept an API key (mock needs none). Keys live in VS Code SecretStorage.
const KEY_PROVIDERS = ['deepseek', 'anthropic', 'openai', 'custom'] as const;
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
        ollama: () => ollama,
        api,
        apiStream,
        apiUrl,
        localModel,
        cloudProvider,
        customBaseUrl,
        storeApiKey: async (provider, key) => {
            if (key) {
                await extensionContext.secrets.store(secretKey(provider), key);
            } else {
                await extensionContext.secrets.delete(secretKey(provider));
            }
        },
        switchLocalModel,
        downloadLocalModel
    });
    context.subscriptions.push(
        viewProvider,
        backend.onDidChangeState(() => viewProvider?.refreshStatus()),
        // Startup milestones: keep the sidebar's progress bar moving while the backend boots.
        backend.onDidChangeProgress(() => viewProvider?.refreshStatus()),
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
            || e.affectsConfiguration('tokenguard.localModel') || e.affectsConfiguration('tokenguard.customBaseUrl')) {
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
}

export function deactivate(): Promise<void> | undefined {
    // Stop everything this extension started: backend, local model memory, and our Ollama.
    return Promise.all([backend?.stop(), ollama?.stop()]).then(() => undefined);
}

function apiUrl(): string {
    // The backend we spawned binds a free port and reports it via baseUrl; the configured
    // address is only the default/fallback for finding an externally started backend.
    const spawned = backend?.baseUrl;
    if (spawned) {
        return spawned;
    }
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

function customBaseUrl(): string {
    return (vscode.workspace.getConfiguration('tokenguard').get<string>('customBaseUrl') || '').trim();
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
    const headers: Record<string, string> = {
        'X-LLM-Provider': provider,
        // The spawned backend requires this session token (see backend/main.py); a manually
        // started `make backend` runs in loopback-only mode and ignores it.
        'X-Token-Guard-Session': backend?.sessionToken ?? ''
    };
    const model = cloudModel();
    if (model) {
        headers['X-LLM-Model'] = model;
    }
    if (provider === 'custom') {
        const base = customBaseUrl();
        if (base) {
            headers['X-LLM-Base-Url'] = base;
        }
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
            headers: { 'Content-Type': 'application/json', 'X-Token-Guard-Session': backend?.sessionToken ?? '' },
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
    pulls?: Record<string, { percent: number; status?: string }>;
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
        async (progress, token) => {
            try {
                await api('POST', '/models/pull', { model }, 15_000);
            } catch (err) {
                void vscode.window.showErrorMessage(`TokenGuard: could not start download. ${(err as Error).message}`);
                return false;
            }
            let reported = 0;
            while (!token.isCancellationRequested) {
                await new Promise(r => setTimeout(r, 2_000));
                try {
                    const info = await fetchModels();
                    if (info.available.includes(model)) {
                        void vscode.window.showInformationMessage(`TokenGuard: ${model} is ready.`);
                        return true;
                    }
                    // Fill the notification's progress bar from Ollama's live bytes/percent.
                    const p = info.pulls?.[model];
                    if (p && p.percent > reported) {
                        progress.report({
                            message: ` ${p.percent}%${p.status ? ` · ${p.status}` : ''}`,
                            increment: p.percent - reported
                        });
                        reported = p.percent;
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
        const resp = await backendFetch(method, path, body, controller.signal, signal, () => timedOut);
        return (await resp.json()) as T;
    } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
    }
}

/**
 * POST to a Server-Sent Events endpoint (/chat/stream): `onEvent` gets each event as it arrives,
 * and the promise resolves with the `done` event's data (or rejects with the `error` event's).
 * The timeout is an idle timeout: the backend sends a heartbeat while it has nothing else to say,
 * so it only fires when the connection really stalls, however long the answer takes.
 */
async function apiStream<T>(
    path: string, body: unknown, onEvent: (event: string, data: any) => void, idleTimeoutMs: number, signal?: AbortSignal
): Promise<T> {
    const controller = new AbortController();
    let timedOut = false;
    let timer: NodeJS.Timeout | undefined;
    const resetIdle = () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
            timedOut = true;
            controller.abort();
        }, idleTimeoutMs);
    };
    const onAbort = () => controller.abort();
    if (signal?.aborted) {
        controller.abort();
    }
    signal?.addEventListener('abort', onAbort, { once: true });
    resetIdle();
    try {
        const resp = await backendFetch('POST', path, body, controller.signal, signal, () => timedOut, 'text/event-stream');
        if (!resp.body) {
            throw new Error('Backend returned an empty stream');
        }
        const reader = resp.body.getReader();
        // Stop reading as soon as we're aborted, without relying on fetch to error the body
        // (VS Code's patched fetch doesn't always); cancelling the reader also drops the connection.
        const aborted = new Promise<never>((_, reject) => {
            const stop = () => {
                reader.cancel().catch(() => undefined);
                reject(new DOMException('The operation was aborted.', 'AbortError'));
            };
            if (controller.signal.aborted) {
                stop();
            } else {
                controller.signal.addEventListener('abort', stop, { once: true });
            }
        });
        aborted.catch(() => undefined); // only observed through the race below
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
            let chunk: Awaited<ReturnType<typeof reader.read>>;
            try {
                chunk = await Promise.race([reader.read(), aborted]);
            } catch (err) {
                if (timedOut && !signal?.aborted) {
                    throw new Error(`LocalPilot backend stopped responding (no data for ${Math.round(idleTimeoutMs / 1000)}s).`);
                }
                throw err;
            }
            if (chunk.done) {
                break;
            }
            resetIdle();
            buffer = (buffer + decoder.decode(chunk.value, { stream: true })).replace(/\r\n/g, '\n');
            let end: number;
            while (!controller.signal.aborted && (end = buffer.indexOf('\n\n')) >= 0) {
                const block = buffer.slice(0, end);
                buffer = buffer.slice(end + 2);
                let event = 'message';
                const data: string[] = [];
                for (const line of block.split('\n')) {
                    if (line.startsWith('event:')) {
                        event = line.slice(6).trim();
                    } else if (line.startsWith('data:')) {
                        data.push(line.slice(5).replace(/^ /, ''));
                    } // ":" lines are heartbeats
                }
                if (!data.length) {
                    continue;
                }
                const payload = JSON.parse(data.join('\n'));
                if (event === 'done') {
                    return payload as T;
                }
                if (event === 'error') {
                    throw new Error(`Backend error ${payload.status}: ${payload.message}`);
                }
                onEvent(event, payload);
            }
        }
        if (controller.signal.aborted) {
            throw new DOMException('The operation was aborted.', 'AbortError');
        }
        throw new Error('LocalPilot backend closed the stream before the answer finished.');
    } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
        controller.abort(); // release the connection if we stopped reading early
    }
}

/** fetch() against the backend with LLM headers; turns network and HTTP failures into readable errors. */
async function backendFetch(
    method: 'GET' | 'POST', path: string, body: unknown, signal: AbortSignal,
    callerSignal: AbortSignal | undefined, timedOut: () => boolean, accept?: string
): Promise<Response> {
    if (backend?.state === 'starting') {
        await backend.ensureRunning();
    }
    let resp: Response;
    try {
        resp = await fetch(`${apiUrl()}${path}`, {
            method,
            headers: {
                'Content-Type': 'application/json',
                ...(accept ? { Accept: accept } : {}),
                ...(await llmHeaders())
            },
            body: body === undefined ? undefined : JSON.stringify(body),
            signal
        });
    } catch (err) {
        if (callerSignal?.aborted) {
            throw err; // cancelled by the caller, who already knows why
        }
        const reason = timedOut() ? 'timed out' : 'is not reachable';
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
    return resp;
}
