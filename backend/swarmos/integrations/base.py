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


@dataclass
class SyncPayload:
    measurements: list[NormalizedMeasurement] = field(default_factory=list)
    features: list[NormalizedFeature] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


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
    scope: Literal["city", "global", "none"]
    limitations: tuple[str, ...] = ()
    setup: str | None = None


class Adapter(ABC):
    spec: SourceSpec

    def configured(self, settings: Settings) -> tuple[bool, str | None]:
        """Return (ready, reason_if_not). Must not perform network I/O."""
        return True, None

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, settings: Settings, city: City | None) -> SyncPayload: ...


class IntegrationError(Exception):
    """A classified, user-presentable integration failure (never includes secrets)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # timeout | http_error | rate_limited | schema | network | auth | config
