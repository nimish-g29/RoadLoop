"""Which authority owns a road? A cheap rule pass first; TinyFish Search only when unsure.

The phone sends the road name and ref from its reverse geocoder (Android/iOS
geocoders and OSM tags expose refs like "NH 44" or "SH 78"). Ownership in India:
  * NH-xx  -> NHAI (or MoRTH / state PWD NH wing on some stretches)
  * SH-xx  -> state PWD
  * else   -> the city's municipal corporation, if the point is inside a known city
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NH = re.compile(r"\b(NH|national\s+highway)\s*[-]?\s*\d+", re.I)
_SH = re.compile(r"\b(SH|state\s+highway)\s*[-]?\s*\d+", re.I)


@dataclass(frozen=True)
class Resolution:
    authority_key: str | None   # key into portals.json, None if unknown
    confidence: float           # 0..1
    reason: str
    search_query: str | None    # set when TinyFish Search should confirm


def road_class_from_ref(road_ref: str | None, road_name: str | None) -> str:
    text = f"{road_ref or ''} {road_name or ''}"
    if _NH.search(text):
        return "national"
    if _SH.search(text):
        return "state"
    if re.search(r"\b(main|ring|outer|highway|expressway|marg)\b", text, re.I):
        return "arterial"
    return "local"


def resolve_authority(road_ref: str | None, road_name: str | None, city_key: str | None,
                      state_key: str | None) -> Resolution:
    text = f"{road_ref or ''} {road_name or ''}".strip()
    if _NH.search(text):
        return Resolution("nhai", 0.85, "national highway ref", None)
    if _SH.search(text) and state_key:
        return Resolution(f"pwd_{state_key}", 0.8, "state highway ref", None)
    if city_key:
        # City roads are usually the corporation's, but arterials are sometimes PWD-owned.
        conf = 0.75 if road_class_from_ref(road_ref, road_name) == "local" else 0.55
        q = None if conf >= 0.7 else f"who maintains {road_name or text} {city_key} road repair authority"
        return Resolution(f"city_{city_key}", conf, "inside city limits", q)
    return Resolution(None, 0.0, "unknown", f"who maintains {text or 'this road'} road repair complaint")
