"""Configurable city registry. Add cities by editing ``data/cities.json`` and re-running ``swarmos seed``."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from importlib import resources
from typing import Any

from sqlalchemy.orm import Session

from swarmos.models import City


@lru_cache
def load_catalog() -> list[dict[str, Any]]:
    raw = resources.files("swarmos.cities").joinpath("data/cities.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    cities = data["cities"]
    ids = [c["id"] for c in cities]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate city ids in catalog")
    return cities


def approx_viewport(lat: float, lon: float, zoom: float) -> list[float]:
    """Approximate rectangular viewport around a city centre (not an administrative boundary)."""
    half_deg_lat = 0.32 if zoom <= 10.8 else 0.22 if zoom <= 11.5 else 0.12
    half_deg_lon = half_deg_lat / max(0.2, math.cos(math.radians(lat)))
    return [round(lon - half_deg_lon, 4), round(lat - half_deg_lat, 4), round(lon + half_deg_lon, 4), round(lat + half_deg_lat, 4)]


def sync_catalog(db: Session, pilot_ids: list[str]) -> int:
    """Upsert the catalog into the database. Existing rows are updated, never deleted."""
    n = 0
    for c in load_catalog():
        city = db.get(City, c["id"]) or City(id=c["id"])
        city.name = c["name"]
        city.state = c["state"]
        city.lat = c["lat"]
        city.lon = c["lon"]
        city.timezone = c.get("timezone", "Asia/Kolkata")
        city.zoom = c.get("zoom", 11)
        city.bbox = approx_viewport(c["lat"], c["lon"], city.zoom)
        city.population = c.get("population")
        city.population_source = c.get("population_source")
        city.aliases = c.get("aliases", [])
        city.is_pilot = c["id"] in pilot_ids
        city.is_active = True
        db.merge(city)
        n += 1
    return n


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
