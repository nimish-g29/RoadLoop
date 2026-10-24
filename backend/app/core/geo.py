"""Geohash + distance helpers. Pure Python, no dependencies.

Dedupe works in two stages:
  1. bucket: candidate hotspots share the detection's precision-7 geohash cell
     or one of its 8 neighbours (cells are ~150 m x 150 m in India);
  2. exact: haversine distance to the candidate is within MERGE_RADIUS_M.
Stage 1 is a cheap indexed string match; stage 2 removes cell-edge false matches.
"""
from __future__ import annotations

import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(_BASE32)}
EARTH_R_M = 6_371_008.8


def encode(lat: float, lon: float, precision: int = 7) -> str:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"invalid coordinate ({lat}, {lon})")
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out, bits, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch, lon_lo = (ch << 1) | 1, mid
            else:
                ch, lon_hi = ch << 1, mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch, lat_lo = (ch << 1) | 1, mid
            else:
                ch, lat_hi = ch << 1, mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_BASE32[ch])
            bits, ch = 0, 0
    return "".join(out)


def bbox(gh: str) -> tuple[float, float, float, float]:
    """Return (lat_lo, lat_hi, lon_lo, lon_hi) of a geohash cell."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for c in gh:
        v = _DECODE[c]
        for shift in range(4, -1, -1):
            bit = (v >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                lon_lo, lon_hi = (mid, lon_hi) if bit else (lon_lo, mid)
            else:
                mid = (lat_lo + lat_hi) / 2
                lat_lo, lat_hi = (mid, lat_hi) if bit else (lat_lo, mid)
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def decode(gh: str) -> tuple[float, float]:
    lat_lo, lat_hi, lon_lo, lon_hi = bbox(gh)
    return (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2


def neighbours(gh: str) -> list[str]:
    """The cell itself plus its 8 neighbours (deduplicated, poles clamped)."""
    lat_lo, lat_hi, lon_lo, lon_hi = bbox(gh)
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    clat, clon = (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2
    cells = []
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            la = clat + i * dlat
            if not -90 <= la <= 90:
                continue
            lo = ((clon + j * dlon + 180) % 360) - 180
            c = encode(la, lo, len(gh))
            if c not in cells:
                cells.append(c)
    return cells


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))
