import * as assert from 'assert';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { InlineDiffManager } from '../inlineDiff';
import { applyChange, currentText } from '../chatView';

// Temporary folder inside the test workspace for new-file flows.
const dir = path.join(__dirname, '..', '..', 'tmp-agent-accept-tests');
const stamp = String(Date.now()) + Math.floor(Math.random() * 1000);

let mgr: InlineDiffManager;

suite('Agent accept (new files)', () => {
    suiteSetup(async () => {
        await vscode.workspace.fs.createDirectory(vscode.Uri.file(dir));
        mgr = new InlineDiffManager();
    });

    suiteTeardown(() => mgr.dispose());

    test('applyChange creates a new file (no inline review)', async function () {
        this.timeout(30_000);
        const p = path.join(dir, `newfile-b-${stamp}.py`);
        const uri = vscode.Uri.file(p);
        await applyChange(uri, "print('hi')\n", false);
        assert.strictEqual(fs.readFileSync(p, 'utf8'), "print('hi')\n");
    });

    test('inline review of a new file + accept writes the content', async function () {
        this.timeout(60_000);
        const p = path.join(dir, `newfile-a-${stamp}.py`);
        const uri = vscode.Uri.file(p);
        const outcomes: string[] = [];
        await mgr.start(uri, null, "print('hi')\n", (o: string) => outcomes.push(o));
        assert.strictEqual(fs.existsSync(p), true, 'file should exist on disk (empty) while reviewing');
        await mgr.resolveAll(uri, true);
        assert.strictEqual(outcomes[0], 'accepted');
        assert.strictEqual(fs.readFileSync(p, 'utf8'), "print('hi')\n");
    });

    test('sidebar accept of a new file after a cancelled inline review', async function () {
        this.timeout(60_000);
        const p = path.join(dir, `newfile-c-${stamp}.py`);
        const uri = vscode.Uri.file(p);
        const outcomes: string[] = [];
        await mgr.start(uri, null, 'x = 1\n', (o: string) => outcomes.push(o));
        // Review cancelled: editor closed, dirty buffer discarded. The (empty) file stays on disk.
        const doc = await vscode.workspace.openTextDocument(uri);
        await vscode.window.showTextDocument(doc, { preview: false });
        await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
        await new Promise(r => setTimeout(r, 500));
        // What _agentAccept does for a pending 'added' change:
        const current = await currentText(uri);
        if (current !== undefined) { // would show "changed since the agent read it" for a real user
            console.log('sidebar accept path sees current text:', JSON.stringify(current));
        }
        await applyChange(uri, 'x = 1\n', current !== undefined);
        assert.strictEqual(fs.readFileSync(p, 'utf8'), 'x = 1\n');
        void mgr;
        void outcomes;
    });

    suiteTeardown(async () => {
        // Close any editors and remove the temp folder.
        await vscode.commands.executeCommand('workbench.action.closeAllEditors');
        await new Promise(r => setTimeout(r, 300));
        try {
            await vscode.workspace.fs.delete(vscode.Uri.file(dir), { recursive: true, useTrash: false });
        } catch { /* already gone */ }
    });
});
