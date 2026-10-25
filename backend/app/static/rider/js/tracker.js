// One event per pothole per pass. Same logic as detector/roadloop_detector/fusion.py:
// a pothole stays in view for many frames while you approach; it grows and slides down.
// We keep its biggest (closest) frame and emit when it has been out of view for gapS.

export class PassTracker {
  constructor(frameW, frameH, { gapS = 0.6, minHits = 2, xTol = 0.18 } = {}) {
    Object.assign(this, { W: frameW, H: frameH, gapS, minHits, xTol });
    this.tracks = [];
  }

  resize(w, h) {
    this.W = w;
    this.H = h;
  }

  /** boxes in frame pixels; capture() returns a snapshot of the current frame (called lazily). */
  update(t, boxes, capture = () => null) {
    const finished = this.tracks.filter((tr) => t - tr.lastT > this.gapS);
    this.tracks = this.tracks.filter((tr) => t - tr.lastT <= this.gapS);
    for (const b of boxes) {
      const cx = (b.x1 + b.x2) / 2 / this.W;
      const area = ((b.x2 - b.x1) * (b.y2 - b.y1)) / (this.W * this.H);
      const tr = this.tracks.find((x) => Math.abs(cx - (x.box.x1 + x.box.x2) / 2 / this.W) < this.xTol);
      if (!tr) {
        this.tracks.push({ firstT: t, lastT: t, bestT: t, area, conf: b.conf, box: b, frame: capture(), hits: 1 });
        continue;
      }
      tr.lastT = t;
      tr.hits += 1;
      if (area > tr.area) Object.assign(tr, { bestT: t, area, conf: b.conf, box: b, frame: capture() });
      else tr.conf = Math.max(tr.conf, b.conf);
    }
    return finished.filter((tr) => tr.hits >= this.minHits);
  }

  flush() {
    const done = this.tracks.filter((tr) => tr.hits >= this.minHits);
    this.tracks = [];
    return done;
  }

  get active() {
    return this.tracks.length > 0;
  }
}

// ------------------------------------------------------------- hands-free capture
// The rider never touches the phone: the camera keeps the last few seconds of frames,
// and a hard jolt picks the frame from just before the wheel hit the pothole.

/** Rolling buffer of recent frames: [{t, frame}], oldest first. */
export class FrameRing {
  constructor(keepS = 2.5) {
    this.keepS = keepS;
    this.items = [];
  }

  push(t, frame) {
    this.items.push({ t, frame });
    while (this.items.length && t - this.items[0].t > this.keepS) this.items.shift();
  }

  /** Latest frame taken at or before t (the oldest one if t is before the buffer). */
  at(t) {
    let best = null;
    for (const it of this.items) {
      if (it.t <= t) best = it;
      else break;
    }
    return best || this.items[0] || null;
  }
}

/** Seconds before the jolt when the pothole was ~lookaheadM ahead (in view of the camera). */
export function bumpLookback(speedKmh, lookaheadM = 8) {
  const v = Math.max((speedKmh || 20) / 3.6, 1);
  return Math.min(lookaheadM / v, 2);
}

/**
 * Fires once per hard bump. The bar adapts to the road: on a rough road the bike shakes all the
 * time, so a pothole must stand out from the last few seconds of vibration (noiseK x the typical
 * shake), never below minG. Ignores bumps while stopped or crawling (handling the phone, getting
 * on the bike) and repeats within cooldownS (one pothole shakes for a while).
 * speedKmh = null means the phone gives no speed: allowed, the crowd check filters noise.
 */
export class JoltTrigger {
  constructor({ minG, thresholdG, noiseK = 3.5, minSpeedKmh = 8, cooldownS = 4, windowS = 0.3, noiseS = 5 } = {}) {
    this.minG = minG ?? thresholdG ?? 0.4;
    Object.assign(this, { noiseK, minSpeedKmh, cooldownS, windowS, noiseS });
    this.lastFire = -Infinity;
    this.threshold = this.minG;
  }

  /** Typical shake over the last noiseS seconds (robust: median absolute deviation, in g). */
  noise(buf, t) {
    const xs = [];
    for (let i = 0; i < buf.t.length; i++) {
      if (buf.t[i] >= t - this.noiseS && buf.t[i] < t - this.windowS) xs.push(buf.g[i]);
    }
    if (xs.length < 10) return 0;
    const med = median(xs);
    return 1.4826 * median(xs.map((x) => Math.abs(x - med)));
  }

  /** buf: JoltBuffer. Returns {t, g, threshold} when a bump fires, else null. */
  check(buf, t, speedKmh) {
    this.threshold = Math.max(this.minG, this.noiseK * this.noise(buf, t));
    if (t - this.lastFire < this.cooldownS) return null;
    if (speedKmh != null && speedKmh < this.minSpeedKmh) return null;
    const g = buf.peak(t - this.windowS, t);
    if (g == null || g < this.threshold) return null;
    this.lastFire = t;
    return { t, g, threshold: this.threshold };
  }
}

function median(xs) {
  const a = [...xs].sort((p, q) => p - q), m = a.length >> 1;
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}

/** When does the wheel reach a pothole seen biggest at bestT? ~lookaheadM metres later. */
export function joltWindow(bestT, speedKmh, lookaheadM = 8) {
  const v = Math.max((speedKmh || 20) / 3.6, 1);
  return [bestT, bestT + lookaheadM / v + 1];
}
