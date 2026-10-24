"""Internal endpoints invoked by the platform scheduler (Vercel Cron Jobs), not by users."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from swarmos.config import get_settings
from swarmos.core.errors import ApiError

router = APIRouter(prefix="/internal", include_in_schema=False)
log = logging.getLogger("swarmos.cron")


def _require_cron_secret(request: Request) -> None:
    settings = get_settings()
    if not settings.cron_secret:
        raise ApiError(503, "cron_not_configured", "Set CRON_SECRET to enable scheduled jobs")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {settings.cron_secret}".encode()):
        raise ApiError(401, "unauthenticated", "Invalid cron credentials")


@router.get("/cron")
async def cron_tick(request: Request) -> dict[str, Any]:
    """Enqueue periodic syncs and run queued jobs within a time budget.

    Vercel calls this with ``Authorization: Bearer $CRON_SECRET``. Without a configured secret the endpoint is
    disabled, so it can never be triggered anonymously.
    """
    _require_cron_secret(request)
    settings = get_settings()

    from swarmos.jobs.worker import drain_queue, schedule_periodic_syncs

    scheduled = schedule_periodic_syncs(settings) if settings.sync_enabled else 0
    stats = await drain_queue(settings.cron_budget_seconds)
    log.info("cron_tick", extra={"scheduled": scheduled, **stats})
    return {"scheduled": scheduled, **stats}


class BootstrapAdmin(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    name: str | None = Field(default=None, max_length=120)


class BootstrapRequest(BaseModel):
    admin: BootstrapAdmin | None = None


@router.post("/bootstrap")
def bootstrap(request: Request, body: BootstrapRequest | None = None) -> dict[str, Any]:
    """Migrate, seed (no demo data) and optionally create the first admin, from inside the deployment.

    Equivalent to ``swarmos migrate && swarmos seed --no-demo && swarmos create-user <email> admin --all-cities``
    for databases that are not reachable from a workstation. Guarded by ``CRON_SECRET`` and idempotent: migrations
    stop at head, seeding upserts, and an existing admin email is left untouched.
    """
    _require_cron_secret(request)
    from alembic import command
    from sqlalchemy import select

    from swarmos.cli import _alembic_cfg
    from swarmos.core.security import hash_password, validate_password_strength
    from swarmos.db import session_scope
    from swarmos.demo.seed import bootstrap as seed
    from swarmos.models import User

    settings = get_settings()
    if body and body.admin:
        validate_password_strength(body.admin.password)  # fail before touching the database

    command.upgrade(_alembic_cfg(), "head")
    out: dict[str, Any] = {"migrated": True}
    with session_scope() as db:
        seeded = seed(db, settings, demo=False)
    seeded.pop("credentials_file", None)
    out["seeded"] = seeded

    if body and body.admin:
        email = body.admin.email.strip().lower()
        with session_scope() as db:
            if db.scalar(select(User).where(User.email == email)):
                out["admin"] = "exists"
            else:
                db.add(User(email=email, display_name=body.admin.name or email, role="admin", all_cities=True,
                            password_hash=hash_password(body.admin.password)))
                out["admin"] = "created"
    log.info("bootstrap", extra={"admin": out.get("admin")})
    return out
