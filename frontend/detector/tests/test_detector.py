"""Detector pipeline tests (numpy + opencv only; a fake model stands in for the trained ONNX)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roadloop_detector import fusion, privacy, yolo  # noqa: E402


def yolo_output(boxes_px, nc=1, n=50):
    """Build a fake Ultralytics output [1, 4+nc, n] with given (cx,cy,w,h,conf) in 640-space."""
    out = np.zeros((1, 4 + nc, n), np.float32)
    for i, (cx, cy, w, h, c) in enumerate(boxes_px):
        out[0, :4, i] = (cx, cy, w, h)
        out[0, 4, i] = c
    return out


class YoloTests(unittest.TestCase):
    def test_letterbox_and_decode_roundtrip(self):
        img = np.zeros((720, 1280, 3), np.uint8)
        lb, r, pad = yolo.letterbox(img)
        self.assertEqual(lb.shape, (640, 640, 3))
        self.assertAlmostEqual(r, 0.5)
        self.assertEqual(pad, (0, 140))
        # A box at original (400,500)-(600,600) -> letterbox (200,390)-(300,440)
        out = yolo_output([(250, 415, 100, 50, 0.9), (252, 416, 98, 52, 0.8), (500, 600, 40, 20, 0.2)])
        boxes = yolo.decode(out, r, pad, img.shape[:2])
        self.assertEqual(len(boxes), 1)  # NMS merged the duplicate, threshold dropped the weak one
        b = boxes[0]
        self.assertAlmostEqual(b.x1, 400, delta=1)
        self.assertAlmostEqual(b.y2, 600, delta=1)
        self.assertAlmostEqual(b.conf, 0.9, places=5)

    def test_transposed_export(self):
        out = np.transpose(yolo_output([(320, 320, 64, 64, 0.7)], n=200), (0, 2, 1))
        self.assertEqual(len(yolo.decode(out, 1.0, (0, 0), (640, 640))), 1)


class FusionTests(unittest.TestCase):
    def test_gps_interpolation(self):
        g = fusion.GpsTrack([fusion.GpsFix(0, 28.0, 77.0, 20), fusion.GpsFix(2, 28.002, 77.0, 40)])
        f = g.at(1.0)
        self.assertAlmostEqual(f.lat, 28.001)
        self.assertAlmostEqual(f.speed_kmh, 30)
        self.assertEqual(g.at(-5).lat, 28.0)

    def test_jolt_peak_ignores_gravity_and_tilt(self):
        t = np.arange(0, 10, 0.02)
        az = 1.0 + 0.05 * np.sin(t)          # gravity + slow tilt
        az[(t > 5.0) & (t < 5.06)] += 0.8    # pothole hit
        j = fusion.JoltSeries(t, az)
        self.assertGreater(j.peak(4.5, 5.5), 0.7)
        self.assertLess(j.peak(1.0, 3.0), 0.1)

    def test_debouncer_one_event_per_pothole(self):
        deb = fusion.PassDebouncer(1280, 720)
        events = []
        for i in range(8):  # pothole approaching: grows, moves down
            s = 40 + 10 * i
            events += deb.update(i * 0.15, [yolo.Box(600, 400 + 10 * i, 600 + s, 400 + 10 * i + s / 2, 0.6 + i * 0.02, 0)])
        events += deb.update(3.0, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].hits, 8)
        self.assertAlmostEqual(events[0].best_t, 7 * 0.15)
        # A single flicker frame is not a pothole.
        deb.update(5.0, [yolo.Box(100, 600, 150, 630, 0.5, 0)])
        self.assertEqual(deb.update(7.0, []), [])


class PrivacyTests(unittest.TestCase):
    def test_only_pothole_region_stays_sharp(self):
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        out = privacy.redact(frame, (600, 500, 700, 560))
        self.assertTrue(np.array_equal(out[520, 650], frame[520, 650]))   # pothole kept
        self.assertGreater(np.abs(out[100:200, 100:200].astype(int) - frame[100:200, 100:200]).mean(), 30)
        self.assertTrue(privacy.encode_jpeg(out)[:2] == b"\xff\xd8")


class FakeModel:
    """Finds the dark blob we draw in the synthetic clip, like a pothole detector would."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, frame):
        mask = (frame[:, :, 0] < 40).astype(np.uint8)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        out = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if area > 30:
                out.append(yolo.Box(float(x), float(y), float(x + w), float(y + h), 0.8, 0))
        return out


class EndToEndDryRun(unittest.TestCase):
    def test_clip_to_events(self):
        import detect
        tmp = Path(tempfile.mkdtemp())
        clip = tmp / "clip.avi"
        W, H, fps = 320, 180, 30
        vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
        for i in range(fps * 6):            # 6 s clip, pothole visible from 1.0 s to 2.5 s
            f = np.full((H, W, 3), 120, np.uint8)
            t = i / fps
            if 1.0 <= t <= 2.5:
                k = (t - 1.0) / 1.5
                cv2.ellipse(f, (160, int(100 + 60 * k)), (int(10 + 30 * k), int(4 + 12 * k)), 0, 0, 360, (20, 20, 20), -1)
            vw.write(f)
        vw.release()
        imu = tmp / "imu.csv"
        t = np.arange(0, 6, 0.02)
        az = np.ones_like(t)
        az[(t > 3.3) & (t < 3.4)] += 0.9     # wheel hits it ~1 s after biggest frame at 25 km/h
        np.savetxt(imu, np.c_[t, az], delimiter=",", header="t,az_g", comments="")
        detect.PotholeModel = FakeModel
        rc = detect.main([str(clip), "--model", "fake.onnx", "--start", "28.5459,77.2732", "--heading", "90",
                          "--speed", "25", "--imu", str(imu), "--dry-run", "--out", str(tmp / "out"),
                          "--road-name", "Okhla Main Road", "--city", "delhi"])
        self.assertEqual(rc, 0)
        events = json.loads((tmp / "out" / "events.json").read_text())
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertGreater(e["jolt_g"], 0.5)
        self.assertGreater(e["lon"], 77.2732)       # moved east along the synthetic track
        self.assertTrue((tmp / "out" / f"{e['client_id']}.jpg").exists())


if __name__ == "__main__":
    unittest.main()
