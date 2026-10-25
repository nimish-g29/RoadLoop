// node --test backend/tests/js
import { test } from "node:test";
import assert from "node:assert/strict";
import { letterboxParams, decode, nms, toCHW } from "../../app/static/rider/js/yolo.js";
import { FrameRing, JoltTrigger, PassTracker, bumpLookback, joltWindow } from "../../app/static/rider/js/tracker.js";
import { GpsTrack, JoltBuffer } from "../../app/static/rider/js/sensors.js";
import { haversine, median } from "../../app/static/rider/js/util.js";

function fakeOutput(boxes, n = 50, nc = 1) {
  const C = 4 + nc, data = new Float32Array(C * n);
  boxes.forEach(([cx, cy, w, h, c], i) => {
    data[0 * n + i] = cx; data[1 * n + i] = cy; data[2 * n + i] = w; data[3 * n + i] = h; data[4 * n + i] = c;
  });
  return { data, dims: [1, C, n] };
}

test("letterbox + decode match the Python implementation", () => {
  const lb = letterboxParams(1280, 720, 640);
  assert.equal(lb.r, 0.5);
  assert.deepEqual([lb.padX, lb.padY], [0, 140]);
  const { data, dims } = fakeOutput([[250, 415, 100, 50, 0.9], [252, 416, 98, 52, 0.8], [500, 600, 40, 20, 0.2]]);
  const boxes = decode(data, dims, lb, 1280, 720);
  assert.equal(boxes.length, 1);
  assert.ok(Math.abs(boxes[0].x1 - 400) < 1 && Math.abs(boxes[0].y2 - 600) < 1);
});

test("decode handles transposed exports", () => {
  const n = 200, C = 5, data = new Float32Array(n * C);
  data.set([320, 320, 64, 64, 0.7], 0); // row 0 in [N, C] layout
  const boxes = decode(data, [1, n, C], letterboxParams(640, 640, 640), 640, 640);
  assert.equal(boxes.length, 1);
});

test("nms keeps different classes", () => {
  const a = { x1: 0, y1: 0, x2: 10, y2: 10, conf: 0.9, cls: 0 };
  assert.equal(nms([a, { ...a, conf: 0.8 }, { ...a, cls: 1, conf: 0.5 }]).length, 2);
});

test("toCHW layout", () => {
  const rgba = new Uint8ClampedArray([255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 0, 0, 0, 255]);
  const out = toCHW(rgba, 2);
  assert.deepEqual(Array.from(out.slice(0, 4)), [1, 0, 0, 0]); // R plane
  assert.deepEqual(Array.from(out.slice(4, 8)), [0, 1, 0, 0]); // G plane
});

test("tracker: one event per pothole, biggest frame kept, flicker ignored", () => {
  const tr = new PassTracker(1280, 720);
  let events = [];
  for (let i = 0; i < 8; i++) {
    const s = 40 + 10 * i;
    events.push(...tr.update(i * 0.2, [{ x1: 600, y1: 400 + 10 * i, x2: 600 + s, y2: 400 + 10 * i + s / 2, conf: 0.6 + i * 0.02 }],
      () => `frame${i}`));
  }
  events.push(...tr.update(3.0, []));
  assert.equal(events.length, 1);
  assert.equal(events[0].hits, 8);
  assert.equal(events[0].frame, "frame7");
  tr.update(5.0, [{ x1: 100, y1: 600, x2: 150, y2: 630, conf: 0.5 }]);
  assert.deepEqual(tr.update(7.0, []), []);
  const [a, b] = joltWindow(10, 36);
  assert.equal(a, 10);
  assert.ok(Math.abs(b - 11.8) < 1e-9); // 8 m at 10 m/s + 1 s
});

test("jolt buffer: gravity removed, hit found", () => {
  const j = new JoltBuffer();
  for (let t = 0; t < 10; t += 0.02) j.push(t, 1 + 0.03 * Math.sin(t) + (t > 5 && t < 5.06 ? 0.8 : 0));
  assert.ok(j.peak(4.5, 5.5) > 0.7);
  assert.ok(j.peak(1, 3) < 0.1);
  assert.equal(j.peak(20, 21), null);
});

test("gps track: interpolation and derived speed", () => {
  const g = new GpsTrack();
  g.push({ t: 0, lat: 28.0, lon: 77.0, accuracy: 5 });
  g.push({ t: 10, lat: 28.0009, lon: 77.0, accuracy: 8 }); // ~100 m in 10 s
  assert.ok(Math.abs(g.last.speedKmh - 36) < 1);
  const mid = g.at(5);
  assert.ok(Math.abs(mid.lat - 28.00045) < 1e-9);
  assert.equal(mid.accuracy, 8);
  assert.equal(g.at(-1).lat, 28.0);
});

test("util", () => {
  assert.ok(Math.abs(haversine(28.6139, 77.209, 28.6139, 77.21) - 97.7) < 1.5);
  assert.equal(median([3, 1, 2, 10]), 2.5);
});

test("hands-free: frame ring keeps ~2.5 s and returns the frame before a time", () => {
  const ring = new FrameRing(2.5);
  for (let i = 0; i <= 25; i++) ring.push(i / 5, `f${(i / 5).toFixed(1)}`);
  assert.equal(ring.items[0].t, 2.6); // frames every 0.2 s: 2.4 is 2.6 s old, so it is dropped
  assert.ok(5 - ring.items[0].t <= 2.5);
  assert.equal(ring.at(4.05).frame, "f4.0");
  assert.equal(ring.at(1).frame, ring.items[0].frame); // before the buffer: oldest frame
});

test("hands-free: look back to when the pothole was ~8 m ahead, capped at 2 s", () => {
  assert.ok(Math.abs(bumpLookback(36) - 0.8) < 1e-9); // 10 m/s
  assert.equal(bumpLookback(5), 2);
});

test("hands-free: jolt trigger fires once per bump, not when slow or quiet", () => {
  const buf = new JoltBuffer();
  for (let t = 0; t < 10; t += 0.02) buf.push(t, 1.0 + (t > 5 && t < 5.1 ? 1.2 : 0)); // one hard hit at 5 s
  const trig = new JoltTrigger();
  assert.equal(trig.check(buf, 3, 30), null);            // smooth road
  assert.equal(trig.check(buf, 5.1, 5), null);           // crawling: ignored
  const hit = trig.check(buf, 5.1, 30);
  assert.ok(hit && hit.g > 1);
  assert.equal(trig.check(buf, 5.12, 30), null);         // same pothole: cooldown
  assert.ok(new JoltTrigger().check(buf, 5.1, null));    // no GPS speed: still allowed
});

test("GPS speed: a phone reporting 0 while moving is overridden by measured movement", () => {
  const g = new GpsTrack();
  // ~36 km/h east (10 m/s), phone always says speed 0
  for (let i = 0; i <= 6; i++) g.push({ t: i, lat: 28.5, lon: 77.0 + (i * 10) / 97800, speedKmh: 0, accuracy: 8 });
  assert.ok(Math.abs(g.last.speedKmh - 36) < 3, `got ${g.last.speedKmh}`);
});

test("GPS speed: standing still with small jitter stays slow", () => {
  const g = new GpsTrack();
  for (let i = 0; i <= 6; i++) g.push({ t: i, lat: 28.5 + (i % 2) * 0.00002, lon: 77.0, speedKmh: 0, accuracy: 8 });
  assert.ok(g.last.speedKmh < 5, `got ${g.last.speedKmh}`);
});

test("hands-free: on a rough road the bar rises above the shaking, a real hit still fires", () => {
  const buf = new JoltBuffer();
  let seed = 7;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5);
  for (let t = 0; t < 10; t += 0.02) buf.push(t, 1.0 + 0.4 * rnd() + (t > 8 && t < 8.06 ? 1.6 : 0));
  const trig = new JoltTrigger({ minG: 0.4, noiseK: 3.5 });
  for (let t = 5.5; t < 7.9; t += 0.1) assert.equal(trig.check(buf, t, 30), null, `false fire at ${t}`);
  assert.ok(trig.threshold > 0.4, `threshold ${trig.threshold}`);
  assert.ok(trig.check(buf, 8.06, 30));
});
