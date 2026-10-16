import * as vscode from 'vscode';
import { ChildProcess, spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

// Owns the LocalPilot backend process: spawns `python -m uvicorn backend.main:app` (from the repo root) with the repo's
// .venv on activation and kills it on deactivate. A backend that is already answering on the
// configured URL (e.g. started with `make backend`) is reused and never killed.

const STARTUP_TIMEOUT_MS = 60_000;
const STOP_GRACE_MS = 3_000;

export type BackendState = 'stopped' | 'starting' | 'running' | 'external' | 'failed';

/** How far along backend startup is, for the sidebar's progress bar. */
export interface BackendProgress {
    percent: number; // 0..100
    step: string;
}

// Uvicorn logs these lines (on stderr) as it boots; each one advances the progress bar.
const STARTUP_MILESTONES: [RegExp, number, string][] = [
    [/Started server process/i, 55, 'Server process started'],
    [/Waiting for application startup/i, 70, 'Loading the application'],
    [/Application startup complete/i, 90, 'Application ready'],
    [/Uvicorn running on/i, 95, 'Waiting for the health check'],
];

export class BackendManager implements vscode.Disposable {
    private _proc?: ChildProcess;
    private _starting?: Promise<void>;
    private _state: BackendState = 'stopped';
    private _lastError?: string;
    private _stderrTail: string[] = [];
    private _stopping = false;
    private _progress: BackendProgress = { percent: 0, step: '' };
    private readonly _onDidChangeState = new vscode.EventEmitter<BackendState>();
    readonly onDidChangeState = this._onDidChangeState.event;
    private readonly _onDidChangeProgress = new vscode.EventEmitter<BackendProgress>();
    readonly onDidChangeProgress = this._onDidChangeProgress.event;

    constructor(
        private readonly _extensionPath: string,
        private readonly _output: vscode.OutputChannel,
        private readonly _apiUrl: () => string
    ) {}

    get state(): BackendState { return this._state; }
    get lastError(): string | undefined { return this._lastError; }
    get progress(): BackendProgress { return this._progress; }

    /** Resolves once the backend answers /health; starts it if needed. Never rejects. */
    ensureRunning(): Promise<void> {
        if (this._starting) {
            return this._starting;
        }
        this._starting = this._ensure().finally(() => { this._starting = undefined; });
        return this._starting;
    }

    async restart(): Promise<void> {
        await this.stop();
        this._lastError = undefined;
        await this.ensureRunning();
    }

    /** Kills the process we spawned (SIGTERM, then SIGKILL after a grace period). */
    async stop(): Promise<void> {
        const proc = this._proc;
        if (!proc || proc.exitCode !== null) {
            return;
        }
        this._output.appendLine('[tokenguard] stopping backend…');
        this._stopping = true;
        this._setProgress(0, '');
        const exited = new Promise<void>(resolve => proc.once('exit', () => resolve()));
        proc.kill('SIGTERM');
        const timer = setTimeout(() => proc.kill('SIGKILL'), STOP_GRACE_MS);
        await exited;
        clearTimeout(timer);
        this._stopping = false;
    }

    /** Synchronous best-effort kill for the process 'exit' event. */
    killSync(): void {
        const pid = this._proc?.pid;
        if (!pid || this._proc?.exitCode !== null) {
            return;
        }
        if (process.platform === 'win32') {
            try {
                spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
            } catch {
                // ignore
            }
        } else {
            try {
                process.kill(pid, 'SIGKILL');
            } catch {
                // already gone
            }
        }
    }

    dispose(): void {
        void this.stop();
        this._onDidChangeState.dispose();
        this._onDidChangeProgress.dispose();
    }

    private _setState(state: BackendState, error?: string) {
        this._state = state;
        this._lastError = error;
        if (error) {
            this._output.appendLine(`[tokenguard] ${error}`);
        }
        this._onDidChangeState.fire(state);
    }

    /** The bar never moves backwards (except an explicit reset to 0 on stop/launch). */
    private _setProgress(percent: number, step: string) {
        if (percent !== 0 && percent < this._progress.percent) {
            return;
        }
        if (percent === this._progress.percent && step === this._progress.step) {
            return;
        }
        this._progress = { percent, step };
        this._onDidChangeProgress.fire(this._progress);
    }

    /** Advance the bar when a uvicorn boot milestone scrolls by on stdout/stderr. */
    private _observeOutput(text: string) {
        for (const [pattern, percent, step] of STARTUP_MILESTONES) {
            if (pattern.test(text)) {
                this._setProgress(percent, step);
            }
        }
    }

    private async _ensure(): Promise<void> {
        if (await this._healthy()) {
            if (!this._proc) {
                this._setState('external');
            }
            return;
        }
        if (this._proc && this._proc.exitCode === null) {
            // Our process is alive but not answering yet (still booting): just wait for it.
            await this._waitHealthy(this._proc);
            return;
        }

        const cfg = vscode.workspace.getConfiguration('tokenguard');
        this._setProgress(5, 'Checking configuration');
        if (!cfg.get<boolean>('autoStartBackend', true)) {
            this._setState('stopped', 'auto-start disabled (tokenguard.autoStartBackend)');
            return;
        }
        const url = new URL(this._apiUrl());
        if (!['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) {
            this._setState('stopped', `not auto-starting: ${url.origin} is not a local URL`);
            return;
        }

        const backendDir = this._backendDir();
        if (!backendDir) {
            this._setState('failed', 'backend folder not found. Set tokenguard.backendPath to the backend/ folder.');
            return;
        }
        this._setProgress(20, 'Backend found');
        // backend/ and compression/ are sibling packages, so uvicorn runs from the repo root
        const repoRoot = path.dirname(backendDir);
        const python = this._python(repoRoot);
        if (!python) {
            this._setState('failed', `no Python venv found in ${repoRoot}. Run \`make setup\` in the repo root.`);
            void this._offerSetup(repoRoot);
            return;
        }
        this._setProgress(30, 'Python environment found');

        const port = url.port || (url.protocol === 'https:' ? '443' : '80');
        const args = ['-m', 'uvicorn', `${path.basename(backendDir)}.main:app`, '--host', '127.0.0.1', '--port', port];
        this._output.appendLine(`[tokenguard] starting: ${python} ${args.join(' ')}  (cwd ${repoRoot})`);
        this._setState('starting');
        this._setProgress(40, 'Launching the server');
        this._stderrTail = [];

        const proc = spawn(python, args, {
            cwd: repoRoot,
            env: { ...process.env, PYTHONUNBUFFERED: '1' }
        });
        this._proc = proc;
        proc.stdout?.on('data', d => {
            const text = d.toString();
            this._output.append(text);
            this._observeOutput(text);
        });
        proc.stderr?.on('data', d => {
            const text = d.toString();
            this._output.append(text);
            this._observeOutput(text);
            this._stderrTail = [...this._stderrTail, ...text.split('\n').filter(Boolean)].slice(-5);
        });
        proc.on('error', err => this._output.appendLine(`[tokenguard] spawn failed: ${err.message}`));
        proc.on('exit', (code, signal) => {
            this._output.appendLine(`[tokenguard] backend exited (code ${code}, signal ${signal})`);
            if (this._proc === proc) {
                this._proc = undefined;
                if (this._stopping) {
                    this._setState('stopped');
                } else {
                    this._setState('failed', `backend exited with code ${code}: ` +
                        (this._stderrTail.join(' | ') || 'see Output > LocalPilot Backend'));
                }
            }
        });

        await this._waitHealthy(proc);
    }

    private async _waitHealthy(proc: ChildProcess): Promise<void> {
        const deadline = Date.now() + STARTUP_TIMEOUT_MS;
        while (Date.now() < deadline) {
            if (proc.exitCode !== null || this._proc !== proc) {
                return; // exit handler already reported the failure
            }
            if (await this._healthy()) {
                this._output.appendLine(`[tokenguard] backend ready at ${this._apiUrl()}`);
                this._setProgress(100, 'Ready');
                this._setState('running');
                return;
            }
            await new Promise(r => setTimeout(r, 500));
        }
        this._setState('failed', `backend did not answer ${this._apiUrl()}/health within ${STARTUP_TIMEOUT_MS / 1000}s`);
    }

    private async _healthy(): Promise<boolean> {
        try {
            const resp = await fetch(`${this._apiUrl()}/health`, { signal: AbortSignal.timeout(2_000) });
            return resp.ok;
        } catch {
            return false;
        }
    }

    /** tokenguard.backendPath, else the backend/ folder next to this extension (repo layout). */
    private _backendDir(): string | undefined {
        const configured = vscode.workspace.getConfiguration('tokenguard').get<string>('backendPath');
        const candidates = configured
            ? [configured]
            : [path.resolve(this._extensionPath, '..', 'backend')];
        return candidates.find(dir => fs.existsSync(path.join(dir, 'main.py')));
    }

    /** tokenguard.pythonPath, else <repo>/.venv. */
    private _python(repoRoot: string): string | undefined {
        const configured = vscode.workspace.getConfiguration('tokenguard').get<string>('pythonPath');
        if (configured) {
            return configured;
        }
        const bin = process.platform === 'win32' ? path.join('Scripts', 'python.exe') : path.join('bin', 'python');
        const python = path.join(repoRoot, '.venv', bin);
        return fs.existsSync(python) ? python : undefined;
    }

    private async _offerSetup(repoRoot: string) {
        const choice = await vscode.window.showErrorMessage(
            'LocalPilot: the Python environment is missing, so the backend cannot start.',
            'Run setup'
        );
        if (choice === 'Run setup') {
            const term = vscode.window.createTerminal({ name: 'TokenGuard setup', cwd: repoRoot });
            term.show();
            term.sendText('make setup');
        }
    }
}
