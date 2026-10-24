"""Reverse geocoding (lat/lon -> road name, ref, address, city, state) via OpenStreetMap Nominatim.

Nominatim's usage policy: identify yourself with a User-Agent, max 1 request/second, cache results.
We only geocode once per NEW hotspot, so volume stays tiny. For production, self-host Nominatim.
"""
from __future__ import annotations

import re
import time

import requests

NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
_last_call = 0.0


def _slug(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or None


def parse_nominatim(data: dict) -> dict:
    """Pure function: Nominatim jsonv2 reverse response -> our place fields."""
    if not data or "error" in data:
        return {}
    a = data.get("address", {}) or {}
    names = data.get("namedetails", {}) or {}
    extra = data.get("extratags", {}) or {}
    road = a.get("road") or a.get("pedestrian") or a.get("residential")
    ref = names.get("ref") or extra.get("ref")
    if not ref and road and re.match(r"^(NH|SH)\s*-?\s*\d+", road, re.I):
        ref = road
    city = a.get("city") or a.get("town") or a.get("municipality") or a.get("county") or a.get("state_district")
    iso = a.get("ISO3166-2-lvl4") or ""            # e.g. "IN-DL", "IN-BR"
    state_key = iso.split("-", 1)[1].lower() if iso.startswith("IN-") else None
    parts = [road, a.get("neighbourhood") or a.get("suburb") or a.get("quarter"), city]
    address = ", ".join(p for p in dict.fromkeys(parts) if p) or data.get("display_name")
    city_key = _slug(city)
    # Common aliases so portals.json keys stay short.
    city_key = {"new_delhi": "delhi", "south_delhi": "delhi", "patna_rural": "patna"}.get(city_key, city_key)
    return {"road_name": road, "road_ref": ref, "address": address[:300] if address else None,
            "city_key": city_key, "state_key": state_key}


def reverse(lat: float, lon: float, user_agent: str, timeout: float = 8.0) -> dict:
    global _last_call
    wait = 1.05 - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    r = requests.get(NOMINATIM, timeout=timeout, headers={"User-Agent": user_agent},
                     params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17, "addressdetails": 1,
                             "namedetails": 1, "extratags": 1, "accept-language": "en"})
    r.raise_for_status()
    return parse_nominatim(r.json())
