// Drive mode: phone mounted, camera on, the model looks for potholes 3-5x per second.
// Nothing is uploaded unless a pothole is seen: then ONE redacted frame + GPS + jolt per pothole.
// Also does repair verification silently: driving past a "portal says fixed" spot with the
// model running and seeing nothing sends a clear pass.
import { api } from "./api.js";
import { snapshot, redact, toJpeg } from "./redact.js";
import { FrameRing, JoltTrigger, PassTracker, bumpLookback, joltWindow } from "./tracker.js";
import { clamp, haversine, uuid } from "./util.js";
import { PotholeModel } from "./yolo.js";

const now = () => performance.now() / 1000;
const round = (x, n) => (x == null ? null : Math.round(x * 10 ** n) / 10 ** n);
const models = new Map();

function loadModel(url, meta) {
  if (!models.has(url)) {
    const p = PotholeModel.load(url, meta);
    p.catch(() => models.delete(url)); // allow a retry after a failure
    models.set(url, p);
  }
  return models.get(url);
}

export class DriveSession extends EventTarget {
  constructor({ video, overlay, gps, motion, uploader, riderId, config, targetFps = 5 }) {
    super();
    Object.assign(this, { video, overlay, gps, motion, uploader, riderId, config, targetFps });
    this.tracker = new PassTracker(1280, 720);
    this.running = false;
    this.model = null;
    this.verify = new Map();
    this.stats = { fps: 0, inferMs: 0, sent: 0, passes: 0, boxes: 0, jolt: 0 };
    this._frames = [];
    this.recent = new FrameRing(2.5); // last few seconds of frames for hands-free capture
    this.bump = new JoltTrigger(config?.bump || {});
    this.lastAutoT = -Infinity;
  }

  _emitEvt(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  /**
   * source: "camera" | "file". sim: {lat, lon, heading, speed} to fake GPS (demo on a laptop).
   * Call straight from a tap handler: motion permission (iOS) must be requested in the gesture.
   */
  async start({ source = "camera", file = null, sim = null } = {}) {
    const motionOk = this.motion.start(); // first statement: keeps the user-gesture for iOS
    if (sim) this.gps.simulate(sim.lat, sim.lon, sim.heading, sim.speed);
    else this.gps.start();

    if (source === "file") {
      if (!file) throw new Error("Choose a video file first.");
      this.video.srcObject = null;
      this.video.src = URL.createObjectURL(file);
      this.video.loop = false;
      this.video.onended = () => this.stop();
    } else {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("Camera needs https. Open the app through the https tunnel link.");
      }
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      this.video.removeAttribute("src");
      this.video.srcObject = this.stream;
    }
    this.video.muted = true;
    this.video.playsInline = true;
    await this.video.play();

    try {
      this.wakeLock = await navigator.wakeLock?.request("screen");
    } catch {
      /* not supported: user must keep the screen on */
    }
    this._onVis = async () => {
      if (document.visibilityState === "visible" && this.running && (!this.wakeLock || this.wakeLock.released)) {
        try { this.wakeLock = await navigator.wakeLock?.request("screen"); } catch { /* ignore */ }
      }
    };
    document.addEventListener("visibilitychange", this._onVis);

    this.motionOk = await motionOk;
    this.running = true;
    this._emitEvt("state", { running: true });

    if (this.config.model_url) {
      this._emitEvt("status", { model: "loading" });
      try {
        this.model = await loadModel(this.config.model_url, this.config.model_meta || {});
        this._emitEvt("status", { model: "ready" });
      } catch (e) {
        this._emitEvt("status", { model: "error", error: e.message });
      }
    } else {
      this._emitEvt("status", { model: "missing" });
    }

    this._tasksTimer = setInterval(() => this.refreshTasks(), 45000);
    setTimeout(() => this.refreshTasks(), 3000);
    this._tick();
  }

  async stop() {
    if (!this.running) return;
    this.running = false;
    clearInterval(this._tasksTimer);
    this.tracker.flush().forEach((tr) => this._scheduleEmit(tr, "auto"));
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.video.pause();
    try { await this.wakeLock?.release(); } catch { /* ignore */ }
    document.removeEventListener("visibilitychange", this._onVis);
    if (this.gps.sim) this.gps.stop();
    this.motion.stop();
    this.overlay.getContext("2d").clearRect(0, 0, this.overlay.width, this.overlay.height);
    this._emitEvt("state", { running: false });
  }

  // ------------------------------------------------------------------- main loop
  async _tick() {
    if (!this.running) return;
    const t = now();
    const v = this.video;
    if (v.readyState >= 2 && v.videoWidth) {
      this.tracker.resize(v.videoWidth, v.videoHeight);
      let boxes = [];
      if (this.model) {
        try {
          boxes = await this.model.detect(v);
          this.stats.inferMs = Math.round(this.model.lastMs);
        } catch (e) {
          this._emitEvt("status", { model: "error", error: e.message });
          this.model = null;
        }
        // Potholes are on the road surface: ignore the top 40% (sky, vehicles ahead).
        boxes = boxes.filter((b) => (b.y1 + b.y2) / 2 > 0.4 * v.videoHeight);
      }
      this.stats.boxes = boxes.length;
      this._draw(boxes);
      this.tracker.update(t, boxes, () => snapshot(v)).forEach((tr) => {
        this.lastAutoT = tr.lastT;
        this._scheduleEmit(tr, "auto");
      });
      this.recent.push(t, snapshot(v, 800));
      this._checkBump(t);
      this._checkVerify(boxes.length > 0);
      this._frames.push(t);
      while (this._frames.length && t - this._frames[0] > 2) this._frames.shift();
      this.stats.fps = Math.round((this._frames.length / 2) * 10) / 10;
      this.stats.jolt = this.motion.hasData ? round(this.motion.buf.recent(1), 2) : null;
      this._emitEvt("stats", this.stats);
    }
    const elapsed = (now() - t) * 1000;
    setTimeout(() => this._tick(), Math.max(10, 1000 / this.targetFps - elapsed));
  }

  _draw(boxes) {
    const c = this.overlay, v = this.video;
    const cw = (c.width = c.clientWidth), ch = (c.height = c.clientHeight);
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, cw, ch);
    if (!boxes.length) return;
    const s = Math.max(cw / v.videoWidth, ch / v.videoHeight); // matches object-fit: cover
    const ox = (cw - v.videoWidth * s) / 2, oy = (ch - v.videoHeight * s) / 2;
    ctx.lineWidth = 3;
    ctx.font = "600 14px system-ui";
    for (const b of boxes) {
      ctx.strokeStyle = "#F5A623";
      ctx.strokeRect(ox + b.x1 * s, oy + b.y1 * s, (b.x2 - b.x1) * s, (b.y2 - b.y1) * s);
      ctx.fillStyle = "#F5A623";
      ctx.fillText(`pothole ${Math.round(b.conf * 100)}%`, ox + b.x1 * s, oy + b.y1 * s - 6);
    }
  }

  /**
   * Hands-free capture: a hard jolt means the wheel just hit something. Send the frame from
   * just before the hit (when the pothole was in view ahead), located where the jolt happened.
   * If the model is running and already tracking it, the model's report wins (no duplicate).
   */
  _checkBump(t) {
    if (!this.motion.hasData) return;
    const fix = this.gps.track.last;
    const speed = fix?.speedKmh ?? null;
    const hit = this.bump.check(this.motion.buf, t, speed);
    if (!hit) return;
    if (this.model && (this.tracker.active || t - this.lastAutoT < 3)) return;
    const f = this.recent.at(hit.t - bumpLookback(speed));
    const v = this.video;
    if (!f || !v.videoWidth) return;
    const W = v.videoWidth, H = v.videoHeight;
    const box = { x1: W * 0.25, y1: H * 0.55, x2: W * 0.75, y2: H * 0.95 }; // road just ahead
    this._scheduleEmit({ bestT: f.t, locT: hit.t, area: 0.035, conf: 0.6, box, frame: f.frame }, "jolt");
    this._emitEvt("bump", { g: hit.g });
  }

  // ------------------------------------------------------------------- uploads
  /** Rider/passenger taps "Mark pothole": use the current frame, lower-centre region. */
  mark() {
    const v = this.video;
    if (!this.running || !v.videoWidth) return false;
    const W = v.videoWidth, H = v.videoHeight;
    const box = { x1: W * 0.3, y1: H * 0.55, x2: W * 0.7, y2: H * 0.9 };
    this._scheduleEmit({ bestT: now(), area: 0.035, conf: 0.7, box, frame: snapshot(v) }, "manual");
    return true;
  }

  _scheduleEmit(tr, source) {
    const fix = this.gps.track.at(tr.locT ?? tr.bestT);
    const [a, b] = joltWindow(tr.bestT, fix?.speedKmh);
    const delay = Math.max(0, (b - now()) * 1000); // wait until the wheel has hit it
    setTimeout(() => this._emitDetection(tr, source, a, b).catch((e) => this._emitEvt("error", e.message)), delay);
  }

  async _emitDetection(tr, source, a, b) {
    const fix = this.gps.track.at(tr.locT ?? tr.bestT); // jolt: where the wheel hit it
    if (!fix) {
      this._emitEvt("error", "Pothole seen but no GPS fix yet: not sent.");
      return;
    }
    const snap = tr.frame;
    if (!snap) return;
    const k = snap.scale || 1;
    const box = { x1: tr.box.x1 * k, y1: tr.box.y1 * k, x2: tr.box.x2 * k, y2: tr.box.y2 * k };
    const blob = await toJpeg(redact(snap, box));
    const jolt = this.motion.hasData ? this.motion.buf.peak(a, b) : null;
    const meta = {
      rider_id: this.riderId,
      lat: fix.lat,
      lon: fix.lon,
      area_frac: round(clamp(tr.area, 0, 1), 5),
      model_conf: round(clamp(tr.conf, 0, 1), 3),
      jolt_g: jolt == null ? null : round(clamp(jolt, 0, 8), 3),
      speed_kmh: fix.speedKmh == null ? null : round(clamp(fix.speedKmh, 0, 200), 1),
      gps_accuracy_m: fix.accuracy == null ? null : round(fix.accuracy, 1),
      captured_at: new Date(Date.now() - (now() - tr.bestT) * 1000).toISOString(),
      client_id: uuid(),
      source,
    };
    await this.uploader.add(meta, blob);
    this.stats.sent += 1;
    this._emitEvt("detection", { meta, preview: blob });
  }

  // -------------------------------------------------------------- verification
  async refreshTasks() {
    const f = this.gps.track.last;
    if (!f || !this.running) return;
    try {
      const r = await api.tasks(this.riderId, f.lat, f.lon);
      for (const v of r.verify) {
        if (!this.verify.has(v.hotspot_id)) {
          this.verify.set(v.hotspot_id, { ...v, minD: Infinity, minFix: null, saw: false, done: false });
        }
      }
      this._emitEvt("tasks", { verify: [...this.verify.values()].filter((v) => !v.done).length });
    } catch {
      /* offline: try again next round */
    }
  }

  _checkVerify(sawNow) {
    const f = this.gps.track.last;
    if (!f || !this.model || (f.accuracy || 0) > 30) return; // only a looking model + good GPS can vouch
    for (const [id, v] of this.verify) {
      if (v.done) continue;
      const d = haversine(f.lat, f.lon, v.lat, v.lon);
      if (d < v.minD) Object.assign(v, { minD: d, minFix: { lat: f.lat, lon: f.lon } });
      if (d <= v.radius_m + 10 && (sawNow || this.tracker.active)) v.saw = true;
      if (v.minD <= v.radius_m && d > v.minD + 30) {
        v.done = true; // passed it and moved on
        if (!v.saw) {
          api.pass(id, this.riderId, v.minFix.lat, v.minFix.lon)
            .then(() => {
              this.stats.passes += 1;
              this._emitEvt("pass", { hotspot_id: id });
            })
            .catch(() => {});
        }
      }
    }
  }
}
