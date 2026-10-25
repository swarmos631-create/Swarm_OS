"""Integration adapter contract and normalized record types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator

from swarmos.config import Settings
from swarmos.models import City


class NormalizedMeasurement(BaseModel):
    category: str
    metric: str
    value: float
    unit: str
    observed_at: datetime
    kind: Literal["observation", "forecast", "model_analysis"] = "observation"
    entity: str = "city"
    lat: float | None = None
    lon: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return v

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("value must be finite")
        return v


class NormalizedFeature(BaseModel):
    layer: str
    external_id: str
    name: str | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    properties: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None


class NormalizedIncident(BaseModel):
    """An official alert or event that becomes a live incident for one city (deduplicated by `external_id`)."""

    external_id: str = Field(max_length=60)
    title: str
    description: str = ""
    category: str
    severity: Literal["low", "moderate", "high", "critical"]
    lat: float | None = None
    lon: float | None = None
    reported_at: datetime
    expires_at: datetime | None = None
    department: str | None = None


@dataclass
class SyncPayload:
    measurements: list[NormalizedMeasurement] = field(default_factory=list)
    features: list[NormalizedFeature] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # None: the source does not report incidents. A list (even empty) is the complete current set for the city,
    # so live incidents from this source that are no longer listed are marked resolved.
    incidents: list[NormalizedIncident] | None = None


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    kind: Literal["api", "feed", "fixture", "internal"]
    categories: tuple[str, ...]
    coverage: str
    auth_required: str
    license: str
    attribution: str
    homepage: str | None
    freshness_threshold_s: int
    min_sync_interval_s: int
    scope: Literal["city", "global", "batch", "none"]
    limitations: tuple[str, ...] = ()
    setup: str | None = None
    # "batch" sources fetch many cities per request; `batch_size` cities are fetched and stored at a time.
    batch_size: int = 20
    # A backup source only fetches cities whose primary source has no successful sync within `fallback_after_s`.
    fallback_for: str | None = None
    fallback_after_s: int = 2 * 3600
    # False: only synchronised on demand (Integrations page), never by the scheduler or on city visits.
    scheduled: bool = True


class Adapter(ABC):
    spec: SourceSpec

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        """Return (ready, reason_if_not). Must not perform network I/O."""
        return True, None

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload: ...

    async def fetch_batch(self, client: httpx.AsyncClient, settings: Settings, cities: list[City]) -> dict[str, SyncPayload]:
        """Batch sources: one payload per city id. A city missing from the result is recorded as failed."""
        raise NotImplementedError


class IntegrationError(Exception):
    """A classified, user-presentable integration failure (never includes secrets)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # timeout | http_error | rate_limited | schema | network | auth | config
