"""Deterministic SYNTHETIC time-series generators (used for training fixtures and demo mode).

Patterns are plausible in shape only (diurnal/weekly cycles, noise, injected anomalies). The numbers carry
no real-world meaning and must always be labelled synthetic/demo.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from swarmos.ml.features import IST


def _hours(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(hours=h) for h in range(n)]


def _gauss(hour: float, mu: float, sigma: float) -> float:
    d = min(abs(hour - mu), 24 - abs(hour - mu))
    return math.exp(-(d**2) / (2 * sigma**2))


def energy_demand(start: datetime, n: int, seed: int, base_mw: float = 3000.0) -> tuple[list[datetime], np.ndarray]:
    rng = np.random.default_rng(seed)
    times = _hours(start, n)
    vals = []
    for i, t in enumerate(times):
        lt = t + IST
        h = lt.hour
        daily = 0.78 + 0.18 * _gauss(h, 10, 2.5) + 0.26 * _gauss(h, 20, 2.2) - 0.08 * _gauss(h, 3.5, 2.5)
        weekly = 0.93 if lt.weekday() >= 5 else 1.0
        seasonal = 1 + 0.06 * math.sin(2 * math.pi * (i / 24) / 120)
        vals.append(base_mw * daily * weekly * seasonal)
    arr = np.array(vals) * (1 + rng.normal(0, 0.025, n))
    return times, arr


def water_demand(start: datetime, n: int, seed: int, base_m3h: float = 60000.0) -> tuple[list[datetime], np.ndarray]:
    rng = np.random.default_rng(seed)
    times = _hours(start, n)
    vals = []
    for t in times:
        lt = t + IST
        h = lt.hour
        daily = 0.45 + 0.75 * _gauss(h, 7, 1.6) + 0.45 * _gauss(h, 19, 1.8) + 0.15 * _gauss(h, 13, 2.5)
        weekly = 1.05 if lt.weekday() >= 5 else 1.0
        vals.append(base_m3h * daily * weekly)
    return times, np.array(vals) * (1 + rng.normal(0, 0.03, n))


def corridor_speed(start: datetime, n: int, seed: int, freeflow: float = 45.0) -> tuple[list[datetime], np.ndarray]:
    rng = np.random.default_rng(seed)
    times = _hours(start, n)
    vals = []
    for t in times:
        lt = t + IST
        h = lt.hour
        weekend = lt.weekday() >= 5
        dip = 0.55 * _gauss(h, 9.5, 1.5) + 0.62 * _gauss(h, 19, 1.8) + 0.25 * _gauss(h, 14, 2.5)
        if weekend:
            dip *= 0.5
        vals.append(freeflow * (1 - dip) + 2)
    arr = np.clip(np.array(vals) + rng.normal(0, 2.0, n), 4, freeflow * 1.05)
    return times, arr


def dma_flow(start: datetime, n: int, seed: int, base: float = 300.0, leak_from_hour: int | None = None,
             leak_m3h: float = 0.0) -> tuple[list[datetime], np.ndarray]:
    rng = np.random.default_rng(seed)
    times, demand = water_demand(start, n, seed, base_m3h=base)
    arr = demand + base * 0.12  # background losses
    if leak_from_hour is not None:
        arr[leak_from_hour:] += leak_m3h
    return times, arr * (1 + rng.normal(0, 0.02, n))


def grid_telemetry(start: datetime, n: int, seed: int, anomaly_rate: float = 0.03) -> dict[str, np.ndarray | list]:
    """Frequency, feeder voltage/load and demand with labelled injected anomalies (for detector training)."""
    rng = np.random.default_rng(seed)
    times, demand = energy_demand(start, n, seed + 1, base_mw=1000)
    freq = 50.0 + rng.normal(0, 0.02, n)
    volt = 1.0 + rng.normal(0, 0.012, n)
    load = 55 + 25 * (demand - demand.min()) / (demand.max() - demand.min()) + rng.normal(0, 3, n)
    delta = np.concatenate([[0.0], np.diff(demand) / demand[:-1] * 100])
    labels = np.zeros(n, dtype=int)
    idx = rng.choice(n, size=max(1, int(n * anomaly_rate)), replace=False)
    for i in idx:
        kind = rng.integers(0, 3)
        if kind == 0:
            freq[i] += rng.choice([-1, 1]) * rng.uniform(0.15, 0.4)
        elif kind == 1:
            volt[i] += rng.choice([-1, 1]) * rng.uniform(0.06, 0.12)
        else:
            load[i] = rng.uniform(102, 125)
            delta[i] += rng.uniform(8, 20)
        labels[i] = 1
    return {"times": times, "frequency_hz": freq, "voltage_pu": volt, "load_pct": load, "demand_delta_pct": delta, "label": labels}
