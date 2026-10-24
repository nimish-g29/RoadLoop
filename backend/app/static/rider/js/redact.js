// Privacy before upload: only the pothole region stays sharp. Everything else (number plates,
// people, shopfronts) is heavily blurred by down/up-scaling, which works in every browser
// (ctx.filter is missing on some iOS versions). Re-encoding to JPEG also drops all EXIF.

export function snapshot(source, maxSide = 1280) {
  const sw = source.videoWidth || source.naturalWidth || source.width;
  const sh = source.videoHeight || source.naturalHeight || source.height;
  const s = Math.min(1, maxSide / Math.max(sw, sh));
  const c = document.createElement("canvas");
  c.width = Math.round(sw * s);
  c.height = Math.round(sh * s);
  c.getContext("2d").drawImage(source, 0, 0, c.width, c.height);
  c.scale = s; // source px -> snapshot px
  return c;
}

export function expandBox(box, w, h, frac = 0.4) {
  const bw = box.x2 - box.x1, bh = box.y2 - box.y1;
  return {
    x1: Math.max(0, Math.floor(box.x1 - bw * frac)), y1: Math.max(0, Math.floor(box.y1 - bh * frac)),
    x2: Math.min(w, Math.ceil(box.x2 + bw * frac)), y2: Math.min(h, Math.ceil(box.y2 + bh * frac)),
  };
}

/** canvas: a snapshot; box: in the snapshot's pixels. Returns a new redacted canvas. */
export function redact(canvas, box, keepFrac = 0.4) {
  const { width: w, height: h } = canvas;
  const out = document.createElement("canvas");
  out.width = w;
  out.height = h;
  const ctx = out.getContext("2d");
  const small = document.createElement("canvas");
  const k = 28;
  small.width = Math.max(1, Math.round(w / k));
  small.height = Math.max(1, Math.round(h / k));
  small.getContext("2d").drawImage(canvas, 0, 0, small.width, small.height);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(small, 0, 0, w, h);
  const r = expandBox(box, w, h, keepFrac);
  ctx.drawImage(canvas, r.x1, r.y1, r.x2 - r.x1, r.y2 - r.y1, r.x1, r.y1, r.x2 - r.x1, r.y2 - r.y1);
  return out;
}

export function toJpeg(canvas, quality = 0.8) {
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("JPEG encode failed"))), "image/jpeg", quality),
  );
}
