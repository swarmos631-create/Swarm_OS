"""Privacy-aware, public route alternatives using a best-effort open routing service."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from swarmos.config import get_settings
from swarmos.core.ratelimit import client_ip, limiter
from swarmos.db import get_db
from swarmos.services.routing import compare_routes

router = APIRouter(tags=["routing"])


class Coordinate(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteCompareIn(BaseModel):
    origin: Coordinate
    destination: Coordinate
    mode: Literal["foot", "bike", "car"] = "foot"


@router.post("/public/cities/{city_id}/routes/compare")
async def route_compare(city_id: str, body: RouteCompareIn, request: Request,
                        db: Session = Depends(get_db)) -> dict:
    limiter.check(f"public-route:{client_ip(request)}", 3, window_s=60)
    return await compare_routes(db, city_id, (body.origin.lat, body.origin.lon),
                                (body.destination.lat, body.destination.lon), body.mode,
                                get_settings().http_user_agent)
