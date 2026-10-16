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
exports.BackendManager = void 0;
const vscode = __importStar(require("vscode"));
const child_process_1 = require("child_process");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
// Owns the LocalPilot backend process: spawns `python -m uvicorn backend.main:app` (from the repo root) with the repo's
// .venv on activation and kills it on deactivate. A backend that is already answering on the
// configured URL (e.g. started with `make backend`) is reused and never killed.
const STARTUP_TIMEOUT_MS = 60_000;
const STOP_GRACE_MS = 3_000;
// Uvicorn logs these lines (on stderr) as it boots; each one advances the progress bar.
const STARTUP_MILESTONES = [
    [/Started server process/i, 55, 'Server process started'],
    [/Waiting for application startup/i, 70, 'Loading the application'],
    [/Application startup complete/i, 90, 'Application ready'],
    [/Uvicorn running on/i, 95, 'Waiting for the health check'],
];
class BackendManager {
    _extensionPath;
    _output;
    _apiUrl;
    _proc;
    _starting;
    _state = 'stopped';
    _lastError;
    _stderrTail = [];
    _stopping = false;
    _progress = { percent: 0, step: '' };
    _onDidChangeState = new vscode.EventEmitter();
    onDidChangeState = this._onDidChangeState.event;
    _onDidChangeProgress = new vscode.EventEmitter();
    onDidChangeProgress = this._onDidChangeProgress.event;
    constructor(_extensionPath, _output, _apiUrl) {
        this._extensionPath = _extensionPath;
        this._output = _output;
        this._apiUrl = _apiUrl;
    }
    get state() { return this._state; }
    get lastError() { return this._lastError; }
    get progress() { return this._progress; }
    /** Resolves once the backend answers /health; starts it if needed. Never rejects. */
    ensureRunning() {
        if (this._starting) {
            return this._starting;
        }
        this._starting = this._ensure().finally(() => { this._starting = undefined; });
        return this._starting;
    }
    async restart() {
        await this.stop();
        this._lastError = undefined;
        await this.ensureRunning();
    }
    /** Kills the process we spawned (SIGTERM, then SIGKILL after a grace period). */
    async stop() {
        const proc = this._proc;
        if (!proc || proc.exitCode !== null) {
            return;
        }
        this._output.appendLine('[tokenguard] stopping backend…');
        this._stopping = true;
        this._setProgress(0, '');
        const exited = new Promise(resolve => proc.once('exit', () => resolve()));
        proc.kill('SIGTERM');
        const timer = setTimeout(() => proc.kill('SIGKILL'), STOP_GRACE_MS);
        await exited;
        clearTimeout(timer);
        this._stopping = false;
    }
    /** Synchronous best-effort kill for the process 'exit' event. */
    killSync() {
        const pid = this._proc?.pid;
        if (!pid || this._proc?.exitCode !== null) {
            return;
        }
        if (process.platform === 'win32') {
            try {
                (0, child_process_1.spawn)('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
            }
            catch {
                // ignore
            }
        }
        else {
            try {
                process.kill(pid, 'SIGKILL');
            }
            catch {
                // already gone
            }
        }
    }
    dispose() {
        void this.stop();
        this._onDidChangeState.dispose();
        this._onDidChangeProgress.dispose();
    }
    _setState(state, error) {
        this._state = state;
        this._lastError = error;
        if (error) {
            this._output.appendLine(`[tokenguard] ${error}`);
        }
        this._onDidChangeState.fire(state);
    }
    /** The bar never moves backwards (except an explicit reset to 0 on stop/launch). */
    _setProgress(percent, step) {
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
    _observeOutput(text) {
        for (const [pattern, percent, step] of STARTUP_MILESTONES) {
            if (pattern.test(text)) {
                this._setProgress(percent, step);
            }
        }
    }
    async _ensure() {
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
        if (!cfg.get('autoStartBackend', true)) {
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
        const proc = (0, child_process_1.spawn)(python, args, {
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
                }
                else {
                    this._setState('failed', `backend exited with code ${code}: ` +
                        (this._stderrTail.join(' | ') || 'see Output > LocalPilot Backend'));
                }
            }
        });
        await this._waitHealthy(proc);
    }
    async _waitHealthy(proc) {
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
    async _healthy() {
        try {
            const resp = await fetch(`${this._apiUrl()}/health`, { signal: AbortSignal.timeout(2_000) });
            return resp.ok;
        }
        catch {
            return false;
        }
    }
    /** tokenguard.backendPath, else the backend/ folder next to this extension (repo layout). */
    _backendDir() {
        const configured = vscode.workspace.getConfiguration('tokenguard').get('backendPath');
        const candidates = configured
            ? [configured]
            : [path.resolve(this._extensionPath, '..', 'backend')];
        return candidates.find(dir => fs.existsSync(path.join(dir, 'main.py')));
    }
    /** tokenguard.pythonPath, else <repo>/.venv. */
    _python(repoRoot) {
        const configured = vscode.workspace.getConfiguration('tokenguard').get('pythonPath');
        if (configured) {
            return configured;
        }
        const bin = process.platform === 'win32' ? path.join('Scripts', 'python.exe') : path.join('bin', 'python');
        const python = path.join(repoRoot, '.venv', bin);
        return fs.existsSync(python) ? python : undefined;
    }
    async _offerSetup(repoRoot) {
        const choice = await vscode.window.showErrorMessage('LocalPilot: the Python environment is missing, so the backend cannot start.', 'Run setup');
        if (choice === 'Run setup') {
            const term = vscode.window.createTerminal({ name: 'TokenGuard setup', cwd: repoRoot });
            term.show();
            term.sendText('make setup');
        }
    }
}
exports.BackendManager = BackendManager;
//# sourceMappingURL=backend.js.map