"""Audit records for privileged and consequential actions."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from swarmos.core.logging import correlation_id_var, redact
from swarmos.core.security import Principal
from swarmos.models import AuditLog


def audit(
    db: Session,
    action: str,
    principal: Principal | None = None,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    city_id: str | None = None,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=principal.id if principal else None,
            actor_email=principal.email if principal else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            city_id=city_id,
            outcome=outcome,
            details=redact(details or {}),  # type: ignore[arg-type]
            ip=ip,
            correlation_id=correlation_id_var.get(),
        )
    )
