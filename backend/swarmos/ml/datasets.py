"""Dataset generation, manifests (versioning + provenance), schema validation and privacy checks."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swarmos.ml import complaints, synthetic
from swarmos.ml.features import aq_row

PII_PATTERNS = {
    "indian_mobile": re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "aadhaar_like": re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)"),
    "pan_like": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "complaints": {"columns": {"text": "str", "category": "str", "template_id": "str"}, "allowed": {"category": list(complaints.CATEGORIES)},
                   "text_columns": ["text"]},
    "timeseries": {"columns": {"timestamp": "datetime", "value": "float"}, "ranges": {"value": (0, 1e7)}, "text_columns": []},
    "aq_daily": {"columns": {"city": "str", "date": "str", "target_pm25": "float", "pm25_d0": "float", "pm25_d1": "float",
                             "pm25_d2": "float", "pm10_d0": "float", "tmax_next": "float", "tmin_next": "float", "rain_next": "float",
                             "wind_next": "float", "rh_next": "float", "lat": "float", "lon": "float"},
                 "ranges": {"target_pm25": (0, 1500), "pm25_d0": (0, 1500), "tmax_next": (-40, 60), "rh_next": (0, 100)},
                 "text_columns": []},
    "grid_telemetry": {"columns": {"timestamp": "datetime", "frequency_hz": "float", "voltage_pu": "float", "load_pct": "float",
                                   "demand_delta_pct": "float", "label": "int"},
                       "ranges": {"frequency_hz": (48.5, 51.5), "voltage_pu": (0.7, 1.3), "label": (0, 1)}, "text_columns": []},
}

GENERATORS = {
    "complaints": lambda p: complaints.synthesize(int(p.get("rows", 2400)), int(p.get("seed", 7))),
    "energy_demand": lambda p: _ts(synthetic.energy_demand, p),
    "water_demand": lambda p: _ts(synthetic.water_demand, p),
    "corridor_speed": lambda p: _ts(synthetic.corridor_speed, p),
    "grid_telemetry": lambda p: _grid(p),
    "kaggle_india_aq": lambda p: _kaggle_india_aq(p),
}

# Kaggle dataset "India Daily Weather & Air Quality" (akshaykarthickms007, CC BY 4.0), read from its official
# Hugging Face mirror because Kaggle downloads require an account token.
KAGGLE_AQ = {
    "kaggle": "https://www.kaggle.com/datasets/akshaykarthickms007/india-daily-weather-and-air-quality",
    "mirror": "https://huggingface.co/datasets/AkshayKarthick/india-weather-air-quality/resolve/main/",
    "license": "CC BY 4.0 (Kaggle: akshaykarthickms007/india-daily-weather-and-air-quality)",
}


def _download_csv(url: str) -> list[dict[str, str]]:
    import httpx

    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "SwarmOS/0.1 (+ml training)"}) as client:
        r = client.get(url)
        r.raise_for_status()
    return list(csv.DictReader(r.text.splitlines()))


def _kaggle_india_aq(p: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = p.get("mirror", KAGGLE_AQ["mirror"])
    aq = _download_csv(base + "air_quality_daily.csv")
    wx = _download_csv(base + "weather_daily.csv")
    coords = {r["city"]: (float(r["latitude"]), float(r["longitude"])) for r in _download_csv(base + "cities.csv")}
    weather = {(r["city"], r["date"]): r for r in wx}
    by_city: dict[str, dict[str, dict[str, str]]] = {}
    for r in aq:
        by_city.setdefault(r["city"], {})[r["date"]] = r
    rows: list[dict[str, Any]] = []
    for city, days in by_city.items():
        if city not in coords:
            continue
        lat, lon = coords[city]
        dates = sorted(days)
        for i in range(2, len(dates) - 1):
            d2, d1, d0, nxt = dates[i - 2], dates[i - 1], dates[i], dates[i + 1]
            day = datetime.fromisoformat(d0)
            if (datetime.fromisoformat(nxt) - day).days != 1 or (day - datetime.fromisoformat(d2)).days != 2:
                continue
            w = weather.get((city, nxt))
            try:
                pm = [float(days[d0]["pm2_5_mean"]), float(days[d1]["pm2_5_mean"]), float(days[d2]["pm2_5_mean"])]
                target = float(days[nxt]["pm2_5_mean"])
                pm10 = float(days[d0]["pm10_mean"])
                if w is None:
                    continue
                wn = {"tmax": float(w["temperature_2m_max"]), "tmin": float(w["temperature_2m_min"]), "rain": float(w["precipitation_sum"]),
                      "wind": float(w["wind_speed_10m_max"]), "rh": float(w["relative_humidity_2m_mean"])}
            except (KeyError, ValueError):
                continue
            feats = aq_row(pm, pm10, wn, datetime.fromisoformat(nxt).timetuple().tm_yday, lat, lon)
            rows.append({"city": city, "date": nxt, "target_pm25": target, "pm25_d0": feats[0], "pm25_d1": feats[1], "pm25_d2": feats[2],
                         "pm10_d0": feats[3], "tmax_next": feats[4], "tmin_next": feats[5], "rain_next": feats[6], "wind_next": feats[7],
                         "rh_next": feats[8], "lat": lat, "lon": lon})
    rows.sort(key=lambda r: (r["date"], r["city"]))
    meta = {"synthetic": False, "license": KAGGLE_AQ["license"],
            "provenance": f"Real daily observations-derived data for {len(by_city)} Indian cities from Kaggle ({KAGGLE_AQ['kaggle']}), "
                          f"downloaded from the dataset's Hugging Face mirror; joined and lagged by SwarmOS."}
    return rows, meta


def _start(p: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(p.get("start", "2025-01-01T00:00:00+00:00")).astimezone(UTC)


def _ts(fn, p: dict[str, Any]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    times, vals = fn(_start(p), int(p.get("hours", 24 * 120)), int(p.get("seed", 11)))
    return [{"timestamp": t.isoformat(), "value": round(float(v), 4)} for t, v in zip(times, vals, strict=True)]


def _grid(p: dict[str, Any]) -> list[dict[str, Any]]:
    d = synthetic.grid_telemetry(_start(p), int(p.get("hours", 24 * 90)), int(p.get("seed", 23)), float(p.get("anomaly_rate", 0.03)))
    return [{"timestamp": t.isoformat(), "frequency_hz": round(float(d["frequency_hz"][i]), 4), "voltage_pu": round(float(d["voltage_pu"][i]), 4),
             "load_pct": round(float(d["load_pct"][i]), 3), "demand_delta_pct": round(float(d["demand_delta_pct"][i]), 4),
             "label": int(d["label"][i])} for i, t in enumerate(d["times"])]


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Dataset:
    path: Path
    manifest: dict[str, Any]
    rows: list[dict[str, Any]]


def generate(datasets_dir: Path, name: str, generator: str, params: dict[str, Any], schema: str) -> Dataset:
    produced = GENERATORS[generator](params)
    rows, extra = produced if isinstance(produced, tuple) else (produced, {})
    if not rows:
        raise ValueError(f"Dataset generator '{generator}' produced no rows")
    out_dir = datasets_dir / "synthetic" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = out_dir / "data.csv"
    with data_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    manifest = {
        "name": name,
        "synthetic": True,
        "schema": schema,
        "generator": {"name": generator, "params": params, "module": "swarmos.ml.datasets"},
        "rows": len(rows),
        "columns": list(rows[0].keys()),
        "sha256": sha256_path(data_path),
        "license": "CC0-1.0 (synthetic data generated by SwarmOS)",
        "provenance": "Generated deterministically by SwarmOS. Contains no real-world or personal data.",
        "created_at": datetime.now(UTC).isoformat(),
        **extra,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return Dataset(data_path, manifest, rows)


def load(datasets_dir: Path, rel: str) -> Dataset:
    base = datasets_dir / rel
    manifest = json.loads((base / "manifest.json").read_text())
    data_path = base / "data.csv"
    actual = sha256_path(data_path)
    if actual != manifest["sha256"]:
        raise ValueError(f"Dataset {rel} checksum mismatch: manifest {manifest['sha256'][:12]}…, file {actual[:12]}…")
    with data_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return Dataset(data_path, manifest, rows)


def validate(ds: Dataset) -> dict[str, Any]:
    """Schema, range, null, licensing/provenance and privacy checks. Raises on hard failures."""
    schema = SCHEMAS[ds.manifest["schema"]]
    errors: list[str] = []
    for key in ("license", "provenance", "sha256"):
        if not ds.manifest.get(key):
            errors.append(f"manifest missing '{key}'")
    cols = schema["columns"]
    missing = [c for c in cols if c not in (ds.rows[0].keys() if ds.rows else [])]
    if missing:
        errors.append(f"missing columns {missing}")
    nulls = 0
    bad_type = 0
    out_of_range = 0
    for r in ds.rows:
        for c, typ in cols.items():
            v = r.get(c)
            if v is None or v == "":
                nulls += 1
                continue
            try:
                if typ == "float":
                    fv = float(v)
                elif typ == "int":
                    fv = float(int(v))
                elif typ == "datetime":
                    datetime.fromisoformat(v)
                    continue
                else:
                    continue
            except ValueError:
                bad_type += 1
                continue
            lo_hi = schema.get("ranges", {}).get(c)
            if lo_hi and not (lo_hi[0] <= fv <= lo_hi[1]):
                out_of_range += 1
        for c, allowed in schema.get("allowed", {}).items():
            if r.get(c) not in allowed:
                errors.append(f"value {r.get(c)!r} not allowed for {c}")
                break
    if nulls:
        errors.append(f"{nulls} null values")
    if bad_type:
        errors.append(f"{bad_type} type errors")
    if out_of_range:
        errors.append(f"{out_of_range} out-of-range values")
    pii = privacy_scan(ds.rows, schema.get("text_columns", []))
    if pii["total"]:
        errors.append(f"possible personal data detected: {pii['by_pattern']}")
    report = {"rows": len(ds.rows), "errors": errors, "privacy": pii, "synthetic": ds.manifest.get("synthetic", False)}
    if errors:
        raise ValueError("Dataset validation failed: " + "; ".join(errors[:5]))
    return report


def privacy_scan(rows: list[dict[str, Any]], text_columns: list[str]) -> dict[str, Any]:
    counts = {k: 0 for k in PII_PATTERNS}
    for r in rows:
        for c in text_columns:
            text = str(r.get(c, ""))
            for k, pat in PII_PATTERNS.items():
                if pat.search(text):
                    counts[k] += 1
    return {"by_pattern": {k: v for k, v in counts.items() if v}, "total": sum(counts.values())}
