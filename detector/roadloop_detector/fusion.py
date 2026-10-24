"""Sensor fusion: GPS track interpolation, accelerometer jolts, and per-pass debouncing.

A pothole is visible in many consecutive frames while you approach it. We want ONE
detection per pothole per pass, using the frame where it looks biggest (closest), and
the jolt felt when the wheel actually hits it (shortly AFTER it leaves the frame).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class GpsFix:
    t: float          # seconds
    lat: float
    lon: float
    speed_kmh: float | None = None
    accuracy_m: float | None = None


class GpsTrack:
    def __init__(self, fixes: list[GpsFix]):
        if not fixes:
            raise ValueError("empty GPS track")
        self.fixes = sorted(fixes, key=lambda f: f.t)
        self.ts = [f.t for f in self.fixes]

    def at(self, t: float) -> GpsFix:
        i = bisect.bisect_left(self.ts, t)
        if i <= 0:
            return self.fixes[0]
        if i >= len(self.fixes):
            return self.fixes[-1]
        a, b = self.fixes[i - 1], self.fixes[i]
        k = (t - a.t) / (b.t - a.t) if b.t > a.t else 0.0
        speed = None
        if a.speed_kmh is not None and b.speed_kmh is not None:
            speed = a.speed_kmh + k * (b.speed_kmh - a.speed_kmh)
        return GpsFix(t, a.lat + k * (b.lat - a.lat), a.lon + k * (b.lon - a.lon), speed,
                      max(a.accuracy_m or 0, b.accuracy_m or 0) or None)


class JoltSeries:
    """Vertical acceleration in g (phone z-axis, gravity included)."""

    def __init__(self, t: np.ndarray, az_g: np.ndarray, window_s: float = 1.0):
        order = np.argsort(t)
        self.t, az = np.asarray(t, float)[order], np.asarray(az_g, float)[order]
        # High-pass: remove gravity + slow tilt with a rolling median (robust to the spike itself).
        dt = np.median(np.diff(self.t)) if len(self.t) > 1 else 0.02
        k = max(3, int(window_s / max(dt, 1e-3)) | 1)
        pad = k // 2
        padded = np.pad(az, pad, mode="edge")
        base = np.array([np.median(padded[i:i + k]) for i in range(len(az))])
        self.dev = np.abs(az - base)

    def peak(self, t0: float, t1: float) -> float | None:
        lo, hi = np.searchsorted(self.t, t0), np.searchsorted(self.t, t1, side="right")
        if hi <= lo:
            return None
        return float(self.dev[lo:hi].max())


@dataclass
class Track:
    first_t: float
    last_t: float
    best_t: float
    best_area: float
    best_conf: float
    best_box: tuple
    best_frame: object = field(repr=False, default=None)
    hits: int = 1


class PassDebouncer:
    """Group per-frame boxes into potholes; emit one event when a track ends.

    Tracking is deliberately simple: the car moves forward, so a pothole's box grows and
    slides DOWN the frame. A new box continues a track if it's within `gap_s` and its
    centre is close horizontally."""

    def __init__(self, frame_w: int, frame_h: int, gap_s: float = 0.6, min_hits: int = 3, x_tol: float = 0.18):
        self.W, self.H, self.gap_s, self.min_hits, self.x_tol = frame_w, frame_h, gap_s, min_hits, x_tol
        self.tracks: list[Track] = []

    def update(self, t: float, boxes, frame=None) -> list[Track]:
        finished = [tr for tr in self.tracks if t - tr.last_t > self.gap_s]
        self.tracks = [tr for tr in self.tracks if t - tr.last_t <= self.gap_s]
        for b in boxes:
            cx = (b.x1 + b.x2) / 2 / self.W
            area = b.area / (self.W * self.H)
            match = None
            for tr in self.tracks:
                tcx = (tr.best_box[0] + tr.best_box[2]) / 2 / self.W
                if abs(cx - tcx) < self.x_tol:
                    match = tr
                    break
            if match is None:
                self.tracks.append(Track(t, t, t, area, b.conf, (b.x1, b.y1, b.x2, b.y2), frame))
                continue
            match.last_t, match.hits = t, match.hits + 1
            if area > match.best_area:
                match.best_t, match.best_area, match.best_conf = t, area, b.conf
                match.best_box, match.best_frame = (b.x1, b.y1, b.x2, b.y2), frame
        return [tr for tr in finished if tr.hits >= self.min_hits]

    def flush(self) -> list[Track]:
        done = [tr for tr in self.tracks if tr.hits >= self.min_hits]
        self.tracks = []
        return done


def jolt_for(track: Track, jolts: JoltSeries | None, speed_kmh: float | None, lookahead_m: float = 8.0):
    """The wheel reaches the pothole roughly `lookahead_m` after its biggest frame."""
    if jolts is None:
        return None
    v = max((speed_kmh or 20.0) / 3.6, 1.0)
    t_hit = track.best_t + lookahead_m / v
    return jolts.peak(track.best_t, t_hit + 1.0)
