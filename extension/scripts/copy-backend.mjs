// Copies the Python gateway (../backend and ../compression, .py files only) into python/ so the
// published VSIX contains it. Runs on `vscode:prepublish`. python/ is generated, not committed.
// The extension installs python/requirements.txt into its own venv on first run (see src/backend.ts).
import { copyFileSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const repo = join(root, '..');
const out = join(root, 'python');

function copyPython(from, to) {
    for (const name of readdirSync(from)) {
        const src = join(from, name);
        if (statSync(src).isDirectory()) {
            if (name !== '__pycache__') {
                copyPython(src, join(to, name));
            }
        } else if (name.endsWith('.py')) {
            mkdirSync(to, { recursive: true });
            copyFileSync(src, join(to, name));
        }
    }
}

rmSync(out, { recursive: true, force: true });
for (const pkg of ['backend', 'compression']) {
    copyPython(join(repo, pkg), join(out, pkg));
}
copyFileSync(join(root, 'requirements.runtime.txt'), join(out, 'requirements.txt'));
console.log(`backend: copied backend/ and compression/ into ${out}`);
