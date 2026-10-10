"""Default configuration for the Context Engine (P2-lite)."""

from __future__ import annotations

from dataclasses import dataclass, field

EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    "coverage",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "vendor",
    "target",
}

EXCLUDE_EXT = {
    ".min.js",
    ".min.css",
    ".map",
    ".lock",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".bmp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".rar",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".wav",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".class",
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
}

CODE_EXT = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".rb",
    ".php",
    ".cs",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".swift",
    ".scala",
    ".sh",
    ".bash",
    ".sql",
    ".lua",
    ".r",
}


@dataclass
class ContextConfig:
    """Tunable knobs. Every value can be overridden per request."""

    top_k: int = 8
    max_tokens: int = 4000
    max_file_bytes: int = 512 * 1024
    max_chunk_lines: int = 200
    redact_secrets: bool = True
    include_data: bool = False
    include_globs: list = field(default_factory=list)
    exclude_globs: list = field(default_factory=list)

    # --- Refined context building (v2) ---
    # "file"   -> whole-file selection (P2-lite, safe fallback)
    # "symbol" -> function/class-level chunks + import graph
    chunking: str = "symbol"
    expand_deps: bool = True
    dep_hops: int = 1
    dep_discount: float = 0.5
    strip_comments: bool = False
    respect_gitignore: bool = True
    window_lines: int = 80
    window_overlap: int = 10
    # Symbol-mode quality knobs.
    chunk_rel_threshold: float = 0.4  # keep chunks scoring >= best_chunk * this
    max_chunks_per_file: int = 4  # bound how deep any single file is mined

    def __post_init__(self) -> None:
        if self.top_k < 1:
            self.top_k = 1
        if self.max_tokens < 1:
            self.max_tokens = 1
        if self.chunking not in ("file", "symbol"):
            self.chunking = "symbol"
        if self.dep_hops < 0:
            self.dep_hops = 0
