"""Best-effort public OSM routing plus transparent, local context counts.

The route provider receives coordinates, but SwarmOS does not persist route requests or route histories.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Literal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from swarmos.cities import haversine_km
from swarmos.core.errors import ApiError, not_found
from swarmos.core.ratelimit import limiter
from swarmos.integrations.base import IntegrationError
from swarmos.integrations.http import request_json
from swarmos.models import City, GeoFeature, Incident

Mode = Literal["foot", "bike", "car"]
HELP_LAYERS = ("hospital", "clinic", "police", "fire_station")
TRANSIT_LAYERS = ("bus_station", "rail_station")
STREET_LIGHT_LAYERS = ("street_light",)
CONTEXT_RADIUS_KM = 0.5
INCIDENT_RADIUS_KM = 0.25
STREET_LIGHT_RADIUS_KM = 0.1
MAX_PUBLIC_FEATURE_EVIDENCE = 25
MAX_ROUTE_CONTEXT_INCIDENTS = 2000
MAX_ROUTE_CONTEXT_FEATURES = 3000


def _near_route(lat: float, lon: float, line: list[list[float]], radius_km: float) -> bool:
    """Check distance to sampled route segments (context only, not turn-by-turn matching)."""
    if not line:
        return False
    return _distance_to_route_km(lat, lon, line) <= radius_km


def _distance_to_route_km(lat: float, lon: float, line: list[list[float]]) -> float:
    if not line:
        return math.inf
    step = max(1, math.ceil(len(line) / 250))
    samples = line[::step]
    if samples[-1] != line[-1]:
        samples.append(line[-1])
    if len(samples) == 1:
        return haversine_km(lat, lon, samples[0][1], samples[0][0])

    # Project this short city-scale search into a local tangent plane, then measure
    # point-to-segment distance. Measuring only vertex distance can miss a nearby
    # feature halfway along a long straight segment.
    km_per_lat_degree = 110.574
    km_per_lon_degree = 111.320 * math.cos(math.radians(lat))
    min_distance_sq = math.inf
    for first, second in zip(samples, samples[1:], strict=False):
        x1, y1 = (first[0] - lon) * km_per_lon_degree, (first[1] - lat) * km_per_lat_degree
        x2, y2 = (second[0] - lon) * km_per_lon_degree, (second[1] - lat) * km_per_lat_degree
        dx, dy = x2 - x1, y2 - y1
        segment_length_sq = dx * dx + dy * dy
        if segment_length_sq == 0:
            distance_sq = x1 * x1 + y1 * y1
        else:
            projection = max(0.0, min(1.0, -(x1 * dx + y1 * dy) / segment_length_sq))
            nearest_x, nearest_y = x1 + projection * dx, y1 + projection * dy
            distance_sq = nearest_x * nearest_x + nearest_y * nearest_y
        min_distance_sq = min(min_distance_sq, distance_sq)
    return math.sqrt(min_distance_sq)


def _context(db: Session, city_id: str, geometry: list[list[float]]) -> dict[str, Any]:
    if not geometry:
        return {"open_incident_points_within_250m": 0, "mapped_help_facilities_within_500m": {},
                "mapped_transit_stations_within_500m": {}, "facility_features_checked": 0,
                "transit_features_checked": 0, "street_light_features_checked": 0,
                "mapped_street_lamp_points_within_100m": 0,
                "incident_candidates_checked": 0, "incident_candidate_cap": MAX_ROUTE_CONTEXT_INCIDENTS,
                "incident_candidates_truncated": False,
                "feature_candidates_checked": 0, "feature_candidate_cap": MAX_ROUTE_CONTEXT_FEATURES,
                "feature_candidates_truncated": False, "context_truncated": False,
                "mapped_wheelchair_accessibility_within_500m": {
                    "yes": 0, "limited": 0, "no": 0, "designated": 0, "unknown": 0,
                },
                "wheelchair_features_near_route": 0, "mapped_feature_evidence": [],
                "mapped_feature_evidence_truncated": False, "provenance": []}
    lons, lats = [point[0] for point in geometry], [point[1] for point in geometry]
    min_lon, max_lon, min_lat, max_lat = min(lons) - 0.01, max(lons) + 0.01, min(lats) - 0.01, max(lats) + 0.01
    incidents = db.scalars(select(Incident).where(
        Incident.city_id == city_id, Incident.status.in_({"open", "acknowledged", "in_progress"}),
        Incident.lat.is_not(None), Incident.lon.is_not(None),
        Incident.lat.between(min_lat, max_lat), Incident.lon.between(min_lon, max_lon),
    ).order_by(Incident.reported_at.desc(), Incident.id.asc()).limit(MAX_ROUTE_CONTEXT_INCIDENTS + 1)).all()
    incident_candidates_truncated = len(incidents) > MAX_ROUTE_CONTEXT_INCIDENTS
    incidents = incidents[:MAX_ROUTE_CONTEXT_INCIDENTS]
    features = db.scalars(select(GeoFeature).where(
        GeoFeature.city_id == city_id, GeoFeature.layer.in_((*HELP_LAYERS, *TRANSIT_LAYERS, *STREET_LIGHT_LAYERS)),
        GeoFeature.provenance.in_({"live", "cached"}),
        GeoFeature.lat.between(min_lat, max_lat), GeoFeature.lon.between(min_lon, max_lon),
    ).order_by(GeoFeature.id.asc()).limit(MAX_ROUTE_CONTEXT_FEATURES + 1)).all()
    feature_candidates_truncated = len(features) > MAX_ROUTE_CONTEXT_FEATURES
    features = features[:MAX_ROUTE_CONTEXT_FEATURES]
    incidents_near = sum(1 for row in incidents if _near_route(row.lat, row.lon, geometry, INCIDENT_RADIUS_KM))
    near_with_distance = [(row, _distance_to_route_km(row.lat, row.lon, geometry)) for row in features]
    near_with_distance = [(row, distance) for row, distance in near_with_distance if distance <= CONTEXT_RADIUS_KM]
    near = [row for row, _distance in near_with_distance]
    facilities = Counter(row.layer for row in near if row.layer in HELP_LAYERS)
    transit = Counter(row.layer for row in near if row.layer in TRANSIT_LAYERS)
    street_lights = sum(
        1 for row, distance in near_with_distance
        if row.layer in STREET_LIGHT_LAYERS and distance <= STREET_LIGHT_RADIUS_KM
    )
    accessibility_features = [row for row in near if row.layer in (*HELP_LAYERS, *TRANSIT_LAYERS)]
    wheelchair = Counter()
    for row in accessibility_features:
        value = row.properties.get("wheelchair") if isinstance(row.properties, dict) else None
        value = str(value).strip().lower() if value is not None else ""
        wheelchair[value if value in {"yes", "limited", "no", "designated"} else "unknown"] += 1
    public_features = sorted(
        ((row, distance) for row, distance in near_with_distance
         if row.layer in (*HELP_LAYERS, *TRANSIT_LAYERS, *STREET_LIGHT_LAYERS)),
        key=lambda item: (item[1], item[0].layer, item[0].external_id),
    )
    mapped_feature_evidence = []
    for row, distance in public_features[:MAX_PUBLIC_FEATURE_EVIDENCE]:
        props = row.properties if isinstance(row.properties, dict) else {}
        mapped_feature_evidence.append({
            "source_id": row.source_id,
            "external_id": row.external_id,
            "name": row.name,
            "layer": row.layer,
            "provenance": row.provenance,
            "distance_to_sampled_route_m": round(distance * 1000),
            "wheelchair": props.get("wheelchair"),
            "observed_at": row.observed_at.isoformat() if row.observed_at else None,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        })
    return {"open_incident_points_within_250m": incidents_near,
            "mapped_help_facilities_within_500m": dict(sorted(facilities.items())),
            "mapped_transit_stations_within_500m": dict(sorted(transit.items())),
            "facility_features_checked": sum(1 for row in features if row.layer in HELP_LAYERS),
            "transit_features_checked": sum(1 for row in features if row.layer in TRANSIT_LAYERS),
            "street_light_features_checked": sum(1 for row in features if row.layer in STREET_LIGHT_LAYERS),
            "mapped_street_lamp_points_within_100m": street_lights,
            "incident_candidates_checked": len(incidents),
            "incident_candidate_cap": MAX_ROUTE_CONTEXT_INCIDENTS,
            "incident_candidates_truncated": incident_candidates_truncated,
            "feature_candidates_checked": len(features),
            "feature_candidate_cap": MAX_ROUTE_CONTEXT_FEATURES,
            "feature_candidates_truncated": feature_candidates_truncated,
            "context_truncated": incident_candidates_truncated or feature_candidates_truncated,
            "mapped_wheelchair_accessibility_within_500m": {
                key: wheelchair[key] for key in ("yes", "limited", "no", "designated", "unknown")
            },
            "wheelchair_features_near_route": len(accessibility_features),
            "mapped_feature_evidence": mapped_feature_evidence,
            "mapped_feature_evidence_truncated": (
                len(public_features) > MAX_PUBLIC_FEATURE_EVIDENCE or feature_candidates_truncated
            ),
            "provenance": sorted({row.provenance for row in near})}


async def compare_routes(db: Session, city_id: str, origin: tuple[float, float], destination: tuple[float, float],
                         mode: Mode, user_agent: str) -> dict[str, Any]:
    city = db.get(City, city_id)
    if city is None:
        raise not_found("City")
    for lat, lon in (origin, destination):
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or haversine_km(city.lat, city.lon, lat, lon) > 150:
            raise ApiError(422, "outside_city_scope", "Route endpoints must be within 150 km of the selected city")
    # FOSSGIS asks clients to stay at or below one request per second; this process-wide guard is best effort.
    limiter.check("sakhi-route:fossgis-upstream", 1, window_s=1)
    profile = "bike" if mode == "bike" else mode
    # FOSSGIS chooses the profile from the routed-{profile} prefix; its OSRM path segment stays "driving".
    path_profile = "driving"
    coordinates = f"{origin[1]:.6f},{origin[0]:.6f};{destination[1]:.6f},{destination[0]:.6f}"
    url = f"https://routing.openstreetmap.de/routed-{profile}/route/v1/{path_profile}/{coordinates}"
    try:
        async with httpx.AsyncClient(headers={"User-Agent": user_agent, "Accept": "application/json"},
                                     follow_redirects=False) as client:
            payload = await request_json(client, "GET", url,
                                         params={"alternatives": 3, "steps": "false", "overview": "simplified",
                                                 "geometries": "geojson"},
                                         timeout=12, retries=0)
    except (IntegrationError, httpx.HTTPError, TimeoutError) as exc:
        # Keep vendor/network details out of public errors.
        raise ApiError(503, "routing_unavailable", "The open routing service is temporarily unavailable") from exc
    if not isinstance(payload, dict) or payload.get("code") != "Ok" or not isinstance(payload.get("routes"), list):
        raise ApiError(422, "no_route", "No route was returned for these endpoints")
    routes = []
    for index, route in enumerate(payload["routes"][:3]):
        geometry_obj = route.get("geometry") if isinstance(route, dict) else None
        coords = geometry_obj.get("coordinates") if isinstance(geometry_obj, dict) else None
        if not isinstance(coords, list) or len(coords) < 2 or len(coords) > 10000:
            continue
        try:
            line = [[float(point[0]), float(point[1])] for point in coords
                    if isinstance(point, list) and len(point) >= 2]
        except (TypeError, ValueError):
            continue
        if len(line) < 2 or any(not (-180 <= x <= 180 and -90 <= y <= 90) for x, y in line):
            continue
        distance_m, duration_s = route.get("distance"), route.get("duration")
        if (not isinstance(distance_m, (int, float)) or not isinstance(duration_s, (int, float))
                or not math.isfinite(distance_m) or not math.isfinite(duration_s)
                or distance_m <= 0 or duration_s <= 0):
            continue
        routes.append({"id": f"route-{index + 1}", "distance_m": round(distance_m),
                       "duration_s": round(duration_s), "geometry": {"type": "LineString", "coordinates": line},
                       "context": _context(db, city_id, line)})
    if not routes:
        raise ApiError(502, "invalid_route_response", "The routing service returned no usable route geometry")
    limitations = [
        "Routes are network paths, not a safety certification or guarantee of personal safety.",
        "Incident and facility counts are mapped records near a sampled route; incomplete mapping or zero counts do not mean a location is safe or a facility is absent.",
        "Transit counts include mapped bus and rail stations only; they do not show routes, service frequency, hours, or current operation.",
        "Wheelchair values are community-mapped facility tags near sampled route points; unknown means no recognized tag was present and does not establish accessibility.",
        "The route itself has not been checked for curb ramps, surface, slope, width, crossings, or step-free continuity.",
        "Mapped street-lamp points are community-mapped OSM infrastructure within 100 m; their count does not verify that lamps work, are switched on, or illuminate the route, and zero does not mean lamps are absent.",
        "This public best-effort routing service has no uptime or data-freshness guarantee and may log submitted coordinates.",
        "No origin, destination, or route history is stored by SwarmOS.",
        "The router estimates distance and time from map data; it does not provide live traffic or transit availability.",
    ]
    if any(route["context"]["context_truncated"] for route in routes):
        limitations.append(
            "At least one route's nearby-record scan reached its candidate cap; that route's context counts and evidence are incomplete lower bounds."
        )
    return {"city_id": city_id, "mode": mode, "routes": routes,
            "source": "FOSSGIS OSRM over OpenStreetMap data",
            "attribution": "© OpenStreetMap contributors · Routing via OSRM/FOSSGIS",
            "source_url": "https://routing.openstreetmap.de/about.html",
            "limitations": limitations}
