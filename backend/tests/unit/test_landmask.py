"""Land/water mask: vector-tile decoding, rasterising and land-only placement of simulated points."""

from __future__ import annotations

import gzip
import random
from types import SimpleNamespace

import numpy as np

from swarmos.demo.landmask import GRID, LandMask, Placer, _tile_xy, rasterise, water_rings


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, payload: bytes) -> bytes:
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


def _zig(v: int) -> int:
    return (v << 1) ^ (v >> 31)


def _tile_with_square(x0: int, y0: int, size: int, layer: str = "water") -> bytes:
    """A vector tile with one square polygon in `layer` (extent 4096)."""
    geom = [1 | 1 << 3, _zig(x0), _zig(y0), 2 | 3 << 3, _zig(size), 0, 0, _zig(size), _zig(-size), 0, 7 | 1 << 3]
    feature = _varint(3 << 3) + _varint(3) + _field(4, b"".join(_varint(g) for g in geom))
    body = _field(1, layer.encode()) + _field(2, feature) + _varint(5 << 3) + _varint(4096)
    return _field(3, body)


def test_decode_and_rasterise_square() -> None:
    tile = gzip.compress(_tile_with_square(0, 0, 2048))  # top-left quarter is water
    rings, extent = water_rings(tile)
    assert extent == 4096 and len(rings) == 1 and len(rings[0]) == 4
    mask = rasterise(rings, extent, 64)
    assert mask[:32, :32].all() and not mask[32:, :].any() and not mask[:, 32:].any()
    assert water_rings(_tile_with_square(0, 0, 100, layer="landcover")) == ([], 4096)


def test_placer_keeps_points_inside_bbox_and_off_water() -> None:
    city = SimpleNamespace(id="t", lat=10.0, lon=76.0, bbox=[75.8, 9.8, 76.2, 10.2])
    # Water east of the city centre: every tile cell with longitude > 76.0 is water.
    tiles = {}
    x0, y0 = _tile_xy(10.2, 75.8, 10)
    x1, y1 = _tile_xy(9.8, 76.2, 10)
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            cols = (tx + (np.arange(GRID) + 0.5) / GRID) / 2 ** 10 * 360 - 180
            tiles[(tx, ty)] = np.broadcast_to(cols > 76.0, (GRID, GRID)).copy()
    mask = LandMask(tiles)
    assert mask.is_water(10.0, 76.1) and not mask.is_water(10.0, 75.9)
    placer = Placer(city, random.Random(1), mask)  # type: ignore[arg-type]
    pts = [placer.point(0.2) for _ in range(300)] + [placer.near(10.0, 75.95, 300) for _ in range(50)]
    assert all(75.8 <= lon <= 76.2 and 9.8 <= lat <= 10.2 for lat, lon in pts)
    assert not any(mask.is_water(lat, lon) for lat, lon in pts)
    # Without a mask (tiles unavailable) placement still stays inside the city.
    free = Placer(city, random.Random(2), None)  # type: ignore[arg-type]
    assert all(75.8 <= lon <= 76.2 for _, lon in (free.point() for _ in range(100)))
