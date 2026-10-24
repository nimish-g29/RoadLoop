// YOLO (Ultralytics v8/11 ONNX export) in the browser via onnxruntime-web.
// Pre/post-processing matches detector/roadloop_detector/yolo.py so phone and laptop agree.

const ORT_VERSION = "1.19.2";
const ORT_BASE = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/`;

export function letterboxParams(srcW, srcH, size) {
  const r = Math.min(size / srcW, size / srcH);
  const w = Math.round(srcW * r), h = Math.round(srcH * r);
  return { r, w, h, padX: Math.floor((size - w) / 2), padY: Math.floor((size - h) / 2), size };
}

/** RGBA pixels (size*size*4) -> Float32 CHW in [0,1]. */
export function toCHW(rgba, size) {
  const n = size * size;
  const out = new Float32Array(3 * n);
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    out[i] = rgba[p] / 255;
    out[n + i] = rgba[p + 1] / 255;
    out[2 * n + i] = rgba[p + 2] / 255;
  }
  return out;
}

export function iou(a, b) {
  const x1 = Math.max(a.x1, b.x1), y1 = Math.max(a.y1, b.y1);
  const x2 = Math.min(a.x2, b.x2), y2 = Math.min(a.y2, b.y2);
  const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
  const ua = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter;
  return ua > 0 ? inter / ua : 0;
}

export function nms(boxes, thr = 0.5) {
  const sorted = [...boxes].sort((a, b) => b.conf - a.conf);
  const keep = [];
  for (const b of sorted) if (keep.every((k) => k.cls !== b.cls || iou(k, b) < thr)) keep.push(b);
  return keep;
}

/**
 * Decode an Ultralytics detect output.
 * data: Float32Array, dims: [1, 4+nc, N] (or transposed [1, N, 4+nc]).
 * lb: letterboxParams; srcW/srcH: original frame size. Returns boxes in source pixels.
 */
export function decode(data, dims, lb, srcW, srcH, confThres = 0.35, iouThres = 0.5) {
  let C = dims[1], N = dims[2], transposed = false;
  if (C > N) { [C, N] = [N, C]; transposed = true; }
  const at = transposed ? (c, i) => data[i * C + c] : (c, i) => data[c * N + i];
  const nc = C - 4;
  const boxes = [];
  for (let i = 0; i < N; i++) {
    let best = 0, cls = 0;
    for (let c = 0; c < nc; c++) {
      const s = at(4 + c, i);
      if (s > best) { best = s; cls = c; }
    }
    if (best < confThres) continue;
    const cx = at(0, i), cy = at(1, i), w = at(2, i), h = at(3, i);
    const x1 = (cx - w / 2 - lb.padX) / lb.r, y1 = (cy - h / 2 - lb.padY) / lb.r;
    const x2 = (cx + w / 2 - lb.padX) / lb.r, y2 = (cy + h / 2 - lb.padY) / lb.r;
    boxes.push({
      x1: Math.max(0, x1), y1: Math.max(0, y1), x2: Math.min(srcW, x2), y2: Math.min(srcH, y2),
      conf: best, cls,
    });
  }
  return nms(boxes, iouThres);
}

// ------------------------------------------------------------------ browser runtime
let ortPromise;
function loadOrt() {
  if (globalThis.ort) return Promise.resolve(globalThis.ort);
  ortPromise ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `${ORT_BASE}ort.min.js`;
    s.onload = () => {
      globalThis.ort.env.wasm.wasmPaths = ORT_BASE;
      globalThis.ort.env.wasm.numThreads = 1; // no cross-origin isolation on a plain tunnel
      resolve(globalThis.ort);
    };
    s.onerror = () => reject(new Error("Could not load the AI runtime (needs internet the first time)."));
    document.head.append(s);
  });
  return ortPromise;
}

export class PotholeModel {
  static async load(url, meta = {}) {
    const ort = await loadOrt();
    const session = await ort.InferenceSession.create(url, { executionProviders: ["wasm"], graphOptimizationLevel: "all" });
    return new PotholeModel(ort, session, meta);
  }

  constructor(ort, session, meta) {
    this.ort = ort;
    this.session = session;
    this.size = meta.imgsz || 640;
    this.conf = meta.conf || 0.35;
    this.canvas = document.createElement("canvas");
    this.canvas.width = this.canvas.height = this.size;
    this.ctx = this.canvas.getContext("2d", { willReadFrequently: true });
    this.lastMs = 0;
  }

  /** source: <video> or <canvas>. Returns boxes in source pixels. */
  async detect(source) {
    const srcW = source.videoWidth || source.width, srcH = source.videoHeight || source.height;
    if (!srcW || !srcH) return [];
    const lb = letterboxParams(srcW, srcH, this.size);
    const t0 = performance.now();
    this.ctx.fillStyle = "rgb(114,114,114)";
    this.ctx.fillRect(0, 0, this.size, this.size);
    this.ctx.drawImage(source, lb.padX, lb.padY, lb.w, lb.h);
    const rgba = this.ctx.getImageData(0, 0, this.size, this.size).data;
    const input = new this.ort.Tensor("float32", toCHW(rgba, this.size), [1, 3, this.size, this.size]);
    const out = await this.session.run({ [this.session.inputNames[0]]: input });
    const t = out[this.session.outputNames[0]];
    this.lastMs = performance.now() - t0;
    return decode(t.data, t.dims, lb, srcW, srcH, this.conf);
  }
}
