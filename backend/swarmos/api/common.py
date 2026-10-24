"""Shared API helpers: pagination and serialisation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session


class PageParams(BaseModel):
    limit: int
    offset: int


def page_params(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=1_000_000)) -> PageParams:
    return PageParams(limit=limit, offset=offset)


def paginate(db: Session, stmt: Select[Any], page: PageParams, serialize) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = [serialize(r) for r in db.scalars(stmt.limit(page.limit).offset(page.offset))]
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def row_dict(obj: Any, fields: list[str]) -> dict[str, Any]:
    out = {}
    for f in fields:
        v = getattr(obj, f)
        out[f] = v.isoformat() if isinstance(v, datetime) else v
    return out
