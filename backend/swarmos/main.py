"""FastAPI application factory."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import inspect, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from swarmos import __version__
from swarmos.agents.registry import AGENTS
from swarmos.api import agents, auth, cities, incidents, internal, platform
from swarmos.config import REPO_ROOT, get_settings
from swarmos.core.errors import install_error_handlers
from swarmos.core.logging import configure_logging, correlation_id_var
from swarmos.db import get_engine

log = logging.getLogger("swarmos.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    tables = set(inspect(get_engine()).get_table_names())
    if "alembic_version" not in tables:
        log.error("database_not_migrated", extra={"hint": "Run: swarmos init  (or: alembic upgrade head)"})
    worker = None
    if settings.worker_embedded and settings.job_mode == "worker" and settings.env != "test":
        from swarmos.jobs.worker import EmbeddedWorker

        worker = EmbeddedWorker()
        worker.start()
    log.info("api_started", extra={"env": settings.env, "agents": len(AGENTS), "worker_embedded": bool(worker)})
    try:
        yield
    finally:
        if worker:
            worker.stop()
        get_engine().dispose()
        log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="SwarmOS API",
        version=__version__,
        description="Intelligent operating system for Indian cities. All AI runs locally; no hosted LLM APIs are used.",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "production" else None,
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    install_error_handlers(app)
    if settings.trusted_hosts and "*" not in settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["content-type", "x-csrf-token", "x-request-id"])

    @app.middleware("http")
    async def correlation_and_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-request-id", "")
        cid = incoming if incoming.isalnum() and len(incoming) <= 64 else secrets.token_hex(8)
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Request-ID"] = cid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/api/"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob: https://tiles.openfreemap.org; "
                "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; font-src 'self' data:; frame-ancestors 'none'",
            )
        elif not request.url.path.startswith("/api/docs"):
            response.headers["Cache-Control"] = "no-store"  # never let browsers/SW cache privileged API responses
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if settings.env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    prefix = "/api/v1"
    for r in (auth.router, cities.router, incidents.router, agents.router, platform.router):
        app.include_router(r, prefix=prefix)
    app.include_router(internal.router, prefix="/api")

    @app.get("/api/health/live", tags=["system"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready", tags=["system"])
    def ready() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
                has = "alembic_version" in inspect(conn).get_table_names()
            checks["database"] = "ok"
            checks["migrations"] = "ok" if has else "missing"
        except Exception as exc:  # pragma: no cover - depends on infra
            checks["database"] = f"error: {type(exc).__name__}"
        checks["agents"] = "ok" if len(AGENTS) == 50 else "error"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks, "version": __version__},
                            status_code=200 if ok else 503)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built PWA (frontend/dist) from the same origin when it exists, with SPA fallback."""
    dist = (REPO_ROOT / "frontend" / "dist").resolve()
    if not (dist / "index.html").exists():
        return

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        target = (dist / path).resolve()
        if path and dist in target.parents and target.is_file():
            immutable = path.startswith("assets/")
            headers = {"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"}
            return FileResponse(target, headers=headers)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})


app = create_app()
