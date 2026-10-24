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

/** When does the wheel reach a pothole seen biggest at bestT? ~lookaheadM metres later. */
export function joltWindow(bestT, speedKmh, lookaheadM = 8) {
  const v = Math.max((speedKmh || 20) / 3.6, 1);
  return [bestT, bestT + lookaheadM / v + 1];
}
