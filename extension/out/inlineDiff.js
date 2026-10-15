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
exports.InlineDiffManager = void 0;
exports.diffLines = diffLines;
const vscode = __importStar(require("vscode"));
const RESOLVE_COMMAND = 'tokenguard.inlineDiff.resolve';
const MAX_DIFF_CELLS = 4_000_000; // LCS table size; larger changes are shown as one block
class InlineDiffManager {
    _reviews = new Map();
    _applying = 0; // our own edits are in flight: don't treat them as user edits
    _nextHunk = 1;
    _lensChanged = new vscode.EventEmitter();
    onDidChangeCodeLenses = this._lensChanged.event;
    _removed = vscode.window.createTextEditorDecorationType({
        isWholeLine: true,
        backgroundColor: new vscode.ThemeColor('diffEditor.removedLineBackground'),
        overviewRulerColor: new vscode.ThemeColor('editorOverviewRuler.deletedForeground'),
        overviewRulerLane: vscode.OverviewRulerLane.Left
    });
    _added = vscode.window.createTextEditorDecorationType({
        isWholeLine: true,
        backgroundColor: new vscode.ThemeColor('diffEditor.insertedLineBackground'),
        overviewRulerColor: new vscode.ThemeColor('editorOverviewRuler.addedForeground'),
        overviewRulerLane: vscode.OverviewRulerLane.Left
    });
    _disposables = [];
    constructor() {
        this._disposables.push(this._removed, this._added, this._lensChanged, vscode.languages.registerCodeLensProvider({ scheme: 'file' }, this), vscode.commands.registerCommand(RESOLVE_COMMAND, (uri, hunkId, accept) => this._resolve(uri, hunkId, accept)), vscode.workspace.onDidChangeTextDocument(e => this._onUserEdit(e)), vscode.window.onDidChangeVisibleTextEditors(editors => editors.forEach(ed => this._decorate(ed))), vscode.workspace.onDidCloseTextDocument(doc => {
            // Closed without deciding (and its unsaved buffer discarded): the review is over.
            const review = this._reviews.get(doc.uri.toString());
            if (review) {
                this._reviews.delete(doc.uri.toString());
                review.onDone('cancelled');
            }
        }));
    }
    dispose() {
        this._disposables.forEach(d => d.dispose());
    }
    isReviewing(uri) {
        return this._reviews.has(uri.toString());
    }
    /**
     * Show `proposed` against `original` inline in `uri` and open it. `original` must be the
     * file's current text (null: the file does not exist); `proposed` null deletes the file.
     */
    async start(uri, original, proposed, onDone) {
        if (this.isReviewing(uri)) {
            await this._show(uri);
            return;
        }
        const oldLines = splitLines(original ?? '');
        const newLines = splitLines(proposed ?? '');
        const hunks = diffLines(oldLines, newLines).map(h => ({ ...h, id: this._nextHunk++ }));
        if (!hunks.length) {
            onDone('accepted');
            return;
        }
        // Old text with each changed block's new lines inserted right after its old lines.
        const lines = [];
        let at = 0;
        for (const hunk of hunks) {
            lines.push(...oldLines.slice(at, hunk.start));
            const start = lines.length;
            lines.push(...hunk.removed, ...hunk.added);
            at = hunk.start + hunk.removed.length;
            hunk.start = start;
        }
        lines.push(...oldLines.slice(at));
        const edit = new vscode.WorkspaceEdit();
        if (original === null) {
            // A brand-new file: make its folder first — WorkspaceEdit.createFile does not create
            // parent directories, so a file in a new folder would silently fail to appear.
            await vscode.workspace.fs.createDirectory(parentDir(uri));
            edit.createFile(uri, { ignoreIfExists: true });
            edit.insert(uri, new vscode.Position(0, 0), lines.join('\n'));
        }
        else {
            const doc = await vscode.workspace.openTextDocument(uri);
            edit.replace(uri, fullRange(doc), lines.join('\n'));
        }
        this._reviews.set(uri.toString(), {
            uri, hunks, accepted: 0, rejected: 0, onDone,
            createdFile: original === null, deletesFile: proposed === null
        });
        if (!(await this._apply(edit))) {
            if (original === null) {
                // createFile can still be refused (read-only folder, odd provider): write it directly.
                try {
                    await vscode.workspace.fs.writeFile(uri, new TextEncoder().encode(lines.join('\n')));
                }
                catch (e) {
                    this._reviews.delete(uri.toString());
                    throw new Error(`VS Code could not create the file: ${e.message}`);
                }
            }
            else {
                this._reviews.delete(uri.toString());
                throw new Error('VS Code refused to show the change in the editor (is the file read-only?)');
            }
        }
        await this._show(uri);
    }
    /** Decide every remaining block of `uri` (the sidebar's Accept / Reject for that file). */
    async resolveAll(uri, accept) {
        await this._resolve(uri.toString(), undefined, accept);
    }
    /** Put every file under review back the way it was (e.g. when the chat is cleared). */
    async rejectAll() {
        for (const key of [...this._reviews.keys()]) {
            await this._resolve(key, undefined, false);
        }
    }
    provideCodeLenses(doc) {
        const review = this._reviews.get(doc.uri.toString());
        if (!review) {
            return [];
        }
        const key = doc.uri.toString();
        const top = new vscode.Range(0, 0, 0, 0);
        const n = review.hunks.length;
        const lenses = [
            new vscode.CodeLens(top, { title: `LocalPilot agent: ${n} change${n === 1 ? '' : 's'} to review`, command: '' }),
            new vscode.CodeLens(top, { title: '✓ Accept all', command: RESOLVE_COMMAND, arguments: [key, undefined, true] }),
            new vscode.CodeLens(top, { title: '✗ Reject all', command: RESOLVE_COMMAND, arguments: [key, undefined, false] })
        ];
        for (const hunk of review.hunks) {
            const range = new vscode.Range(hunk.start, 0, hunk.start, 0);
            lenses.push(new vscode.CodeLens(range, { title: '✓ Accept', command: RESOLVE_COMMAND, arguments: [key, hunk.id, true] }), new vscode.CodeLens(range, { title: '✗ Reject', command: RESOLVE_COMMAND, arguments: [key, hunk.id, false] }));
        }
        return lenses;
    }
    // ---- internals ------------------------------------------------------------------------
    async _show(uri) {
        const editor = await vscode.window.showTextDocument(uri, { preview: false, preserveFocus: true });
        this._decorate(editor);
        const first = this._reviews.get(uri.toString())?.hunks[0];
        if (first) {
            editor.revealRange(new vscode.Range(first.start, 0, first.start, 0), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
        }
    }
    async _apply(edit) {
        this._applying++;
        try {
            return await vscode.workspace.applyEdit(edit);
        }
        finally {
            this._applying--;
        }
    }
    async _resolve(key, hunkId, accept) {
        const review = this._reviews.get(key);
        if (!review) {
            return;
        }
        const chosen = review.hunks.filter(h => hunkId === undefined || h.id === hunkId);
        if (!chosen.length) {
            return;
        }
        const doc = await vscode.workspace.openTextDocument(review.uri);
        const edit = new vscode.WorkspaceEdit();
        for (const hunk of chosen) {
            // Accept drops the old (red) lines; reject drops the new (green) ones.
            const [from, count] = accept
                ? [hunk.start, hunk.removed.length]
                : [hunk.start + hunk.removed.length, hunk.added.length];
            if (count) {
                edit.delete(review.uri, lineSpan(doc, from, count));
            }
        }
        if (!(await this._apply(edit))) {
            void vscode.window.showWarningMessage('TokenGuard: could not apply that choice to the file.');
            return;
        }
        // Shift the blocks below by the lines just removed.
        const decided = new Set(chosen.map(h => h.id));
        let removedAbove = 0;
        const remaining = [];
        for (const hunk of review.hunks) {
            if (decided.has(hunk.id)) {
                removedAbove += accept ? hunk.removed.length : hunk.added.length;
            }
            else {
                remaining.push({ ...hunk, start: hunk.start - removedAbove });
            }
        }
        review.hunks = remaining;
        review[accept ? 'accepted' : 'rejected'] += chosen.length;
        if (remaining.length) {
            this._refresh(review.uri);
            return;
        }
        await this._finish(review);
    }
    /** Every block decided: write the result (or put the file back) and report. */
    async _finish(review) {
        this._reviews.delete(review.uri.toString());
        this._refresh(review.uri);
        const outcome = !review.accepted ? 'rejected' : review.rejected ? 'partial' : 'accepted';
        try {
            if ((review.deletesFile && outcome === 'accepted') || (review.createdFile && outcome === 'rejected')) {
                const doc = await vscode.workspace.openTextDocument(review.uri);
                if (doc.isDirty) {
                    await doc.save(); // so deleting it does not leave a dirty editor behind
                }
                const edit = new vscode.WorkspaceEdit();
                edit.deleteFile(review.uri, { ignoreIfNotExists: true });
                await this._apply(edit);
            }
            else {
                const doc = await vscode.workspace.openTextDocument(review.uri);
                if (doc.isDirty) {
                    await doc.save();
                }
            }
        }
        finally {
            review.onDone(outcome);
        }
    }
    /** The user typed in a file under review: keep the blocks' line numbers in step. */
    _onUserEdit(e) {
        const review = this._reviews.get(e.document.uri.toString());
        if (!review || this._applying || !e.contentChanges.length) {
            return;
        }
        for (const change of e.contentChanges) {
            const first = change.range.start.line;
            const last = change.range.end.line;
            const delta = (change.text.match(/\n/g)?.length ?? 0) - (last - first);
            review.hunks = review.hunks.filter(hunk => {
                const end = hunk.start + hunk.removed.length + hunk.added.length - 1;
                if (hunk.start > last) {
                    hunk.start += delta;
                    return true;
                }
                // Edited inside a block: fine while the line count holds; otherwise the block can no
                // longer be tracked, so it is kept as the user left it.
                return end < first || delta === 0;
            });
        }
        if (!review.hunks.length) {
            void this._finish(review);
            return;
        }
        this._refresh(review.uri);
    }
    _refresh(uri) {
        vscode.window.visibleTextEditors
            .filter(ed => ed.document.uri.toString() === uri.toString())
            .forEach(ed => this._decorate(ed));
        this._lensChanged.fire();
    }
    _decorate(editor) {
        const review = this._reviews.get(editor.document.uri.toString());
        const removed = [];
        const added = [];
        for (const hunk of review?.hunks ?? []) {
            if (hunk.removed.length) {
                removed.push(new vscode.Range(hunk.start, 0, hunk.start + hunk.removed.length - 1, 0));
            }
            if (hunk.added.length) {
                const from = hunk.start + hunk.removed.length;
                added.push(new vscode.Range(from, 0, from + hunk.added.length - 1, 0));
            }
        }
        editor.setDecorations(this._removed, removed);
        editor.setDecorations(this._added, added);
    }
}
exports.InlineDiffManager = InlineDiffManager;
function splitLines(text) {
    return text.split(/\r?\n/);
}
function fullRange(doc) {
    return new vscode.Range(doc.positionAt(0), doc.positionAt(doc.getText().length));
}
/** The folder that should contain `uri`. */
function parentDir(uri) {
    const parts = uri.path.split('/');
    parts.pop();
    return uri.with({ path: parts.join('/') || '/' });
}
/** The range covering `count` whole lines from `from`, including their line breaks. */
function lineSpan(doc, from, count) {
    const to = from + count;
    if (to < doc.lineCount) {
        return new vscode.Range(from, 0, to, 0);
    }
    const end = doc.lineAt(doc.lineCount - 1).range.end;
    return from > 0 ? new vscode.Range(doc.lineAt(from - 1).range.end, end) : new vscode.Range(0, 0, end.line, end.character);
}
/** Line diff as changed blocks: `start` is the block's first line in `a`. */
function diffLines(a, b) {
    let pre = 0;
    while (pre < a.length && pre < b.length && a[pre] === b[pre]) {
        pre++;
    }
    let suf = 0;
    while (suf < a.length - pre && suf < b.length - pre && a[a.length - 1 - suf] === b[b.length - 1 - suf]) {
        suf++;
    }
    const A = a.slice(pre, a.length - suf);
    const B = b.slice(pre, b.length - suf);
    const ops = [];
    if ((A.length + 1) * (B.length + 1) > MAX_DIFF_CELLS) {
        A.forEach(() => ops.push('-'));
        B.forEach(() => ops.push('+'));
    }
    else {
        // lcs[i][j]: longest common subsequence of A[i:] and B[j:]
        const w = B.length + 1;
        const lcs = new Uint32Array((A.length + 1) * w);
        for (let i = A.length - 1; i >= 0; i--) {
            for (let j = B.length - 1; j >= 0; j--) {
                lcs[i * w + j] = A[i] === B[j] ? lcs[(i + 1) * w + j + 1] + 1 : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
            }
        }
        let i = 0;
        let j = 0;
        while (i < A.length && j < B.length) {
            if (A[i] === B[j]) {
                ops.push('=');
                i++;
                j++;
            }
            else if (lcs[(i + 1) * w + j] >= lcs[i * w + j + 1]) {
                ops.push('-');
                i++;
            }
            else {
                ops.push('+');
                j++;
            }
        }
        while (i++ < A.length) {
            ops.push('-');
        }
        while (j++ < B.length) {
            ops.push('+');
        }
    }
    const hunks = [];
    let ai = 0;
    let bi = 0;
    let current;
    for (const op of ops) {
        if (op === '=') {
            current = undefined;
            ai++;
            bi++;
            continue;
        }
        if (!current) {
            current = { start: pre + ai, removed: [], added: [] };
            hunks.push(current);
        }
        if (op === '-') {
            current.removed.push(A[ai++]);
        }
        else {
            current.added.push(B[bi++]);
        }
    }
    return hunks;
}
//# sourceMappingURL=inlineDiff.js.map