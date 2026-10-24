"""Feature engineering shared by training and inference (single source of truth to avoid skew)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

IST = timedelta(hours=5, minutes=30)
FORECAST_LAGS = (24, 48, 168)
FORECAST_FEATURES = ("lag_24", "lag_48", "lag_168", "mean_24_prior", "hour_sin", "hour_cos", "dow", "is_weekend")
MIN_HISTORY_HOURS = 168 + 24


def floor_hour(t: datetime) -> datetime:
    t = t if t.tzinfo else t.replace(tzinfo=UTC)
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hourly_map(times: Sequence[datetime], values: Sequence[float]) -> dict[datetime, float]:
    return {floor_hour(t): float(v) for t, v in zip(times, values, strict=True)}


def forecast_row(series: dict[datetime, float], target: datetime) -> list[float] | None:
    """Direct multi-horizon features: only lags >= 24 h, so any target up to 24 h ahead is computable."""
    target = floor_hour(target)
    lags = []
    for lag in FORECAST_LAGS:
        v = series.get(target - timedelta(hours=lag))
        if v is None:
            return None
        lags.append(v)
    window = [series.get(target - timedelta(hours=h)) for h in range(24, 48)]
    if any(v is None for v in window):
        return None
    local = target + IST
    hour = local.hour
    return [
        *lags,
        float(np.mean(window)),  # type: ignore[arg-type]
        math.sin(2 * math.pi * hour / 24),
        math.cos(2 * math.pi * hour / 24),
        float(local.weekday()),
        1.0 if local.weekday() >= 5 else 0.0,
    ]


def seasonal_naive(series: dict[datetime, float], target: datetime, lag_hours: int = 168) -> float | None:
    return series.get(floor_hour(target) - timedelta(hours=lag_hours))


def anomaly_features(frequency_hz: float, voltage_pu: float, load_pct: float, demand_delta_pct: float) -> list[float]:
    return [frequency_hz - 50.0, voltage_pu - 1.0, load_pct / 100.0, demand_delta_pct / 100.0]


ANOMALY_FEATURES = ("freq_dev_hz", "voltage_dev_pu", "load_frac", "demand_delta_frac")
