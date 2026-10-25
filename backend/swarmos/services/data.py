"""Read-side data access used by the dashboard, analytics and agents. City scope is always explicit."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from swarmos.cities import haversine_km
from swarmos.db import utcnow
from swarmos.models import City, GeoFeature, Incident, Measurement, ServiceRequest


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Point:
    t: datetime
    v: float


@dataclass
class Series:
    metric: str
    unit: str
    source_id: str
    provenance: str
    kind: str
    entity: str
    points: list[Point]
    fetched_at: datetime | None

    @property
    def values(self) -> list[float]:
        return [p.v for p in self.points]

    def ref(self) -> dict[str, Any]:
        return {
            "type": "series",
            "metric": self.metric,
            "entity": self.entity,
            "source_id": self.source_id,
            "provenance": self.provenance,
            "kind": self.kind,
            "n": len(self.points),
            "from": self.points[0].t.isoformat() if self.points else None,
            "to": self.points[-1].t.isoformat() if self.points else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


PROVENANCE_RANK = {"live": 0, "cached": 1, "operator": 2, "derived": 3, "demo": 4, "synthetic": 5}


class CityData:
    """Facade over persisted data for one city. Agents receive this — never a raw session."""

    def __init__(self, db: Session, city: City, now: datetime | None = None) -> None:
        self.db = db
        self.city = city
        self.now = now or utcnow()

    # ---------------------------------------------------------------- measurements
    def series(
        self,
        metric: str,
        *,
        hours_back: float = 48,
        hours_forward: float = 0,
        kinds: tuple[str, ...] = ("observation", "model_analysis"),
        entity: str | None = "city",
        category: str | None = None,
    ) -> list[Series]:
        q = select(Measurement).where(
            Measurement.city_id == self.city.id,
            Measurement.metric == metric,
            Measurement.kind.in_(kinds),
            Measurement.observed_at >= self.now - timedelta(hours=hours_back),
            Measurement.observed_at <= self.now + timedelta(hours=hours_forward),
        )
        if entity is not None:
            q = q.where(Measurement.entity == entity)
        if category is not None:
            q = q.where(Measurement.category == category)
        groups: dict[tuple[str, str, str], list[Measurement]] = defaultdict(list)
        for m in self.db.scalars(q.order_by(Measurement.observed_at)):
            groups[(m.source_id, m.entity, m.kind)].append(m)
        out = []
        for (source_id, ent, kind), rows in groups.items():
            out.append(
                Series(
                    metric=metric, unit=rows[0].unit, source_id=source_id, provenance=rows[0].provenance, kind=kind,
                    entity=ent, points=[Point(aware(r.observed_at), r.value) for r in rows],  # type: ignore[arg-type]
                    fetched_at=max(aware(r.fetched_at) for r in rows),  # type: ignore[type-var]
                )
            )
        return out

    def one_series(self, metric: str, **kw: Any) -> Series | None:
        """Prefer live over demo when both exist; prefer the longest series otherwise."""
        options = self.series(metric, **kw)
        if not options:
            return None
        return sorted(options, key=lambda s: (PROVENANCE_RANK.get(s.provenance, 9), -len(s.points)))[0]

    def forecast_rows(self, metric: str, limit: int = 3) -> list[Measurement]:
        """Upcoming forecast rows from a single source, live sources first (never a mix of live and simulated)."""
        base = select(Measurement).where(
            Measurement.city_id == self.city.id, Measurement.metric == metric, Measurement.kind == "forecast",
            Measurement.observed_at >= self.now - timedelta(hours=24),
        )
        sources = list(self.db.execute(select(Measurement.source_id, Measurement.provenance).where(
            Measurement.city_id == self.city.id, Measurement.metric == metric, Measurement.kind == "forecast",
            Measurement.observed_at >= self.now - timedelta(hours=24)).distinct()))
        if not sources:
            return []
        source_id = sorted(sources, key=lambda r: (PROVENANCE_RANK.get(r[1], 9), r[0]))[0][0]
        return list(self.db.scalars(base.where(Measurement.source_id == source_id).order_by(Measurement.observed_at).limit(limit)))

    def entities(self, category: str, hours_back: float = 72, *, live_first: bool = True) -> list[str]:
        """Entities (corridors, stations, …) with recent data. When a live feed reports any entity, only live
        entities are returned, so live and simulated corridors are never mixed in one analysis."""
        q = select(Measurement.entity).where(
            Measurement.city_id == self.city.id, Measurement.category == category,
            Measurement.observed_at >= self.now - timedelta(hours=hours_back),
        ).distinct()
        if live_first:
            live = sorted(self.db.scalars(q.where(Measurement.provenance.in_(("live", "cached", "operator")))))
            if live:
                return live
        return sorted(self.db.scalars(q))

    def latest(self, metric: str, *, entity: str = "city", category: str | None = None) -> Measurement | None:
        """Most recent value; a live reading from the last 6 hours wins over a newer simulated one."""
        q = select(Measurement).where(
            Measurement.city_id == self.city.id, Measurement.metric == metric, Measurement.entity == entity,
            Measurement.kind.in_(("observation", "model_analysis")), Measurement.observed_at <= self.now,
        )
        if category:
            q = q.where(Measurement.category == category)
        live = self.db.scalars(q.where(Measurement.provenance.in_(("live", "cached", "operator")),
                                       Measurement.observed_at >= self.now - timedelta(hours=6))
                               .order_by(Measurement.observed_at.desc()).limit(1)).first()
        return live or self.db.scalars(q.order_by(Measurement.observed_at.desc()).limit(1)).first()

    def has_category(self, category: str, hours_back: float = 24 * 30) -> bool:
        return bool(
            self.db.scalar(
                select(func.count(Measurement.id)).where(
                    Measurement.city_id == self.city.id, Measurement.category == category,
                    Measurement.observed_at >= self.now - timedelta(hours=hours_back),
                )
            )
        )

    # ---------------------------------------------------------------- features
    def features(self, layers: list[str] | None = None, limit: int = 50000) -> list[GeoFeature]:
        q = select(GeoFeature).where(GeoFeature.city_id == self.city.id)
        if layers:
            q = q.where(GeoFeature.layer.in_(layers))
        return list(self.db.scalars(q.limit(limit)))

    def feature_counts(self) -> dict[str, int]:
        rows = self.db.execute(
            select(GeoFeature.layer, func.count(GeoFeature.id)).where(GeoFeature.city_id == self.city.id).group_by(GeoFeature.layer)
        )
        return {layer: n for layer, n in rows}

    def earthquakes_near(self, radius_km: float = 500, days: float = 7) -> list[tuple[GeoFeature, float]]:
        since = self.now - timedelta(days=days)
        dlat = radius_km / 111.0
        q = select(GeoFeature).where(
            GeoFeature.layer == "earthquake", GeoFeature.city_id.is_(None),
            GeoFeature.lat.between(self.city.lat - dlat, self.city.lat + dlat),
            GeoFeature.observed_at >= since,
        )
        out = []
        for f in self.db.scalars(q):
            d = haversine_km(self.city.lat, self.city.lon, f.lat, f.lon)
            if d <= radius_km:
                out.append((f, d))
        return sorted(out, key=lambda x: x[1])

    # ---------------------------------------------------------------- incidents & requests
    def incidents(self, *, statuses: tuple[str, ...] | None = ("open", "acknowledged", "in_progress"),
                  categories: tuple[str, ...] | None = None, days: float = 30) -> list[Incident]:
        q = select(Incident).where(Incident.city_id == self.city.id,
                                   Incident.reported_at >= self.now - timedelta(days=days))
        if statuses:
            q = q.where(Incident.status.in_(statuses))
        if categories:
            q = q.where(Incident.category.in_(categories))
        return list(self.db.scalars(q.order_by(Incident.reported_at.desc())))

    def service_requests(self, *, days: float = 30, categories: tuple[str, ...] | None = None) -> list[ServiceRequest]:
        q = select(ServiceRequest).where(
            and_(ServiceRequest.city_id == self.city.id, ServiceRequest.created_at >= self.now - timedelta(days=days))
        )
        if categories:
            q = q.where(ServiceRequest.category.in_(categories))
        return list(self.db.scalars(q.order_by(ServiceRequest.created_at.desc())))
