"""Persistent, incremental repository index (SQLite).

Content-addressed and derivation-environment aware (see `env.py`): a derived row is reused only
when BOTH the source hash and the artifact's environment digest still match. A parser/model/ruleset
upgrade therefore re-derives exactly the affected kind, never a blind full reindex and never silent
staleness.

Storage is intentionally simple (tables for files/symbols/edges) — plenty for a single-repo index
and easy to inspect. The public `CodebaseIndex` API hides the schema so storage can change later.
"""

from __future__ import annotations

import array
import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ..context_engine.config import ContextConfig
from ..context_engine.tokens import TokenCounter
from ..context_engine.walker import walk_repo
from . import env, extract, resolve

_COUNTER = TokenCounter()
log = logging.getLogger("localpilot.codeindex")
_sync_cache: Dict[str, tuple] = {}  # db_path -> (timestamp, SyncReport) for the sync TTL fast path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    language TEXT,
    size INTEGER,
    token_count INTEGER DEFAULT 0,
    mtime REAL,
    dynamic INTEGER DEFAULT 0,
    env_symbols TEXT,
    env_edges TEXT,
    indexed_at REAL,
    text TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    content TEXT,
    env_digest TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    src_path TEXT NOT NULL,
    src_name TEXT,
    dst_name TEXT,
    dst_path TEXT,
    kind TEXT NOT NULL,
    resolution TEXT NOT NULL,
    confidence REAL NOT NULL,
    basis TEXT,
    line INTEGER,
    env_digest TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_path);
CREATE INDEX IF NOT EXISTS idx_edges_dstname ON edges(dst_name);
CREATE INDEX IF NOT EXISTS idx_edges_dstpath ON edges(dst_path);
CREATE TABLE IF NOT EXISTS embeddings (
    symbol_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER,
    vector BLOB,
    env_digest TEXT,
    created_at REAL
);
"""


def _vec_to_blob(vector) -> bytes:
    return array.array("f", vector).tobytes()


def _blob_to_vec(blob: bytes):
    vec = array.array("f")
    vec.frombytes(blob)
    return list(vec)


@dataclass
class SyncReport:
    scanned: int = 0
    parsed: int = 0
    reused_renames: int = 0
    removed: int = 0
    symbols: int = 0
    edges: int = 0
    unresolved_refs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


def default_index_dir() -> str:
    return os.getenv("CONTEXT_INDEX_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "localpilot", "index")


def _repo_key(repo_path: str) -> str:
    return hashlib.sha1(os.path.realpath(repo_path).encode("utf-8")).hexdigest()[:16]


class CodebaseIndex:
    def __init__(self, repo_path: str, index_dir: Optional[str] = None) -> None:
        self.repo_path = os.path.realpath(repo_path)
        self.index_dir = index_dir or default_index_dir()
        os.makedirs(self.index_dir, exist_ok=True)
        self.db_path = os.path.join(self.index_dir, f"{_repo_key(self.repo_path)}.db")
        self._ensure_schema()

    # -- plumbing ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _ensure_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(_SCHEMA)
            # Lightweight migration: add newer columns to pre-existing databases.
            try:
                con.execute("ALTER TABLE files ADD COLUMN text TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            con.commit()
        finally:
            con.close()

    # -- sync -------------------------------------------------------------
    def sync(self, repo_path: Optional[str] = None, force: bool = False) -> SyncReport:
        # Fast path: repeated syncs (e.g. /analyze then /chat) within the TTL reuse the last result
        # instead of re-walking and hashing the whole repo. Correctness window is configurable.
        ttl = float(os.getenv("CONTEXT_INDEX_SYNC_TTL_S", "5"))
        hit = _sync_cache.get(self.db_path)
        if not force and hit and time.time() - hit[0] < ttl:
            return hit[1]

        repo_path = os.path.realpath(repo_path or self.repo_path)
        cfg = ContextConfig()
        files = walk_repo(repo_path, cfg)
        current = {f.rel_path: f for f in files}
        env_sym = env.env_digest("symbols")
        env_edge = env.env_digest("edges")
        report = SyncReport(scanned=len(files))

        con = self._connect()
        try:
            existing = {
                r["path"]: r
                for r in con.execute("SELECT path, source_hash, env_symbols, env_edges FROM files")
            }
            removed: Set[str] = set(existing) - set(current)

            to_parse: List[list] = []
            for rel, info in current.items():
                sh = env.source_hash(info.text)
                row = existing.get(rel)
                if row and row["source_hash"] == sh and row["env_symbols"] == env_sym and row["env_edges"] == env_edge:
                    continue  # fresh: source and environment both unchanged
                to_parse.append([rel, info, sh, row is None])

            # Rename: a new path whose content + environment match a removed file reuses its rows.
            for item in list(to_parse):
                rel, info, sh, is_new = item
                if not is_new:
                    continue
                match = next(
                    (p for p in removed
                     if existing[p]["source_hash"] == sh
                     and existing[p]["env_symbols"] == env_sym
                     and existing[p]["env_edges"] == env_edge),
                    None,
                )
                if match is None:
                    continue
                con.execute("UPDATE files SET path=? WHERE path=?", (rel, match))
                con.execute("UPDATE symbols SET path=? WHERE path=?", (rel, match))
                con.execute("UPDATE edges SET src_path=? WHERE src_path=?", (rel, match))
                con.execute("UPDATE edges SET dst_path=? WHERE dst_path=?", (rel, match))
                removed.discard(match)
                to_parse.remove(item)
                report.reused_renames += 1

            for p in removed:
                con.execute("DELETE FROM files WHERE path=?", (p,))
                con.execute("DELETE FROM symbols WHERE path=?", (p,))
                con.execute("DELETE FROM edges WHERE src_path=?", (p,))
                con.execute(
                    "UPDATE edges SET dst_path=NULL, resolution='syntactic', confidence=0.0, "
                    "basis='definition removed' WHERE dst_path=?", (p,))
                report.removed += 1

            for rel, info, sh, _is_new in to_parse:
                con.execute("DELETE FROM symbols WHERE path=?", (rel,))
                con.execute("DELETE FROM edges WHERE src_path=?", (rel,))
                parsed = extract.parse_file(info)
                for s in parsed.symbols:
                    con.execute(
                        "INSERT INTO symbols(path,name,kind,start_line,end_line,signature,content,env_digest)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (rel, s.name, s.kind, s.start_line, s.end_line, s.signature, s.content, env_sym),
                    )
                for r in parsed.refs:
                    con.execute(
                        "INSERT INTO edges(src_path,src_name,dst_name,dst_path,kind,resolution,"
                        "confidence,basis,line,env_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (rel, "", r.name, None, r.kind, "syntactic", 0.0, "unresolved", r.line, env_edge),
                    )
                token_count = _COUNTER.count(info.text)
                con.execute(
                    "INSERT INTO files(path,source_hash,language,size,token_count,mtime,dynamic,"
                    "env_symbols,env_edges,indexed_at,text) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(path) DO UPDATE SET source_hash=excluded.source_hash,"
                    " language=excluded.language, size=excluded.size, token_count=excluded.token_count,"
                    " mtime=excluded.mtime, dynamic=excluded.dynamic, env_symbols=excluded.env_symbols,"
                    " env_edges=excluded.env_edges, indexed_at=excluded.indexed_at, text=excluded.text",
                    (rel, sh, extract.language_of(rel), info.size, token_count, info.mtime,
                     1 if parsed.dynamic else 0, env_sym, env_edge, time.time(), info.text),
                )
                report.parsed += 1

            # Resolution is global: a changed definition can change other files' edges.
            resolve.resolve_all(con)
            con.commit()

            report.symbols = con.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
            report.edges = con.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"]
            report.unresolved_refs = con.execute(
                "SELECT COUNT(*) AS n FROM edges WHERE resolution != 'resolved'").fetchone()["n"]
            con.execute(
                "INSERT INTO meta(key,value) VALUES('last_sync',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
            con.commit()
        finally:
            con.close()
        _sync_cache[self.db_path] = (time.time(), report)
        return report

    def is_stale(self, rel_path: str) -> bool:
        """True when the file is missing, its content changed, or a component version drifted."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT source_hash, env_symbols, env_edges FROM files WHERE path=?",
                (rel_path,)).fetchone()
            if row is None:
                return True
            if row["env_symbols"] != env.env_digest("symbols") or row["env_edges"] != env.env_digest("edges"):
                return True  # a parser/resolver/chunker upgrade invalidated this row
            full = os.path.join(self.repo_path, rel_path)
            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    sh = env.source_hash(fh.read())
            except OSError:
                return True
            return sh != row["source_hash"]
        finally:
            con.close()

    # -- queries ----------------------------------------------------------
    def symbols(self, name: Optional[str] = None) -> List[sqlite3.Row]:
        con = self._connect()
        try:
            if name is None:
                return list(con.execute("SELECT * FROM symbols"))
            return list(con.execute("SELECT * FROM symbols WHERE name=?", (name,)))
        finally:
            con.close()

    def symbols_in_path(self, rel_path: str) -> List[sqlite3.Row]:
        con = self._connect()
        try:
            return list(con.execute("SELECT * FROM symbols WHERE path=?", (rel_path,)))
        finally:
            con.close()

    def dynamic_paths(self) -> Set[str]:
        con = self._connect()
        try:
            return {r["path"] for r in con.execute("SELECT path FROM files WHERE dynamic=1")}
        finally:
            con.close()

    def symbol_names(self) -> Set[str]:
        con = self._connect()
        try:
            return {r["name"] for r in con.execute("SELECT DISTINCT name FROM symbols")}
        finally:
            con.close()

    def files(self) -> List[sqlite3.Row]:
        con = self._connect()
        try:
            return list(con.execute("SELECT * FROM files"))
        finally:
            con.close()

    def edges(self, src_path: Optional[str] = None, dst_path: Optional[str] = None,
              dst_name: Optional[str] = None, min_confidence: float = 0.0) -> List[sqlite3.Row]:
        clauses, params = ["confidence >= ?"], [min_confidence]
        if src_path is not None:
            clauses.append("src_path = ?")
            params.append(src_path)
        if dst_path is not None:
            clauses.append("dst_path = ?")
            params.append(dst_path)
        if dst_name is not None:
            clauses.append("dst_name = ?")
            params.append(dst_name)
        sql = "SELECT * FROM edges WHERE " + " AND ".join(clauses)
        con = self._connect()
        try:
            return list(con.execute(sql, params))
        finally:
            con.close()

    def related_paths(self, path: str, direction: str = "both",
                      min_confidence: float = 0.5) -> Dict[str, List[Dict[str, Any]]]:
        """Files related to `path` through resolved/confident edges, with provenance."""
        out = {"outgoing": [], "incoming": []}
        con = self._connect()
        try:
            if direction in ("outgoing", "both"):
                for r in con.execute(
                        "SELECT * FROM edges WHERE src_path=? AND dst_path IS NOT NULL AND confidence>=?",
                        (path, min_confidence)):
                    out["outgoing"].append({
                        "path": r["dst_path"], "kind": r["kind"], "resolution": r["resolution"],
                        "confidence": r["confidence"], "basis": r["basis"],
                    })
            if direction in ("incoming", "both"):
                for r in con.execute(
                        "SELECT * FROM edges WHERE dst_path=? AND confidence>=?",
                        (path, min_confidence)):
                    out["incoming"].append({
                        "path": r["src_path"], "kind": r["kind"], "resolution": r["resolution"],
                        "confidence": r["confidence"], "basis": r["basis"],
                    })
        finally:
            con.close()
        return out

    # -- embeddings (Stage 2, fail-open) ----------------------------------
    def embed_missing(self, embedder, limit: Optional[int] = None) -> int:
        """Embed symbols that lack a vector for this model+environment. Returns how many were made."""
        env_d = env.env_digest("embeddings")
        model = embedder.name
        limit = limit or int(os.getenv("CONTEXT_EMBED_MAX_SYMBOLS", "2000"))
        con = self._connect()
        try:
            have = {r["symbol_id"] for r in con.execute(
                "SELECT symbol_id FROM embeddings WHERE model=? AND env_digest=?", (model, env_d))}
            rows = [r for r in con.execute("SELECT id,name,kind,path,content FROM symbols")
                    if r["id"] not in have][:limit]
        finally:
            con.close()
        if not rows:
            return 0
        texts = [f"{r['name']} {r['kind']} {r['path']}\n{(r['content'] or '')[:2000]}" for r in rows]
        try:
            vectors = embedder.embed(texts)
        except Exception as e:  # noqa: BLE001 - fail open
            log.info("embedding failed; semantic retrieval disabled this run: %s", e)
            return 0
        con = self._connect()
        try:
            con.executemany(
                "INSERT OR REPLACE INTO embeddings(symbol_id,model,dim,vector,env_digest,created_at)"
                " VALUES(?,?,?,?,?,?)",
                [(r["id"], model, len(v), _vec_to_blob(v), env_d, time.time())
                 for r, v in zip(rows, vectors)])
            con.commit()
        finally:
            con.close()
        return len(rows)

    def load_embeddings(self, model: str):
        env_d = env.env_digest("embeddings")
        con = self._connect()
        try:
            out = []
            for r in con.execute(
                    "SELECT s.id,s.path,s.name,s.kind,s.start_line,s.end_line,s.content,e.vector"
                    " FROM embeddings e JOIN symbols s ON s.id=e.symbol_id"
                    " WHERE e.model=? AND e.env_digest=?", (model, env_d)):
                out.append((dict(r), _blob_to_vec(r["vector"])))
            return out
        finally:
            con.close()

    def clear_embeddings(self) -> None:
        con = self._connect()
        try:
            con.execute("DELETE FROM embeddings")
            con.commit()
        finally:
            con.close()

    def status(self) -> Dict[str, Any]:
        con = self._connect()
        try:
            n_files = con.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
            n_symbols = con.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
            n_edges = con.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"]
            unresolved = con.execute(
                "SELECT COUNT(*) AS n FROM edges WHERE resolution != 'resolved'").fetchone()["n"]
            return {
                "files": n_files, "symbols": n_symbols, "edges": n_edges,
                "unresolved_refs": unresolved,
                "env": {"symbols": env.env_digest("symbols"), "edges": env.env_digest("edges")},
            }
        finally:
            con.close()
