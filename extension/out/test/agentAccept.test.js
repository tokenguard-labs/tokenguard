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
const assert = __importStar(require("assert"));
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
const inlineDiff_1 = require("../inlineDiff");
const chatView_1 = require("../chatView");
// Temporary folder inside the test workspace for new-file flows.
const dir = path.join(__dirname, '..', '..', 'tmp-agent-accept-tests');
const stamp = String(Date.now()) + Math.floor(Math.random() * 1000);
let mgr;
suite('Agent accept (new files)', () => {
    suiteSetup(async () => {
        await vscode.workspace.fs.createDirectory(vscode.Uri.file(dir));
        mgr = new inlineDiff_1.InlineDiffManager();
    });
    suiteTeardown(() => mgr.dispose());
    test('applyChange creates a new file (no inline review)', async function () {
        this.timeout(30_000);
        const p = path.join(dir, `newfile-b-${stamp}.py`);
        const uri = vscode.Uri.file(p);
        await (0, chatView_1.applyChange)(uri, "print('hi')\n", false);
        assert.strictEqual(fs.readFileSync(p, 'utf8'), "print('hi')\n");
    });
    test('inline review of a new file + accept writes the content', async function () {
        this.timeout(60_000);
        const p = path.join(dir, `newfile-a-${stamp}.py`);
        const uri = vscode.Uri.file(p);
        const outcomes = [];
        await mgr.start(uri, null, "print('hi')\n", (o) => outcomes.push(o));
        assert.strictEqual(fs.existsSync(p), true, 'file should exist on disk (empty) while reviewing');
        await mgr.resolveAll(uri, true);
        assert.strictEqual(outcomes[0], 'accepted');
        assert.strictEqual(fs.readFileSync(p, 'utf8'), "print('hi')\n");
    });
    test('sidebar accept of a new file after a cancelled inline review', async function () {
        this.timeout(60_000);
        const p = path.join(dir, `newfile-c-${stamp}.py`);
        const uri = vscode.Uri.file(p);
        const outcomes = [];
        await mgr.start(uri, null, 'x = 1\n', (o) => outcomes.push(o));
        // Review cancelled: editor closed, dirty buffer discarded. The (empty) file stays on disk.
        const doc = await vscode.workspace.openTextDocument(uri);
        await vscode.window.showTextDocument(doc, { preview: false });
        await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
        await new Promise(r => setTimeout(r, 500));
        // What _agentAccept does for a pending 'added' change:
        const current = await (0, chatView_1.currentText)(uri);
        if (current !== undefined) { // would show "changed since the agent read it" for a real user
            console.log('sidebar accept path sees current text:', JSON.stringify(current));
        }
        await (0, chatView_1.applyChange)(uri, 'x = 1\n', current !== undefined);
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
        }
        catch { /* already gone */ }
    });
});
//# sourceMappingURL=agentAccept.test.js.map