"""Internal endpoints invoked by the platform scheduler (Vercel Cron Jobs), not by users."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request

from swarmos.config import get_settings
from swarmos.core.errors import ApiError

router = APIRouter(prefix="/internal", include_in_schema=False)
log = logging.getLogger("swarmos.cron")


@router.get("/cron")
async def cron_tick(request: Request) -> dict[str, Any]:
    """Enqueue periodic syncs and run queued jobs within a time budget.

    Vercel calls this with ``Authorization: Bearer $CRON_SECRET``. Without a configured secret the endpoint is
    disabled, so it can never be triggered anonymously.
    """
    settings = get_settings()
    if not settings.cron_secret:
        raise ApiError(503, "cron_not_configured", "Set CRON_SECRET to enable scheduled jobs")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {settings.cron_secret}".encode()):
        raise ApiError(401, "unauthenticated", "Invalid cron credentials")

    from swarmos.jobs.worker import drain_queue, schedule_periodic_syncs

    scheduled = schedule_periodic_syncs(settings) if settings.sync_enabled else 0
    stats = await drain_queue(settings.cron_budget_seconds)
    log.info("cron_tick", extra={"scheduled": scheduled, **stats})
    return {"scheduled": scheduled, **stats}
