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
exports.OllamaManager = void 0;
const vscode = __importStar(require("vscode"));
const child_process_1 = require("child_process");
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
// Owns the Ollama lifecycle for the extension:
//   - starts `ollama serve` if it isn't already reachable (and remembers that it did),
//   - frees model memory on stop (POST /api/generate {keep_alive: 0} for every running model),
//   - stops the `ollama serve` it started on deactivate / process exit.
// An Ollama that was already running (e.g. a systemd service) is reused and NEVER killed, so we
// don't fight the user's own setup. `setup.sh` records a PID in .ollama.pid when it starts one.
const STARTUP_TIMEOUT_MS = 30_000;
const STOP_GRACE_MS = 3_000;
class OllamaManager {
    _output;
    _url;
    _proc;
    _starting;
    _state = 'stopped';
    _lastError;
    _stopping = false;
    _markerPath;
    _onDidChangeState = new vscode.EventEmitter();
    onDidChangeState = this._onDidChangeState.event;
    constructor(extensionPath, _output, _url) {
        this._output = _output;
        this._url = _url;
        this._markerPath = path.resolve(extensionPath, '..', '.ollama.pid');
    }
    get state() { return this._state; }
    get lastError() { return this._lastError; }
    /** Resolves once Ollama answers /api/tags; starts it if needed. Never rejects. */
    ensureRunning() {
        if (this._starting) {
            return this._starting;
        }
        this._starting = this._ensure().finally(() => { this._starting = undefined; });
        return this._starting;
    }
    /** Unload every running model now (frees RAM/VRAM). Returns the model names. */
    async unloadModels() {
        const base = this._url().replace(/\/+$/, '');
        let names = [];
        try {
            const resp = await fetch(`${base}/api/ps`, { signal: AbortSignal.timeout(3_000) });
            if (!resp.ok) {
                return [];
            }
            const data = await resp.json();
            names = (data.models || []).map(m => m.name || m.model).filter((n) => !!n);
        }
        catch {
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
    async stop() {
        await this.unloadModels();
        if (!vscode.workspace.getConfiguration('tokenguard').get('stopOllamaOnExit', true)) {
            return;
        }
        const pid = this._ownedPid();
        if (!pid || !this._alive(pid) || !this._looksLikeOllama(pid)) {
            return;
        }
        this._output.appendLine(`[tokenguard] stopping Ollama (pid ${pid})…`);
        this._stopping = true;
        const exited = this._proc && this._proc.pid === pid
            ? new Promise(resolve => this._proc.once('exit', () => resolve()))
            : Promise.resolve();
        this._killTree(pid, 'SIGTERM');
        const timer = setTimeout(() => this._killTree(pid, 'SIGKILL'), STOP_GRACE_MS);
        await exited;
        clearTimeout(timer);
        this._stopping = false;
        this._clearMarker();
    }
    /** Synchronous best-effort cleanup for the process 'exit' event (kill our children). */
    killSync() {
        if (this._proc?.pid) {
            this._killTree(this._proc.pid, 'SIGKILL');
        }
        const pid = this._readMarker();
        if (pid && pid !== this._proc?.pid && this._looksLikeOllama(pid)) {
            this._killTree(pid, 'SIGKILL');
        }
    }
    dispose() {
        void this.stop();
        this._onDidChangeState.dispose();
    }
    _setState(state, error) {
        this._state = state;
        this._lastError = error;
        this._onDidChangeState.fire(state);
    }
    async _ensure() {
        if (await this._healthy()) {
            this._setState('external'); // started elsewhere (e.g. systemd): leave it alone
            return;
        }
        if (this._proc && this._proc.exitCode === null) {
            await this._waitHealthy();
            return;
        }
        if (!vscode.workspace.getConfiguration('tokenguard').get('autoStartOllama', true)) {
            this._setState('stopped', 'auto-start disabled (tokenguard.autoStartOllama)');
            return;
        }
        if (!this._hasCli()) {
            this._setState('failed', 'the `ollama` CLI was not found on PATH');
            return;
        }
        this._output.appendLine('[tokenguard] starting ollama serve…');
        this._setState('starting');
        const proc = (0, child_process_1.spawn)('ollama', ['serve'], {
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
                this._setState(this._stopping ? 'stopped' : 'failed', this._stopping ? undefined : `ollama exited (code ${code}, signal ${signal})`);
            }
        });
        await this._waitHealthy();
    }
    async _waitHealthy() {
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
    async _healthy() {
        try {
            const resp = await fetch(`${this._url().replace(/\/+$/, '')}/api/tags`, { signal: AbortSignal.timeout(2_000) });
            return resp.ok;
        }
        catch {
            return false;
        }
    }
    _hasCli() {
        const cmd = process.platform === 'win32' ? 'where' : 'which';
        try {
            return (0, child_process_1.spawnSync)(cmd, ['ollama'], { windowsHide: true }).status === 0;
        }
        catch {
            return false;
        }
    }
    /** The PID of the Ollama we should stop: our child, else the setup.sh marker. */
    _ownedPid() {
        if (this._proc?.pid && this._proc.exitCode === null) {
            return this._proc.pid;
        }
        return this._readMarker();
    }
    _readMarker() {
        try {
            const pid = Number(fs.readFileSync(this._markerPath, 'utf8').trim());
            return Number.isInteger(pid) && pid > 0 ? pid : undefined;
        }
        catch {
            return undefined;
        }
    }
    _clearMarker() {
        try {
            fs.rmSync(this._markerPath, { force: true });
        }
        catch {
            // ignore
        }
    }
    _alive(pid) {
        try {
            process.kill(pid, 0);
            return true;
        }
        catch {
            return false;
        }
    }
    /** Guard against killing an unrelated process that reused the marker PID (Linux). */
    _looksLikeOllama(pid) {
        if (process.platform !== 'linux') {
            return true;
        }
        try {
            return fs.readFileSync(`/proc/${pid}/comm`, 'utf8').trim().includes('ollama');
        }
        catch {
            return false;
        }
    }
    _killPid(pid, signal) {
        if (process.platform === 'win32') {
            try {
                (0, child_process_1.spawn)('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
            }
            catch {
                // ignore
            }
            return;
        }
        try {
            process.kill(pid, signal);
        }
        catch {
            // already gone
        }
    }
    _killTree(pid, signal) {
        if (process.platform === 'win32') {
            this._killPid(pid, signal);
            return;
        }
        try {
            process.kill(-pid, signal); // negative PID = the whole process group
        }
        catch {
            this._killPid(pid, signal);
        }
    }
}
exports.OllamaManager = OllamaManager;
//# sourceMappingURL=ollama.js.map