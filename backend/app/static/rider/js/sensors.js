// GPS + accelerometer. Pure parts (interpolation, jolt peak) are unit-tested in Node.
import { haversine, median } from "./util.js";

const now = () => performance.now() / 1000;

// ----------------------------------------------------------------------------- GPS
export class GpsTrack {
  constructor(keepS = 120) {
    this.fixes = [];
    this.keepS = keepS;
  }

  push(fix) {
    const prev = this.fixes[this.fixes.length - 1];
    if (fix.speedKmh == null && prev && fix.t > prev.t) {
      fix.speedKmh = (haversine(prev.lat, prev.lon, fix.lat, fix.lon) / (fix.t - prev.t)) * 3.6;
    }
    this.fixes.push(fix);
    while (this.fixes.length && fix.t - this.fixes[0].t > this.keepS) this.fixes.shift();
  }

  get last() {
    return this.fixes[this.fixes.length - 1] || null;
  }

  at(t) {
    const f = this.fixes;
    if (!f.length) return null;
    if (t <= f[0].t) return f[0];
    if (t >= f[f.length - 1].t) return f[f.length - 1];
    let i = 1;
    while (f[i].t < t) i++;
    const a = f[i - 1], b = f[i], k = (t - a.t) / (b.t - a.t);
    return {
      t, lat: a.lat + k * (b.lat - a.lat), lon: a.lon + k * (b.lon - a.lon),
      speedKmh: a.speedKmh != null && b.speedKmh != null ? a.speedKmh + k * (b.speedKmh - a.speedKmh) : b.speedKmh,
      accuracy: Math.max(a.accuracy || 0, b.accuracy || 0) || null,
    };
  }
}

export class Gps {
  constructor() {
    this.track = new GpsTrack();
    this.watchId = null;
    this.sim = null;
    this.error = null;
    this.listeners = new Set();
  }

  onChange(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  _emit() {
    this.listeners.forEach((fn) => fn(this.track.last, this.error));
  }

  start() {
    if (this.sim || this.watchId !== null) return;
    if (!("geolocation" in navigator)) {
      this.error = "This browser has no GPS access.";
      return this._emit();
    }
    this.watchId = navigator.geolocation.watchPosition(
      (p) => {
        this.error = null;
        this.track.push({
          t: now(), lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy,
          speedKmh: p.coords.speed != null && p.coords.speed >= 0 ? p.coords.speed * 3.6 : null,
        });
        this._emit();
      },
      (e) => {
        this.error = e.code === 1 ? "Location permission denied. Allow it in browser settings." : "Waiting for GPS…";
        this._emit();
      },
      { enableHighAccuracy: true, maximumAge: 1000, timeout: 20000 },
    );
  }

  stop() {
    if (this.watchId !== null) navigator.geolocation.clearWatch(this.watchId);
    this.watchId = null;
    clearInterval(this.sim?.timer);
    this.sim = null;
  }

  /** Demo: drive in a straight line from (lat, lon) at headingDeg / speedKmh. */
  simulate(lat, lon, headingDeg = 90, speedKmh = 25) {
    this.stop();
    const t0 = now();
    const step = () => {
      const t = now(), d = ((speedKmh / 3.6) * (t - t0));
      const h = (headingDeg * Math.PI) / 180;
      this.track.push({
        t, lat: lat + (d * Math.cos(h)) / 111320,
        lon: lon + (d * Math.sin(h)) / (111320 * Math.cos((lat * Math.PI) / 180)),
        speedKmh, accuracy: 5,
      });
      this._emit();
    };
    step();
    this.sim = { timer: setInterval(step, 1000) };
  }

  /** One good fix for a photo report: waits up to timeoutMs for accuracy <= wantM. */
  fixOnce(wantM = 25, timeoutMs = 15000) {
    this.start();
    return new Promise((resolve) => {
      const done = (f) => { off(); clearTimeout(timer); resolve(f); };
      const off = this.onChange((f) => { if (f && (f.accuracy || 999) <= wantM) done(f); });
      const timer = setTimeout(() => done(this.track.last), timeoutMs);
      const f = this.track.last;
      if (f && now() - f.t < 10 && (f.accuracy || 999) <= wantM) done(f);
    });
  }
}

// --------------------------------------------------------------------- accelerometer
/** Ring buffer of |acceleration incl. gravity| in g. Orientation-independent, so any mount works. */
export class JoltBuffer {
  constructor(keepS = 30) {
    this.t = [];
    this.g = [];
    this.keepS = keepS;
  }

  push(t, magG) {
    this.t.push(t);
    this.g.push(magG);
    while (this.t.length && t - this.t[0] > this.keepS) {
      this.t.shift();
      this.g.shift();
    }
  }

  /** Peak |a - baseline| in [t0, t1]; baseline = median over a wider window (gravity + slow tilt). */
  peak(t0, t1) {
    const win = [], base = [];
    for (let i = 0; i < this.t.length; i++) {
      if (this.t[i] >= t0 - 1.5 && this.t[i] <= t1 + 1.5) base.push(this.g[i]);
      if (this.t[i] >= t0 && this.t[i] <= t1) win.push(this.g[i]);
    }
    if (!win.length) return null;
    const b = median(base);
    return Math.max(...win.map((x) => Math.abs(x - b)));
  }

  /** Recent peak, for the live meter. */
  recent(seconds = 1) {
    const last = this.t[this.t.length - 1];
    return last == null ? 0 : this.peak(last - seconds, last) || 0;
  }
}

export class Motion {
  constructor() {
    this.buf = new JoltBuffer();
    this.on = false;
    this.handler = (e) => {
      const a = e.accelerationIncludingGravity;
      if (!a || a.x == null) return;
      this.buf.push(now(), Math.hypot(a.x, a.y, a.z) / 9.80665);
    };
  }

  /** Must be called from a tap (iOS asks for permission). Returns false if unavailable. */
  async start() {
    if (this.on) return true;
    if (typeof DeviceMotionEvent === "undefined") return false;
    if (typeof DeviceMotionEvent.requestPermission === "function") {
      try {
        if ((await DeviceMotionEvent.requestPermission()) !== "granted") return false;
      } catch {
        return false;
      }
    }
    window.addEventListener("devicemotion", this.handler);
    this.on = true;
    return true;
  }

  stop() {
    window.removeEventListener("devicemotion", this.handler);
    this.on = false;
  }

  get hasData() {
    return this.buf.t.length > 0;
  }
}
