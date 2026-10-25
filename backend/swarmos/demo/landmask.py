"""Land/water mask for placing simulated points (facilities, incidents, sensors) on land, inside the city.

Water polygons come from the OpenStreetMap-derived vector tiles that the map itself uses (OpenFreeMap, OpenMapTiles
schema, layer ``water``: sea, lakes, rivers). Tiles at zoom 10 covering the city are decoded with a small built-in
Mapbox Vector Tile reader and rasterised to a ~150 m grid, so a lookup is O(1). If the tiles cannot be fetched the
mask is ``None`` and callers fall back to bounding-box placement.
"""

from __future__ import annotations

import gzip
import logging
import math
import random
import threading

import httpx
import numpy as np

from swarmos.integrations.mvt import fields as _fields
from swarmos.integrations.mvt import packed as _packed
from swarmos.models import City

log = logging.getLogger("swarmos.landmask")

TILEJSON = "https://tiles.openfreemap.org/planet"
ZOOM = 10
GRID = 256  # cells per tile side (~150 m at zoom 10)
SHORE_MARGIN = 0.0012  # ~130 m: keep simulated markers off the waterline


# --------------------------------------------------------------------------- MVT polygons (reader in integrations.mvt)

def _rings(geometry: list[int]) -> list[list[tuple[int, int]]]:
    """Decode MVT polygon geometry commands into rings of tile coordinates."""
    rings: list[list[tuple[int, int]]] = []
    ring: list[tuple[int, int]] = []
    x = y = 0
    i = 0
    while i < len(geometry):
        cmd, count = geometry[i] & 7, geometry[i] >> 3
        i += 1
        if cmd in (1, 2):
            for _ in range(count):
                dx, dy = geometry[i], geometry[i + 1]
                i += 2
                x += (dx >> 1) ^ -(dx & 1)
                y += (dy >> 1) ^ -(dy & 1)
                if cmd == 1 and ring:
                    rings.append(ring)
                    ring = []
                ring.append((x, y))
        elif cmd == 7:
            if ring:
                rings.append(ring)
                ring = []
    if ring:
        rings.append(ring)
    return rings


def water_rings(tile: bytes, layer_name: str = "water") -> tuple[list[list[tuple[int, int]]], int]:
    """All polygon rings of one layer in a vector tile, plus the layer extent."""
    if tile[:2] == b"\x1f\x8b":
        tile = gzip.decompress(tile)
    for field, _, layer in _fields(tile):
        if field != 3:
            continue
        name, extent, feats = None, 4096, []
        for f, _, v in _fields(layer):
            if f == 1:
                name = v.decode()
            elif f == 5:
                extent = v
            elif f == 2:
                feats.append(v)
        if name != layer_name:
            continue
        rings: list[list[tuple[int, int]]] = []
        for feat in feats:
            gtype, geom = 0, b""
            for f, _, v in _fields(feat):
                if f == 3:
                    gtype = v
                elif f == 4:
                    geom = v
            if gtype == 3 and geom:
                rings.extend(_rings(_packed(geom)))
        return rings, extent
    return [], 4096


def rasterise(rings: list[list[tuple[int, int]]], extent: int, grid: int = GRID) -> np.ndarray:
    """Even-odd scanline fill of polygon rings into a boolean grid[row, col] (row 0 = top of the tile)."""
    mask = np.zeros((grid, grid), dtype=bool)
    edges = [(a, b) for r in rings if len(r) >= 3 for a, b in zip(r, r[1:] + r[:1], strict=True)]
    if not edges:
        return mask
    e = np.array([[a[0], a[1], b[0], b[1]] for a, b in edges], dtype=np.float64)
    x0, y0, x1, y1 = e[:, 0], e[:, 1], e[:, 2], e[:, 3]
    cell = extent / grid
    centres = (np.arange(grid) + 0.5) * cell
    for row, yc in enumerate(centres):
        hit = ((y0 <= yc) & (yc < y1)) | ((y1 <= yc) & (yc < y0))
        if not hit.any():
            continue
        xs = np.sort(x0[hit] + (yc - y0[hit]) * (x1[hit] - x0[hit]) / (y1[hit] - y0[hit]))
        for xa, xb in zip(xs[0::2], xs[1::2], strict=False):
            c0 = max(0, math.ceil(xa / cell - 0.5))
            c1 = min(grid, math.floor(xb / cell - 0.5) + 1)
            if c1 > c0:
                mask[row, c0:c1] = True
    return mask


# --------------------------------------------------------------------------- mask

def _tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lat = max(-85.0, min(85.0, lat))
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


class LandMask:
    def __init__(self, tiles: dict[tuple[int, int], np.ndarray], zoom: int = ZOOM) -> None:
        self.tiles = tiles
        self.zoom = zoom

    def is_water(self, lat: float, lon: float) -> bool:
        x, y = _tile_xy(lat, lon, self.zoom)
        grid = self.tiles.get((int(x), int(y)))
        if grid is None:
            return False
        n = grid.shape[0]
        return bool(grid[min(n - 1, int((y % 1) * n)), min(n - 1, int((x % 1) * n))])

    def water_share(self) -> float:
        cells = sum(g.size for g in self.tiles.values())
        return float(sum(int(g.sum()) for g in self.tiles.values())) / cells if cells else 0.0


_cache: dict[str, LandMask | None] = {}
_lock = threading.Lock()


def land_mask(city: City, timeout: float = 15.0) -> LandMask | None:
    """Water mask for the city's viewport (cached per process). None when the tiles are unavailable."""
    with _lock:
        if city.id in _cache:
            return _cache[city.id]
    min_lon, min_lat, max_lon, max_lat = city.bbox or [city.lon - 0.2, city.lat - 0.2, city.lon + 0.2, city.lat + 0.2]
    x0, y0 = _tile_xy(max_lat, min_lon, ZOOM)
    x1, y1 = _tile_xy(min_lat, max_lon, ZOOM)
    mask: LandMask | None = None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "SwarmOS/0.1 (+simulation)"}) as client:
            template = client.get(TILEJSON).json()["tiles"][0]
            tiles: dict[tuple[int, int], np.ndarray] = {}
            for tx in range(int(x0), int(x1) + 1):
                for ty in range(int(y0), int(y1) + 1):
                    r = client.get(template.format(z=ZOOM, x=tx, y=ty))
                    if r.status_code == 204 or not r.content:
                        tiles[(tx, ty)] = np.zeros((GRID, GRID), dtype=bool)
                        continue
                    r.raise_for_status()
                    rings, extent = water_rings(r.content)
                    tiles[(tx, ty)] = rasterise(rings, extent)
            mask = LandMask(tiles)
            log.info("land_mask_built", extra={"city": city.id, "tiles": len(tiles), "water_share": round(mask.water_share(), 3)})
    except Exception as exc:  # network or format problem: place points by bounding box only
        log.warning("land_mask_unavailable", extra={"city": city.id, "error_type": type(exc).__name__})
        return None  # not cached, so the next seeding tries again
    with _lock:
        _cache[city.id] = mask
    return mask


class Placer:
    """Random points inside the city's bounding box, concentrated around the centre, never on water."""

    def __init__(self, city: City, rng: random.Random, mask: LandMask | None) -> None:
        self.city, self.rng, self.mask = city, rng, mask
        self.bbox = city.bbox or [city.lon - 0.15, city.lat - 0.15, city.lon + 0.15, city.lat + 0.15]

    def _on_land(self, lat: float, lon: float, margin: float) -> bool:
        """The point and its neighbours `margin` degrees away are land, so markers do not sit on the shoreline."""
        if self.mask is None:
            return True
        offsets = [(0.0, 0.0)] + ([(margin, 0.0), (-margin, 0.0), (0.0, margin), (0.0, -margin)] if margin else [])
        return not any(self.mask.is_water(lat + a, lon + b) for a, b in offsets)

    def point(self, spread: float = 0.2) -> tuple[float, float]:
        """`spread` is the standard deviation as a fraction of the bounding-box size."""
        min_lon, min_lat, max_lon, max_lat = self.bbox
        sx, sy = (max_lon - min_lon) * spread, (max_lat - min_lat) * spread
        fallback: tuple[float, float] | None = None
        lat = lon = 0.0
        for attempt in range(80):
            grow = 1 + attempt / 20  # widen the search if the centre is surrounded by water
            lon = round(min(max_lon, max(min_lon, self.rng.gauss(self.city.lon, sx * grow))), 5)
            lat = round(min(max_lat, max(min_lat, self.rng.gauss(self.city.lat, sy * grow))), 5)
            if self._on_land(lat, lon, SHORE_MARGIN):
                return lat, lon
            if fallback is None and self._on_land(lat, lon, 0.0):
                fallback = (lat, lon)
        return fallback or (lat, lon)

    def near(self, lat: float, lon: float, metres: float) -> tuple[float, float]:
        """A land point within roughly `metres` of (lat, lon) — used for clustered incidents."""
        d = metres / 111_000
        for _ in range(40):
            la, lo = round(lat + self.rng.uniform(-d, d), 5), round(lon + self.rng.uniform(-d, d), 5)
            if self._on_land(la, lo, 0.0):
                return la, lo
        return round(lat, 5), round(lon, 5)
