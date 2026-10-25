"""Minimal Mapbox Vector Tile (protobuf) reader — enough to read point features and their tags.

Used for OpenStreetMap-derived OpenMapTiles data served by OpenFreeMap (no key, CDN-cached). Kept dependency-free:
the format is a handful of protobuf messages (tile → layers → features with tag indices and geometry commands).
"""

from __future__ import annotations

import gzip
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


def varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield (field number, wire type, value) for one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = varint(buf, i)
        field, wire = key >> 3, key & 7
        val: Any
        if wire == 0:
            val, i = varint(buf, i)
        elif wire == 2:
            ln, i = varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        else:  # groups are not used by MVT
            raise ValueError(f"unsupported wire type {wire}")
        yield field, wire, val


def packed(buf: bytes) -> list[int]:
    out, i = [], 0
    while i < len(buf):
        v, i = varint(buf, i)
        out.append(v)
    return out


def _value(buf: bytes) -> Any:
    for f, _, v in fields(buf):
        if f == 1:
            return v.decode("utf-8", "replace")
        if f in (4, 5):  # int64 / uint64
            return v
        if f == 6:  # sint64
            return (v >> 1) ^ -(v & 1)
        if f == 7:
            return bool(v)
    return None


@dataclass
class PointFeature:
    id: int | None
    x: int
    y: int
    tags: dict[str, Any]


def point_features(tile: bytes, layer_name: str) -> tuple[list[PointFeature], int]:
    """Point features (first MoveTo position) of one layer, with decoded tags, plus the layer extent."""
    if tile[:2] == b"\x1f\x8b":
        tile = gzip.decompress(tile)
    for field, _, layer in fields(tile):
        if field != 3:
            continue
        name, extent, keys, values, feats = None, 4096, [], [], []
        for f, _, v in fields(layer):
            if f == 1:
                name = v.decode()
            elif f == 2:
                feats.append(v)
            elif f == 3:
                keys.append(v.decode())
            elif f == 4:
                values.append(_value(v))
            elif f == 5:
                extent = v
        if name != layer_name:
            continue
        out: list[PointFeature] = []
        for feat in feats:
            fid, gtype, tags, geom = None, 0, [], []
            for f, _, v in fields(feat):
                if f == 1:
                    fid = v
                elif f == 2:
                    tags = packed(v)
                elif f == 3:
                    gtype = v
                elif f == 4:
                    geom = packed(v)
            if gtype != 1 or len(geom) < 3 or geom[0] & 7 != 1:
                continue
            x = (geom[1] >> 1) ^ -(geom[1] & 1)
            y = (geom[2] >> 1) ^ -(geom[2] & 1)
            d = {keys[tags[i]]: values[tags[i + 1]] for i in range(0, len(tags) - 1, 2)
                 if tags[i] < len(keys) and tags[i + 1] < len(values)}
            out.append(PointFeature(fid, x, y, d))
        return out, extent
    return [], 4096


def tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lat = max(-85.0, min(85.0, lat))
    return (lon + 180.0) / 360.0 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def tile_to_lonlat(tx: int, ty: int, px: float, py: float, extent: int, z: int) -> tuple[float, float]:
    n = 2 ** z
    x = (tx + px / extent) / n
    y = (ty + py / extent) / n
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))
    return lon, lat
