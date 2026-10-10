"""Thin FastAPI wrapper exposing ``POST /context`` for Person 1's orchestrator.

FastAPI is an optional dependency; the core engine is stdlib-only. If FastAPI
is not installed, importing this module still succeeds and ``app`` is ``None``
with an explanatory ``IMPORT_ERROR``.
"""

from __future__ import annotations

from .engine import ContextEngine
from .models import ContextRequest

IMPORT_ERROR = None
app = None

try:  # pragma: no cover - depends on environment
    from fastapi import Body, FastAPI

    _engine = ContextEngine()

    def create_app():
        application = FastAPI(
            title="LocalPilot Context Engine",
            description="Person 2: repository context selection and token reduction.",
            version="0.1.0",
        )

        @application.get("/health")
        def health():
            return {"status": "ok", "counter": _engine.counter.label}

        @application.post("/context")
        def context(payload: dict = Body(...)):
            req = ContextRequest(
                query=payload.get("query", ""),
                repo_path=payload["repo_path"],
                task_type=payload.get("task_type", "general"),
                max_tokens=int(payload.get("max_tokens", 4000)),
                top_k=int(payload.get("top_k", 8)),
                include_globs=payload.get("include_globs", []) or [],
                exclude_globs=payload.get("exclude_globs", []) or [],
                redact_secrets=bool(payload.get("redact_secrets", True)),
                chunking=payload.get("chunking", "symbol"),
                expand_deps=bool(payload.get("expand_deps", True)),
            )
            return _engine.build(req).to_dict()

        return application

    app = create_app()
except Exception as exc:  # pragma: no cover - depends on environment
    IMPORT_ERROR = (
        f"FastAPI not available ({exc.__class__.__name__}). "
        "Install with: pip install fastapi uvicorn"
    )


def main() -> None:  # pragma: no cover - manual entrypoint
    import uvicorn

    uvicorn.run("compression.context_engine.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
