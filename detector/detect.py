"""Run RoadLoop detection on a recorded dashcam clip (the 24-hour demo path).

  python detect.py clip.mp4 --model weights/pothole.onnx --rider-id <id> \
      --gps gps.csv --imu imu.csv --api http://localhost:8000 --road-name "Okhla Main Road" --city delhi

gps.csv:  t,lat,lon[,speed_kmh,accuracy_m]   (t = seconds from clip start)
imu.csv:  t,az_g                             (vertical acceleration in g, gravity included)
No GPS log? Use --start LAT,LON --heading DEG --speed KMH to synthesise a straight track.
--dry-run writes events.json + redacted frames to --out instead of uploading.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import uuid
from pathlib import Path

import cv2
import numpy as np

from roadloop_detector.fusion import GpsFix, GpsTrack, JoltSeries, PassDebouncer, jolt_for
from roadloop_detector.privacy import encode_jpeg, redact
from roadloop_detector.yolo import PotholeModel


def load_gps(path: str) -> GpsTrack:
    fixes = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            fixes.append(GpsFix(float(row["t"]), float(row["lat"]), float(row["lon"]),
                                float(row["speed_kmh"]) if row.get("speed_kmh") else None,
                                float(row["accuracy_m"]) if row.get("accuracy_m") else None))
    return GpsTrack(fixes)


def synth_gps(start: str, heading_deg: float, speed_kmh: float, duration_s: float) -> GpsTrack:
    lat0, lon0 = (float(x) for x in start.split(","))
    v = speed_kmh / 3.6
    fixes = []
    for i in range(int(duration_s) + 2):
        d = v * i
        dlat = d * math.cos(math.radians(heading_deg)) / 111_320
        dlon = d * math.sin(math.radians(heading_deg)) / (111_320 * math.cos(math.radians(lat0)))
        fixes.append(GpsFix(float(i), lat0 + dlat, lon0 + dlon, speed_kmh, 8.0))
    return GpsTrack(fixes)


def load_imu(path: str) -> JoltSeries:
    data = np.loadtxt(path, delimiter=",", skiprows=1)
    return JoltSeries(data[:, 0], data[:, 1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--model", required=True)
    ap.add_argument("--rider-id")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--gps")
    ap.add_argument("--imu")
    ap.add_argument("--start")
    ap.add_argument("--heading", type=float, default=0.0)
    ap.add_argument("--speed", type=float, default=25.0)
    ap.add_argument("--road-name")
    ap.add_argument("--road-ref")
    ap.add_argument("--address")
    ap.add_argument("--city")
    ap.add_argument("--state")
    ap.add_argument("--fps", type=float, default=6.0, help="frames per second to analyse (phone runs ~5-8)")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="out")
    ap.add_argument("--show", action="store_true", help="preview window with boxes")
    a = ap.parse_args(argv)

    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        print(f"cannot open {a.video}", file=sys.stderr)
        return 2
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = n_frames / src_fps if n_frames else 600
    if a.gps:
        gps = load_gps(a.gps)
    elif a.start:
        gps = synth_gps(a.start, a.heading, a.speed, duration)
    else:
        print("need --gps or --start", file=sys.stderr)
        return 2
    jolts = load_imu(a.imu) if a.imu else None
    if not a.dry_run and not a.rider_id:
        print("--rider-id is required unless --dry-run", file=sys.stderr)
        return 2

    model = PotholeModel(a.model, conf_thres=a.conf)
    deb = PassDebouncer(W, H)
    step = max(1, int(round(src_fps / a.fps)))
    out = Path(a.out)
    events = []

    def emit(tracks):
        for tr in tracks:
            fix = gps.at(tr.best_t)
            jolt = jolt_for(tr, jolts, fix.speed_kmh)
            img = redact(tr.best_frame, tr.best_box)
            meta = {"rider_id": a.rider_id or "dry-run", "lat": fix.lat, "lon": fix.lon,
                    "area_frac": round(tr.best_area, 5), "model_conf": round(tr.best_conf, 3),
                    "jolt_g": None if jolt is None else round(jolt, 3), "speed_kmh": fix.speed_kmh,
                    "gps_accuracy_m": fix.accuracy_m, "road_name": a.road_name, "road_ref": a.road_ref,
                    "address": a.address, "city_key": a.city, "state_key": a.state,
                    "client_id": uuid.uuid4().hex}
            events.append(meta)
            if a.dry_run:
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{meta['client_id']}.jpg").write_bytes(encode_jpeg(img))
                print(f"[t={tr.best_t:6.1f}s] pothole conf={tr.best_conf:.2f} area={tr.best_area:.3f} jolt={jolt}")
            else:
                import requests
                r = requests.post(f"{a.api}/v1/detections", data={"meta": json.dumps(meta)},
                                  files={"photo": ("hazard.jpg", encode_jpeg(img), "image/jpeg")}, timeout=20)
                r.raise_for_status()
                print(f"[t={tr.best_t:6.1f}s] uploaded -> {r.json()}")

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            t = idx / src_fps
            boxes = model(frame)
            # Ignore the top 40% of the frame (sky, vehicles ahead): potholes are on the road surface.
            boxes = [b for b in boxes if (b.y1 + b.y2) / 2 > 0.4 * H]
            emit(deb.update(t, boxes, frame))
            if a.show:
                vis = frame.copy()
                for b in boxes:
                    cv2.rectangle(vis, (int(b.x1), int(b.y1)), (int(b.x2), int(b.y2)), (0, 165, 255), 2)
                cv2.imshow("RoadLoop", vis)
                if cv2.waitKey(1) == 27:
                    break
        idx += 1
    emit(deb.flush())
    if a.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        (out / "events.json").write_text(json.dumps(events, indent=2))
    print(f"{len(events)} pothole events from {idx} frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
