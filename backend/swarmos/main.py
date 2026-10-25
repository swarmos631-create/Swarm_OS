"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import inspect, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from swarmos import __version__
from swarmos.agents.registry import AGENTS
from swarmos.api import agents, auth, cities, incidents, internal, platform
from swarmos.config import ON_VERCEL, REPO_ROOT, get_settings
from swarmos.core.errors import install_error_handlers
from swarmos.core.logging import configure_logging, correlation_id_var
from swarmos.db import get_engine

log = logging.getLogger("swarmos.app")


def _queue_missing_models() -> None:
    """Train every configured model that has no promoted version yet, so agents use ML rather than baselines."""
    try:
        from sqlalchemy import select

        from swarmos.db import session_scope, utcnow
        from swarmos.jobs.queue import enqueue
        from swarmos.ml.pipeline import available_configs, training_available
        from swarmos.models import ModelVersion

        if not training_available():
            return
        settings = get_settings()
        with session_scope() as db:
            promoted = set(db.scalars(select(ModelVersion.model_name).where(ModelVersion.status == "promoted")))
            for cfg in available_configs(settings):
                name = cfg["model_name"]
                if name not in promoted:
                    enqueue(db, "train_model", {"model_name": name, "promote_if_better": True, "actor_id": None},
                            dedupe_key=f"autotrain:{name}:{utcnow():%Y%m%d}", priority=7, max_attempts=2)
                    log.info("model_autotrain_queued", extra={"model": name})
    except Exception as exc:  # pragma: no cover - never block startup
        log.warning("model_autotrain_skipped", extra={"error_type": type(exc).__name__})


def _refresh_source_registry() -> None:
    """Re-register data sources at startup so newly added adapters and newly set API keys show up without re-seeding."""
    try:
        from swarmos.db import session_scope
        from swarmos.integrations.sync import sync_registry

        with session_scope() as db:
            sync_registry(db, get_settings())
        from swarmos.demo.seed import seed_documents

        with session_scope() as db:
            seed_documents(db)  # adds new reference documents to existing deployments
    except Exception as exc:  # pragma: no cover - never block startup
        log.warning("source_registry_refresh_skipped", extra={"error_type": type(exc).__name__})


def _preload_models() -> None:
    """Load promoted models into memory in the background so the first agent run does not pay for it."""
    import threading

    def run() -> None:
        try:
            from swarmos.ai.runtime import get_runtime

            loaded = get_runtime().artifacts.preload()
            log.info("models_preloaded", extra={"models": loaded})
        except Exception as exc:  # pragma: no cover - never block startup
            log.warning("model_preload_skipped", extra={"error_type": type(exc).__name__})

    threading.Thread(target=run, name="model-preload", daemon=True).start()


def tune_database() -> None:
    """Cap PostgreSQL's write-ahead log on the app's own database and report its size.

    Postgres lets WAL grow to 1 GB before forcing a checkpoint; on a 500 MB volume a burst of writes (seeding,
    training) fills the disk. Needs a superuser connection (Railway's Postgres template gives one); otherwise skipped.
    """
    settings = get_settings()
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            if settings.db_max_wal_size and conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar():
                current = conn.execute(text("SHOW max_wal_size")).scalar()
                if current != settings.db_max_wal_size:
                    conn.execute(text(f"ALTER SYSTEM SET max_wal_size = '{settings.db_max_wal_size}'"))
                    conn.execute(text("ALTER SYSTEM SET min_wal_size = '32MB'"))
                    conn.execute(text("SELECT pg_reload_conf()"))
                conn.execute(text("CHECKPOINT"))  # recycle WAL segments now
            db_mb = conn.execute(text("SELECT pg_database_size(current_database()) / 1048576")).scalar()
            try:
                wal_mb = conn.execute(text("SELECT (coalesce(sum(size), 0) / 1048576)::int FROM pg_ls_waldir()")).scalar()
            except Exception:
                wal_mb = None
            log.info("database_tuned", extra={"database_mb": db_mb, "wal_mb": wal_mb,
                                               "max_wal_size": conn.execute(text("SHOW max_wal_size")).scalar()})
    except Exception as exc:  # pragma: no cover - depends on the database's permissions
        log.warning("database_tuning_skipped", extra={"error_type": type(exc).__name__, "error": str(exc)[:200]})


def _reclaim_space() -> None:
    """Prune old live readings and vacuum, so the space is reusable before the pre-build writes more."""
    try:
        from swarmos.integrations.sync import prune_measurements

        settings = get_settings()
        removed = prune_measurements(settings.measurement_retention_days)
        engine = get_engine()
        if engine.dialect.name == "postgresql":
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("VACUUM (ANALYZE) measurements"))
                db_mb = conn.execute(text("SELECT pg_database_size(current_database()) / 1048576")).scalar()
        else:
            db_mb = None
        log.info("database_space_reclaimed", extra={"removed": removed, "database_mb": db_mb,
                                                     "retention_days": settings.measurement_retention_days})
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("database_reclaim_skipped", extra={"error_type": type(exc).__name__, "error": str(exc)[:200]})


def _prebuild_cities(stop) -> None:  # type: ignore[no-untyped-def]
    """Build every city's simulated data in the background so switching to a new city is instant."""
    import threading

    def run() -> None:
        try:
            from swarmos.demo.seed import prebuild_cities

            _reclaim_space()
            time.sleep(20)  # let startup work (model training, first syncs) get going first
            prebuild_cities(get_settings(), stop, pause_s=2.0)
        except Exception as exc:  # pragma: no cover - never break the API
            log.warning("city_prebuild_skipped", extra={"error_type": type(exc).__name__})

    threading.Thread(target=run, name="city-prebuild", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        tables = set(inspect(get_engine()).get_table_names())
        if "alembic_version" not in tables:
            log.error("database_not_migrated", extra={"hint": "Run: swarmos init  (or: alembic upgrade head)"})
        elif settings.env != "test":
            _refresh_source_registry()
    except Exception as exc:
        # Keep serving: /api/health/ready and the API's 503 responses then explain the problem to operators.
        log.error("database_unreachable_at_startup", extra={"error_type": type(exc).__name__})
    worker = None
    import threading

    stop_prebuild = threading.Event()
    if settings.worker_embedded and settings.job_mode == "worker" and settings.env != "test":
        from swarmos.jobs.worker import EmbeddedWorker

        tune_database()

        worker = EmbeddedWorker()
        worker.start()
        if settings.auto_train_models:
            _queue_missing_models()
        _preload_models()
        if settings.demo_mode and settings.prebuild_cities:
            _prebuild_cities(stop_prebuild)
    log.info("api_started", extra={"env": settings.env, "agents": len(AGENTS), "worker_embedded": bool(worker)})
    try:
        yield
    finally:
        stop_prebuild.set()
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
                # Same policy as vercel.json: the MapmyIndia Web SDK loads scripts, styles, tiles and fonts from its CDN.
                "default-src 'self'; script-src 'self' https://sdk.mappls.com https://apis.mappls.com https://*.mappls.com; "
                "style-src 'self' 'unsafe-inline' https://*.mappls.com; "
                "img-src 'self' data: blob: https://*.mappls.com https://*.mapmyindia.com; "
                "connect-src 'self' https://*.mappls.com https://*.mapmyindia.com; font-src 'self' data: https://*.mappls.com; "
                "worker-src 'self' blob:; child-src blob:; frame-ancestors 'none'",
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
        db_latency_ms = None
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
                # One warm round trip: a few ms when the API and database share a region, ~100-250 ms when they do not.
                t0 = time.perf_counter()
                conn.execute(text("SELECT 1"))
                db_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                has = "alembic_version" in inspect(conn).get_table_names()
            checks["database"] = "ok"
            checks["migrations"] = "ok" if has else "missing"
        except Exception as exc:  # pragma: no cover - depends on infra
            checks["database"] = f"error: {type(exc).__name__}"
        checks["agents"] = "ok" if len(AGENTS) == 50 else "error"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks, "version": __version__,
                             "db_latency_ms": db_latency_ms, "region": os.environ.get("VERCEL_REGION")},
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


def _misconfigured_app(errors: list[str]) -> FastAPI:
    """Serverless fallback when settings are invalid: every API call answers 503 with the reason.

    Without it the function crashes on import and the browser only sees an opaque platform error.
    Only validator messages are exposed (never input values), and they contain no secrets.
    """
    fallback = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @fallback.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"], include_in_schema=False)
    def misconfigured(path: str) -> JSONResponse:
        return JSONResponse({"error": {"code": "misconfigured", "message": " ".join(errors), "details": None,
                                       "correlation_id": None}}, status_code=503,
                            headers={"Cache-Control": "no-store"})

    return fallback


def _build_app() -> FastAPI:
    try:
        return create_app()
    except ValidationError as exc:
        if not ON_VERCEL:
            raise
        messages = [str(e["msg"]).removeprefix("Value error, ") for e in exc.errors()]
        logging.getLogger("swarmos.app").error("invalid_configuration", extra={"errors": messages})
        return _misconfigured_app(messages)


# Kept as a plain top-level assignment: Vercel's Python builder looks for `app` here statically.
app = _build_app()
