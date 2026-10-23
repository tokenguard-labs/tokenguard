import * as vscode from 'vscode';
import { ChildProcess, execFile, spawn } from 'child_process';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as net from 'net';
import * as path from 'path';

// Owns the LocalPilot backend process: spawns `python -m uvicorn backend.main:app` the first time
// the chat asks for it and kills it on deactivate. A backend that is already answering on the
// configured URL (e.g. started with `make backend`) is reused and never killed.
//
// Each spawned session gets a random token (TOKENGUARD_SESSION_TOKEN env var) that is sent with
// every request; without it `make backend` runs in loopback-only mode. Auto-started backends listen
// on a free port so they don't clash with dev servers on 8000.
//
// Where the backend comes from, first match wins:
//   1. tokenguard.backendPath (+ tokenguard.pythonPath or <repo>/.venv): development override.
//   2. ../backend next to this extension (repo checkout / Extension Development Host) + <repo>/.venv.
//   3. python/ inside the installed extension (copied by scripts/copy-backend.mjs at package time),
//      run with a venv the extension creates in its global storage on first run
//      (or tokenguard.pythonPath, if set). The venv is built from a Python 3.9+ found on the
//      machine, or, when there is none, from a standalone CPython downloaded into global storage.

const STARTUP_TIMEOUT_MS = 60_000;
const STOP_GRACE_MS = 3_000;
const MIN_PYTHON: [number, number] = [3, 9];

// Standalone CPython (github.com/astral-sh/python-build-standalone), used when the machine has no
// usable Python. Pinned build; the digests come from the release's asset list.
const STANDALONE_PYTHON_RELEASE = '20260929';
const STANDALONE_PYTHON_VERSION = '3.12.14';
const STANDALONE_PYTHON_SHA256: Record<string, string> = {
    'x86_64-unknown-linux-gnu': '06c90b93f419b63371c18f20fed0558a1a901f6518c3c24f755077e048447e7f',
    'aarch64-unknown-linux-gnu': '9c797cf657f6dced51d3e74eeabd7c1ca742d5bf10f66080a290e424ced8edaf',
    'x86_64-apple-darwin': 'f51ec8a7fa0ede129a5e2e942a2a453ba4830adaca2289da4449390e42443292',
    'aarch64-apple-darwin': 'de6b8f94fa765639b423ea353ab340669704c7186f96ee3cab389dcfde770c3c',
    'x86_64-pc-windows-msvc': '28728baf30b65e263f0b25c5a85be8226e7ab0d212fbadd6a8f0f796139fa804',
    'aarch64-pc-windows-msvc': '96b4c8cb02ad37d97141035530fa63cac3ff2690a9cfdfd343290c0c94227ea2',
};

interface PythonCommand {
    command: string;
    args: string[];
}

interface Runtime {
    /** Folder containing the backend/ and compression/ packages; uvicorn runs from here. */
    root: string;
    python: string;
    /** The backend package's folder name under `root` (normally "backend"). */
    module: string;
}

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
    // Random token for this VS Code session, required by the spawned backend on every request.
    private readonly _sessionToken = crypto.randomBytes(32).toString('hex');
    // Base URL of the backend we spawned (with its auto-picked port); undefined until/unless we own one.
    private _baseUrl?: string;
    private readonly _onDidChangeState = new vscode.EventEmitter<BackendState>();
    readonly onDidChangeState = this._onDidChangeState.event;
    private readonly _onDidChangeProgress = new vscode.EventEmitter<BackendProgress>();
    readonly onDidChangeProgress = this._onDidChangeProgress.event;

    constructor(
        private readonly _extensionPath: string,
        private readonly _storagePath: string,
        private readonly _output: vscode.OutputChannel,
        private readonly _apiUrl: () => string
    ) {}

    get state(): BackendState { return this._state; }
    get lastError(): string | undefined { return this._lastError; }
    get progress(): BackendProgress { return this._progress; }
    /** http://127.0.0.1:<free port> of the backend we spawned, once known. */
    get baseUrl(): string | undefined { return this._baseUrl; }
    get sessionToken(): string { return this._sessionToken; }

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

        const runtime = await this._runtime();
        if (!runtime) {
            return; // _runtime() already reported why
        }
        this._setProgress(30, 'Python environment found');

        // Auto-start always binds a free port, so it never clashes with dev servers (or the
        // port a manual `make backend` already holds). tokenguard.apiUrl is only the fallback
        // address for finding an externally started backend.
        this._baseUrl = await this._pickPort();
        const port = new URL(this._baseUrl).port;
        const { root, python, module } = runtime;
        const args = ['-m', 'uvicorn', `${module}.main:app`, '--host', '127.0.0.1', '--port', port];
        this._output.appendLine(`[tokenguard] starting: ${python} ${args.join(' ')}  (cwd ${root})`);
        this._setState('starting');
        this._setProgress(40, 'Launching the server');
        this._stderrTail = [];

        // The backend must talk to the same Ollama as the sidebar: pass an explicitly set
        // tokenguard.ollamaUrl along (otherwise OLLAMA_URL from backend/.env or its default applies).
        const ollamaSetting = vscode.workspace.getConfiguration('tokenguard').inspect<string>('ollamaUrl');
        const ollamaUrl = (ollamaSetting?.workspaceFolderValue ?? ollamaSetting?.workspaceValue
            ?? ollamaSetting?.globalValue ?? '').trim();
        const proc = spawn(python, args, {
            cwd: root,
            env: {
                ...process.env,
                PYTHONUNBUFFERED: '1',
                // Windows otherwise decodes the backend's output with the ANSI code page.
                PYTHONIOENCODING: 'utf-8',
                TOKENGUARD_SESSION_TOKEN: this._sessionToken,
                ...(ollamaUrl ? { OLLAMA_URL: ollamaUrl } : {})
            }
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
                this._baseUrl = undefined;
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
            if (await this._healthy(this._baseUrl ?? this._apiUrl())) {
                this._output.appendLine(`[tokenguard] backend ready at ${this._baseUrl ?? this._apiUrl()}`);
                this._setProgress(100, 'Ready');
                this._setState('running');
                return;
            }
            await new Promise(r => setTimeout(r, 500));
        }
        this._setState('failed', `backend did not answer ${this._baseUrl ?? this._apiUrl()}/health within ${STARTUP_TIMEOUT_MS / 1000}s`);
    }

    private async _healthy(url = this._apiUrl()): Promise<boolean> {
        try {
            const resp = await fetch(`${url}/health`, {
                headers: { 'X-Token-Guard-Session': this._sessionToken },
                signal: AbortSignal.timeout(2_000)
            });
            return resp.ok;
        } catch {
            return false;
        }
    }

    /** Picks the backend source and interpreter (see the header comment). Reports failures itself. */
    private async _runtime(): Promise<Runtime | undefined> {
        const cfg = vscode.workspace.getConfiguration('tokenguard');
        const configuredDir = cfg.get<string>('backendPath');
        const configuredPython = cfg.get<string>('pythonPath') || undefined;
        const hasMain = (dir: string) => fs.existsSync(path.join(dir, 'main.py'));

        // 1 + 2: a source checkout, run with the repo's .venv (created by `make setup`).
        const devDir = configuredDir || path.resolve(this._extensionPath, '..', 'backend');
        if (configuredDir && !hasMain(configuredDir)) {
            this._setState('failed', `tokenguard.backendPath has no main.py: ${configuredDir}`);
            return undefined;
        }
        if (hasMain(devDir)) {
            // backend/ and compression/ are sibling packages, so uvicorn runs from the repo root
            const root = path.dirname(devDir);
            const python = configuredPython ?? this._repoPython(root);
            if (!python) {
                this._setState('failed', `no Python venv found in ${root}. Run \`make setup\` in the repo root.`);
                void this._offerSetup(root);
                return undefined;
            }
            this._setProgress(20, 'Backend found');
            return { root, python, module: path.basename(devDir) };
        }

        // 3: the copy bundled into the VSIX, with the extension's own venv.
        const root = path.join(this._extensionPath, 'python');
        if (!hasMain(path.join(root, 'backend'))) {
            this._setState('failed', 'backend not found in the extension package. Reinstall TokenGuard, ' +
                'or set tokenguard.backendPath to a backend/ folder.');
            return undefined;
        }
        this._setProgress(20, 'Backend found');
        const python = configuredPython ?? await this._ensureVenv(path.join(root, 'requirements.txt'));
        return python ? { root, python, module: 'backend' } : undefined;
    }

    /** Ask the OS for a free TCP port (bind port 0, then release it). */
    private _pickPort(): Promise<string> {
        return new Promise((resolve, reject) => {
            const server = net.createServer();
            server.listen(0, '127.0.0.1', () => {
                const port = (server.address() as net.AddressInfo).port;
                server.close(() => resolve(`http://127.0.0.1:${port}`));
            });
            server.on('error', reject);
        });
    }

    /** <repo>/.venv's interpreter, if it exists. */
    private _repoPython(repoRoot: string): string | undefined {
        const python = venvPython(path.join(repoRoot, '.venv'));
        return fs.existsSync(python) ? python : undefined;
    }

    /**
     * Creates (or updates) <globalStorage>/venv and installs the bundled requirements into it.
     * Re-installs only when requirements.txt changed since the last successful install.
     */
    private async _ensureVenv(requirements: string): Promise<string | undefined> {
        const venvDir = path.join(this._storagePath, 'venv');
        const stampFile = path.join(this._storagePath, 'venv.stamp');
        const python = venvPython(venvDir);
        const wanted = crypto.createHash('sha256').update(fs.readFileSync(requirements)).digest('hex');
        const stamp = fs.existsSync(stampFile) ? fs.readFileSync(stampFile, 'utf8').trim() : '';
        if (fs.existsSync(python) && stamp === wanted) {
            return python;
        }

        this._setState('starting');
        this._output.appendLine(`[tokenguard] setting up the backend environment in ${venvDir} (one time)`);
        const ok = await vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'LocalPilot: setting up the backend (first run only)'
        }, async progress => {
            fs.mkdirSync(this._storagePath, { recursive: true });
            if (!fs.existsSync(python)) {
                progress.report({ message: 'looking for Python…' });
                const base = await findSystemPython() ?? await this._standalonePython(progress);
                if (!base) {
                    return false;
                }
                progress.report({ message: 'creating the virtual environment…' });
                fs.rmSync(venvDir, { recursive: true, force: true }); // a half-created venv from a failed run
                if (await this._run(base.command, [...base.args, '-m', 'venv', venvDir]) !== 0) {
                    return false;
                }
            }
            progress.report({ message: 'installing dependencies…' });
            return await this._run(python, [
                '-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', '-r', requirements
            ]) === 0;
        });
        if (!ok) {
            this._setState('failed', 'could not set up the backend environment (see Output > LocalPilot Backend). ' +
                'Check your internet connection and run "TokenGuard: Restart Backend", ' +
                'or set tokenguard.pythonPath to a Python that has the backend dependencies installed.');
            return undefined;
        }
        fs.writeFileSync(stampFile, wanted);
        return python;
    }

    /**
     * Downloads a standalone CPython into <globalStorage>/python (once) and returns it. Used only
     * when the machine has no usable Python. The archive is checked against a pinned SHA-256.
     */
    private async _standalonePython(
        progress: vscode.Progress<{ message?: string }>
    ): Promise<PythonCommand | undefined> {
        const installDir = path.join(this._storagePath, 'python');
        const exe = process.platform === 'win32'
            ? path.join(installDir, 'python.exe')
            : path.join(installDir, 'bin', 'python3');
        if (fs.existsSync(exe)) {
            return { command: exe, args: [] };
        }

        const triple = standaloneTriple();
        const sha256 = triple && STANDALONE_PYTHON_SHA256[triple];
        if (!triple || !sha256) {
            this._output.appendLine(`[tokenguard] no Python found, and no standalone Python build exists for ` +
                `${process.platform}-${process.arch}. Install Python ${MIN_PYTHON.join('.')}+ or set tokenguard.pythonPath.`);
            return undefined;
        }
        const name = `cpython-${STANDALONE_PYTHON_VERSION}+${STANDALONE_PYTHON_RELEASE}-${triple}-install_only.tar.gz`;
        const url = 'https://github.com/astral-sh/python-build-standalone/releases/download/' +
            `${STANDALONE_PYTHON_RELEASE}/${encodeURIComponent(name)}`;
        const archive = path.join(this._storagePath, name);
        progress.report({ message: `downloading Python ${STANDALONE_PYTHON_VERSION}…` });
        this._output.appendLine(`[tokenguard] no Python ${MIN_PYTHON.join('.')}+ found; downloading ${url}`);
        try {
            const resp = await fetch(url, { signal: AbortSignal.timeout(10 * 60_000) });
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            const data = Buffer.from(await resp.arrayBuffer());
            const actual = crypto.createHash('sha256').update(data).digest('hex');
            if (actual !== sha256) {
                throw new Error(`checksum mismatch (expected ${sha256}, got ${actual})`);
            }
            fs.writeFileSync(archive, data);
        } catch (err) {
            this._output.appendLine(`[tokenguard] Python download failed: ${err instanceof Error ? err.message : err}`);
            return undefined;
        }

        progress.report({ message: `unpacking Python ${STANDALONE_PYTHON_VERSION}…` });
        // The archive holds a top-level python/ folder. tar ships with macOS, Linux and Windows 10+.
        fs.rmSync(installDir, { recursive: true, force: true });
        const code = await this._run('tar', ['-xzf', archive, '-C', this._storagePath]);
        fs.rmSync(archive, { force: true });
        if (code !== 0 || !fs.existsSync(exe)) {
            fs.rmSync(installDir, { recursive: true, force: true });
            this._output.appendLine('[tokenguard] could not unpack the downloaded Python');
            return undefined;
        }
        return { command: exe, args: [] };
    }

    /** Runs a setup command, streaming its output to the channel. Resolves to the exit code. */
    private _run(command: string, args: string[]): Promise<number | null> {
        this._output.appendLine(`[tokenguard] $ ${command} ${args.join(' ')}`);
        return new Promise(resolve => {
            const proc = spawn(command, args, { windowsHide: true });
            proc.stdout?.on('data', d => this._output.append(d.toString()));
            proc.stderr?.on('data', d => this._output.append(d.toString()));
            proc.on('error', err => {
                this._output.appendLine(`[tokenguard] ${err.message}`);
                resolve(null);
            });
            proc.on('exit', code => resolve(code));
        });
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

function venvPython(venvDir: string): string {
    return process.platform === 'win32'
        ? path.join(venvDir, 'Scripts', 'python.exe')
        : path.join(venvDir, 'bin', 'python');
}

/**
 * The first Python >= MIN_PYTHON that can create virtual environments: PATH first
 * (`py -3` on Windows, then python3 / python), then the usual install locations.
 */
async function findSystemPython(): Promise<PythonCommand | undefined> {
    for (const candidate of pythonCandidates()) {
        // `import ensurepip` fails on Debian/Ubuntu without python3-venv, where `-m venv` would too.
        const version = await new Promise<string | undefined>(resolve => {
            execFile(candidate.command, [...candidate.args, '-c',
                'import sys, venv, ensurepip; print("%d.%d" % sys.version_info[:2])'],
            { timeout: 10_000, windowsHide: true },
            (err, stdout) => resolve(err ? undefined : stdout.trim()));
        });
        const [major, minor] = (version ?? '').split('.').map(Number);
        if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
            return candidate;
        }
    }
    return undefined;
}

function pythonCandidates(): PythonCommand[] {
    const onPath: PythonCommand[] = process.platform === 'win32'
        ? [{ command: 'py', args: ['-3'] }, { command: 'python', args: [] }]
        : [{ command: 'python3', args: [] }, { command: 'python', args: [] }];
    const files: string[] = [];
    if (process.platform === 'win32') {
        // python.org per-user and all-users installs, newest first
        const roots = [
            process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Programs', 'Python'),
            process.env.ProgramFiles,
            process.env['ProgramFiles(x86)'],
        ].filter((r): r is string => !!r);
        for (const root of roots) {
            let dirs: string[] = [];
            try {
                dirs = fs.readdirSync(root).filter(d => /^Python3\d+/i.test(d));
            } catch {
                continue;
            }
            dirs.sort((a, b) => Number(b.replace(/\D/g, '')) - Number(a.replace(/\D/g, '')));
            files.push(...dirs.map(d => path.join(root, d, 'python.exe')));
        }
    } else {
        files.push('/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3',
            '/Library/Frameworks/Python.framework/Versions/Current/bin/python3');
    }
    return [...onPath, ...files.filter(f => fs.existsSync(f)).map(command => ({ command, args: [] }))];
}

/** python-build-standalone's target name for this machine, if it publishes one. */
function standaloneTriple(): string | undefined {
    const arch = process.arch === 'x64' ? 'x86_64' : process.arch === 'arm64' ? 'aarch64' : undefined;
    if (!arch) {
        return undefined;
    }
    switch (process.platform) {
        case 'linux': return `${arch}-unknown-linux-gnu`;
        case 'darwin': return `${arch}-apple-darwin`;
        case 'win32': return `${arch}-pc-windows-msvc`;
        default: return undefined;
    }
}
