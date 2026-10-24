"""Scripted live demo of the full loop against the running stack (mock or live TinyFish).

  docker compose up -d --build
  python scripts/demo.py               # press Enter between acts while you narrate
  python scripts/demo.py --no-pause    # run straight through (rehearsal / CI)

Acts:
 1. Six riders pass a crater on Okhla Main Road -> one High hotspot (plus two smaller ones).
 2. A rider approves -> TinyFish agent files on the portal -> complaint ID comes back.
 3. The "officer" closes the ticket on the portal without fixing it -> Monitor sees "Closed".
 4. The next rider's phone still sees the crater -> reopened + auto-escalated with evidence.
 5. The escalation is closed and two riders pass a clean road -> verified fixed.
Open the dashboard at http://localhost:5173 alongside.
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import time
import uuid

import requests
from PIL import Image, ImageDraw

CRATER = (28.54612, 77.27215)   # Okhla, ~1 km from IIIT Delhi
SMALL = [(28.54840, 77.27490), (28.55120, 77.26980)]


def photo(dark: int = 25) -> bytes:
    img = Image.new("RGB", (640, 360), (118, 118, 118))
    d = ImageDraw.Draw(img)
    d.ellipse((230, 220, 410, 290), fill=(dark, dark, dark))
    d.text((10, 10), "RoadLoop demo hazard frame (synthetic)", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class Demo:
    def __init__(self, api: str, mock: str, pause: bool):
        self.api, self.mock, self.pause = api.rstrip("/"), mock.rstrip("/"), pause

    def act(self, title: str):
        print(f"\n=== {title} ===")
        if self.pause:
            input("  [Enter] ")

    def rider(self, name: str) -> str:
        r = requests.post(f"{self.api}/v1/riders",
                          json={"display_name": name, "phone": f"98{random.randint(10_000_000, 99_999_999)}",
                                "consent": True})
        r.raise_for_status()
        return r.json()["id"]

    def detect(self, rider: str, lat: float, lon: float, area: float, conf: float, jolt: float | None,
               road="Okhla Main Road", address="Okhla Main Road, Okhla Phase 3, New Delhi"):
        meta = {"rider_id": rider, "lat": lat + random.uniform(-3e-5, 3e-5), "lon": lon + random.uniform(-3e-5, 3e-5),
                "area_frac": area, "model_conf": conf, "jolt_g": jolt, "speed_kmh": random.uniform(18, 32),
                "gps_accuracy_m": 6, "road_name": road, "address": address, "city_key": "delhi", "state_key": "dl",
                "client_id": uuid.uuid4().hex}
        r = requests.post(f"{self.api}/v1/detections", data={"meta": json.dumps(meta)},
                          files={"photo": ("hazard.jpg", photo(), "image/jpeg")})
        r.raise_for_status()
        return r.json()

    def hotspot(self, hid: str) -> dict:
        return requests.get(f"{self.api}/v1/hotspots/{hid}").json()

    def wait_state(self, hid: str, states: set[str], timeout: float = 90) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            h = self.hotspot(hid)
            if h["state"] in states:
                return h
            time.sleep(1.5)
        raise SystemExit(f"timed out waiting for {states}; last state {h['state']}. Is the worker running?")

    def portal_set(self, complaint_id: str, status: str):
        requests.post(f"{self.mock}/portal/admin/{complaint_id}", data={"status": status}, allow_redirects=False)

    def run(self):
        riders = [self.rider(n) for n in ("Aman", "Priya", "Rahul", "Sana", "Imran", "Neha", "Vikram", "Asha")]

        self.act("Act 1: riders pass a fresh crater after heavy rain")
        for i, rid in enumerate(riders[:6]):
            out = self.detect(rid, *CRATER, area=random.uniform(0.06, 0.09), conf=random.uniform(0.7, 0.9),
                              jolt=random.uniform(0.6, 0.9))
            print(f"  {['Aman', 'Priya', 'Rahul', 'Sana', 'Imran', 'Neha'][i]:6s} -> hotspot {out['hotspot_id'][:8]} "
                  f"[{out['hotspot_state']}] detection severity {out['severity']}")
        hid = out["hotspot_id"]
        for lat, lon in SMALL:
            for rid in riders[:2]:
                self.detect(rid, lat, lon, area=0.015, conf=0.65, jolt=0.25, road="Okhla Industrial Estate Rd",
                            address="Okhla Industrial Estate, New Delhi")
        h = self.hotspot(hid)
        print(f"  6 passes merged into ONE hotspot: {h['severity']} severity, {h['distinct_riders']} riders, "
              f"confidence {h['confidence']:.2f}, priority {h['priority']:.0f}")

        self.act("Act 2: Aman approves; a TinyFish agent files the complaint")
        r = requests.post(f"{self.api}/v1/hotspots/{hid}/approve", json={"rider_id": riders[0]})
        r.raise_for_status()
        h = self.wait_state(hid, {"filed", "filing_failed"})
        c = h["complaints"][-1]
        print(f"  state={h['state']}  complaint {c['portal_complaint_id']} on {c['portal_key']} "
              f"(photo attached: {c['photo_attached']})")
        if h["state"] != "filed":
            raise SystemExit(f"filing failed: {c['needs_human_reason']}")

        self.act("Act 3: the portal marks it Closed. Nobody fixed the road.")
        self.portal_set(c["portal_complaint_id"], "Closed")
        h = self.wait_state(hid, {"awaiting_verification"})
        print("  Monitor saw 'Closed' -> RoadLoop asks riders passing the spot to re-check")

        self.act("Act 4: the next rider's phone still sees the crater")
        self.detect(riders[6], *CRATER, area=0.085, conf=0.91, jolt=0.85)
        h = self.wait_state(hid, {"escalated"})
        esc = h["complaints"][-1]
        print(f"  reopened and escalated to {esc['portal_key']} (level {h['escalation_level']})")
        print(f"  evidence sent: {esc['reason']}")
        h = self.wait_state(hid, {"escalated"})
        for _ in range(40):
            h = self.hotspot(hid)
            if h["complaints"][-1]["status"] == "filed":
                break
            time.sleep(1.5)
        print(f"  escalation complaint {h['complaints'][-1]['portal_complaint_id']}")

        self.act("Act 5: escalation resolved; two riders pass a smooth road")
        self.portal_set(h["complaints"][-1]["portal_complaint_id"], "Closed")
        self.wait_state(hid, {"awaiting_verification"})
        for rid in riders[6:8]:
            requests.post(f"{self.api}/v1/hotspots/{hid}/passes",
                          json={"rider_id": rid, "lat": h["lat"], "lon": h["lon"]}).raise_for_status()
        h = self.wait_state(hid, {"verified_fixed"})
        print(f"  VERIFIED FIXED. Timeline has {len(h['timeline'])} audited events.")
        print(json.dumps(requests.get(f"{self.api}/v1/stats").json(), indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--mock", default="http://localhost:8100")
    ap.add_argument("--no-pause", action="store_true")
    a = ap.parse_args()
    try:
        Demo(a.api, a.mock, not a.no_pause).run()
    except requests.ConnectionError as e:
        sys.exit(f"cannot reach the stack: {e}")
