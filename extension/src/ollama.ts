import * as vscode from 'vscode';
import { ChildProcess, spawn, spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

// Owns the Ollama lifecycle for the extension:
//   - starts `ollama serve` if it isn't already reachable (and remembers that it did),
//   - frees model memory on stop (POST /api/generate {keep_alive: 0} for every running model),
//   - stops the `ollama serve` it started on deactivate / process exit.
// An Ollama that was already running (e.g. a systemd service) is reused and NEVER killed, so we
// don't fight the user's own setup. `setup.sh` records a PID in .ollama.pid when it starts one.

const STARTUP_TIMEOUT_MS = 30_000;
const STOP_GRACE_MS = 3_000;
const INSTALL_TIMEOUT_MS = 30 * 60_000;

// Official install commands, run in a terminal so the user sees the output and can enter a password.
const INSTALL_COMMANDS: Partial<Record<NodeJS.Platform, string>> = {
    linux: 'curl -fsSL https://ollama.com/install.sh | sh',
    darwin: 'brew install ollama',
    win32: 'winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements'
};
export const OLLAMA_DOWNLOAD_PAGE = 'https://ollama.com/download';

export type OllamaState = 'checking' | 'missing' | 'installing' | 'stopped' | 'starting' | 'running' | 'external' | 'failed';

export class OllamaManager implements vscode.Disposable {
    private _proc?: ChildProcess;
    private _starting?: Promise<void>;
    private _state: OllamaState = 'stopped';
    private _lastError?: string;
    private _stopping = false;
    private _installing?: Promise<boolean>;
    private readonly _markerPath: string;
    private readonly _onDidChangeState = new vscode.EventEmitter<OllamaState>();
    readonly onDidChangeState = this._onDidChangeState.event;

    constructor(
        extensionPath: string,
        private readonly _output: vscode.OutputChannel,
        private readonly _url: () => string
    ) {
        this._markerPath = path.resolve(extensionPath, '..', '.ollama.pid');
    }

    get state(): OllamaState { return this._state; }
    get lastError(): string | undefined { return this._lastError; }

    /**
     * Resolves once Ollama answers /api/tags; starts it if needed. Never rejects.
     * `force` starts it even when tokenguard.autoStartOllama is off (the user clicked "Start").
     */
    ensureRunning(force = false): Promise<void> {
        if (this._starting) {
            return this._starting;
        }
        this._starting = this._ensure(force).finally(() => { this._starting = undefined; });
        return this._starting;
    }

    /** True when the Ollama server answers right now (no state change, no auto-start). */
    isHealthy(): Promise<boolean> {
        return this._healthy();
    }

    /** Names of the models pulled into Ollama, or undefined when it isn't reachable. */
    async listModels(): Promise<string[] | undefined> {
        try {
            const resp = await fetch(`${this._url().replace(/\/+$/, '')}/api/tags`, { signal: AbortSignal.timeout(3_000) });
            if (!resp.ok) {
                return undefined;
            }
            const data = await resp.json() as { models?: Array<{ name?: string; model?: string }> };
            return (data.models || []).map(m => m.name || m.model).filter((n): n is string => !!n);
        } catch {
            return undefined;
        }
    }

    /** Whether this platform has an install command we can run (else: send the user to the download page). */
    canInstall(): boolean {
        if (process.platform === 'darwin') {
            return this._which('brew');
        }
        return !!INSTALL_COMMANDS[process.platform];
    }

    /**
     * Install Ollama with the official installer in a terminal (it may ask for a password), showing a
     * progress notification until the installer finishes. Resolves true when the CLI is available.
     */
    install(): Promise<boolean> {
        if (!this._installing) {
            this._installing = this._install().finally(() => { this._installing = undefined; });
        }
        return this._installing;
    }

    private async _install(): Promise<boolean> {
        const command = INSTALL_COMMANDS[process.platform];
        if (!command || !this.canInstall()) {
            void vscode.env.openExternal(vscode.Uri.parse(OLLAMA_DOWNLOAD_PAGE));
            return false;
        }
        const marker = path.join(os.tmpdir(), `tokenguard-ollama-install-${process.pid}-${Date.now()}`);
        const terminal = process.platform === 'win32'
            ? vscode.window.createTerminal({
                name: 'Install Ollama',
                shellPath: 'powershell.exe',
                shellArgs: ['-NoProfile', '-Command',
                    `${command}; Set-Content -Path '${marker}' -Value $LASTEXITCODE; Read-Host 'Press Enter to close'`]
            })
            : vscode.window.createTerminal({
                name: 'Install Ollama',
                shellPath: '/bin/bash',
                // $1 = marker file: the installer's exit code is written there when it finishes.
                shellArgs: ['-c', `set -o pipefail; ${command}; echo $? > "$1"; echo; read -rp "Press Enter to close this terminal"`,
                    'tokenguard', marker]
            });
        terminal.show();
        this._output.appendLine(`[tokenguard] installing Ollama: ${command}`);
        this._setState('installing');

        const exitCode = await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'TokenGuard: installing Ollama',
                cancellable: true
            },
            async (progress, token) => {
                progress.report({ message: 'follow the "Install Ollama" terminal (it may ask for your password)…' });
                let closed = false;
                const sub = vscode.window.onDidCloseTerminal(t => { closed = closed || t === terminal; });
                const deadline = Date.now() + INSTALL_TIMEOUT_MS;
                try {
                    while (!token.isCancellationRequested && Date.now() < deadline) {
                        const code = this._readInstallMarker(marker);
                        if (code !== undefined) {
                            return code;
                        }
                        if (closed) {
                            return undefined;
                        }
                        await new Promise(r => setTimeout(r, 1_000));
                    }
                    return undefined;
                } finally {
                    sub.dispose();
                    fs.rmSync(marker, { force: true });
                }
            }
        );

        const ok = !!this._cliPath();
        if (ok) {
            this._output.appendLine('[tokenguard] Ollama installed.');
            this._setState('stopped');
        } else {
            this._setState('missing', exitCode !== undefined && exitCode !== 0
                ? `the installer failed (exit code ${exitCode}); see the "Install Ollama" terminal`
                : 'Ollama is not installed');
        }
        return ok;
    }

    private _readInstallMarker(marker: string): number | undefined {
        try {
            const text = fs.readFileSync(marker, 'utf8').replace(/^\uFEFF/, '').trim();
            return text ? Number(text) || 0 : undefined;
        } catch {
            return undefined;
        }
    }

    /** Unload every running model now (frees RAM/VRAM). Returns the model names. */
    async unloadModels(): Promise<string[]> {
        const base = this._url().replace(/\/+$/, '');
        let names: string[] = [];
        try {
            const resp = await fetch(`${base}/api/ps`, { signal: AbortSignal.timeout(3_000) });
            if (!resp.ok) {
                return [];
            }
            const data = await resp.json() as { models?: Array<{ name?: string; model?: string }> };
            names = (data.models || []).map(m => m.name || m.model).filter((n): n is string => !!n);
        } catch {
            return []; // Ollama not reachable: nothing to unload
        }
        await Promise.all(names.map(name => fetch(`${base}/api/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: name, keep_alive: 0, stream: false }),
            signal: AbortSignal.timeout(5_000)
        }).catch(() => undefined)));
        if (names.length) {
            this._output.appendLine(`[tokenguard] unloaded local model(s): ${names.join(', ')}`);
        }
        return names;
    }

    /** Free model memory and stop the Ollama this tool started (external instances are kept). */
    async stop(): Promise<void> {
        await this.unloadModels();
        if (!vscode.workspace.getConfiguration('tokenguard').get<boolean>('stopOllamaOnExit', true)) {
            return;
        }
        const pid = this._ownedPid();
        if (!pid || !this._alive(pid) || !this._looksLikeOllama(pid)) {
            return;
        }
        this._output.appendLine(`[tokenguard] stopping Ollama (pid ${pid})…`);
        this._stopping = true;
        const exited = this._proc && this._proc.pid === pid
            ? new Promise<void>(resolve => this._proc!.once('exit', () => resolve()))
            : Promise.resolve();
        this._killTree(pid, 'SIGTERM');
        const timer = setTimeout(() => this._killTree(pid, 'SIGKILL'), STOP_GRACE_MS);
        await exited;
        clearTimeout(timer);
        this._stopping = false;
        this._clearMarker();
    }

    /** Synchronous best-effort cleanup for the process 'exit' event (kill our children). */
    killSync(): void {
        if (this._proc?.pid) {
            this._killTree(this._proc.pid, 'SIGKILL');
        }
        const pid = this._readMarker();
        if (pid && pid !== this._proc?.pid && this._looksLikeOllama(pid)) {
            this._killTree(pid, 'SIGKILL');
        }
    }

    dispose(): void {
        void this.stop();
        this._onDidChangeState.dispose();
    }

    private _setState(state: OllamaState, error?: string) {
        if (state === this._state && error === this._lastError) {
            return;
        }
        this._state = state;
        this._lastError = error;
        this._onDidChangeState.fire(state);
    }

    private async _ensure(force: boolean): Promise<void> {
        if (this._state !== 'running' && this._state !== 'external' && this._state !== 'installing') {
            this._setState('checking'); // so the sidebar can say "checking Ollama…" right away
        }
        if (await this._healthy()) {
            if (this._state === 'running' && this._proc && this._proc.exitCode === null) {
                return; // the one we started is still answering
            }
            this._setState('external'); // started elsewhere (e.g. systemd): leave it alone
            return;
        }
        if (this._proc && this._proc.exitCode === null) {
            await this._waitHealthy();
            return;
        }
        const cli = this._cliPath();
        if (!cli) {
            if (!this._installing) {
                this._setState('missing', 'Ollama is not installed (the `ollama` CLI was not found)');
            }
            return;
        }
        if (!force && !vscode.workspace.getConfiguration('tokenguard').get<boolean>('autoStartOllama', true)) {
            this._setState('stopped', 'auto-start disabled (tokenguard.autoStartOllama)');
            return;
        }

        this._output.appendLine('[tokenguard] starting ollama serve…');
        this._setState('starting');
        const proc = spawn(cli, ['serve'], {
            detached: process.platform !== 'win32', // own process group so we can kill the tree
            windowsHide: true,
            stdio: ['ignore', 'pipe', 'pipe']
        });
        this._proc = proc;
        proc.stdout?.on('data', d => this._output.append(d.toString()));
        proc.stderr?.on('data', d => this._output.append(d.toString()));
        proc.on('error', err => this._setState('failed', `spawn failed: ${err.message}`));
        proc.on('exit', (code, signal) => {
            if (this._proc === proc) {
                this._proc = undefined;
                this._setState(this._stopping ? 'stopped' : 'failed',
                    this._stopping ? undefined : `ollama exited (code ${code}, signal ${signal})`);
            }
        });
        await this._waitHealthy();
    }

    private async _waitHealthy(): Promise<void> {
        const deadline = Date.now() + STARTUP_TIMEOUT_MS;
        while (Date.now() < deadline) {
            if (this._proc && (this._proc.exitCode !== null)) {
                return; // exit handler already reported it
            }
            if (await this._healthy()) {
                this._setState('running');
                return;
            }
            await new Promise(r => setTimeout(r, 500));
        }
        this._setState('failed', `ollama did not answer ${this._url()}/api/tags within ${STARTUP_TIMEOUT_MS / 1000}s`);
    }

    private async _healthy(): Promise<boolean> {
        try {
            const resp = await fetch(`${this._url().replace(/\/+$/, '')}/api/tags`, { signal: AbortSignal.timeout(2_000) });
            return resp.ok;
        } catch {
            return false;
        }
    }

    /** The `ollama` executable: on PATH, else a standard install location (a fresh install isn't on our PATH yet). */
    private _cliPath(): string | undefined {
        if (this._which('ollama')) {
            return 'ollama';
        }
        const candidates = process.platform === 'win32'
            ? [path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Ollama', 'ollama.exe')]
            : ['/usr/local/bin/ollama', '/usr/bin/ollama', '/opt/homebrew/bin/ollama',
                '/Applications/Ollama.app/Contents/Resources/ollama'];
        return candidates.find(p => fs.existsSync(p));
    }

    private _which(name: string): boolean {
        const cmd = process.platform === 'win32' ? 'where' : 'which';
        try {
            return spawnSync(cmd, [name], { windowsHide: true }).status === 0;
        } catch {
            return false;
        }
    }

    /** The PID of the Ollama we should stop: our child, else the setup.sh marker. */
    private _ownedPid(): number | undefined {
        if (this._proc?.pid && this._proc.exitCode === null) {
            return this._proc.pid;
        }
        return this._readMarker();
    }

    private _readMarker(): number | undefined {
        try {
            const pid = Number(fs.readFileSync(this._markerPath, 'utf8').trim());
            return Number.isInteger(pid) && pid > 0 ? pid : undefined;
        } catch {
            return undefined;
        }
    }

    private _clearMarker(): void {
        try {
            fs.rmSync(this._markerPath, { force: true });
        } catch {
            // ignore
        }
    }

    private _alive(pid: number): boolean {
        try {
            process.kill(pid, 0);
            return true;
        } catch {
            return false;
        }
    }

    /** Guard against killing an unrelated process that reused the marker PID (Linux). */
    private _looksLikeOllama(pid: number): boolean {
        if (process.platform !== 'linux') {
            return true;
        }
        try {
            return fs.readFileSync(`/proc/${pid}/comm`, 'utf8').trim().includes('ollama');
        } catch {
            return false;
        }
    }

    private _killPid(pid: number, signal: NodeJS.Signals): void {
        if (process.platform === 'win32') {
            try {
                spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
            } catch {
                // ignore
            }
            return;
        }
        try {
            process.kill(pid, signal);
        } catch {
            // already gone
        }
    }

    private _killTree(pid: number, signal: NodeJS.Signals): void {
        if (process.platform === 'win32') {
            this._killPid(pid, signal);
            return;
        }
        try {
            process.kill(-pid, signal); // negative PID = the whole process group
        } catch {
            this._killPid(pid, signal);
        }
    }
}
