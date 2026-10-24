// Report mode: walking past a pothole? Take a photo, tap the pothole, pick its size, send.
// Location is attached automatically; everything except the tapped area is blurred on the phone.
import { redact, toJpeg } from "./redact.js";
import { clamp, h, toast, uuid } from "./util.js";

const SIZES = [
  { key: "small", label: "Small", hint: "crack, shallow dip", area: 0.01 },
  { key: "medium", label: "Medium", hint: "about a wheel wide", area: 0.035 },
  { key: "large", label: "Large", hint: "crater, wheel drops in", area: 0.08 },
];

export function mountReport(root, { gps, uploader, getRiderId }) {
  let canvas = null, point = null, size = null, fix = null, fixPromise = null;

  const fileInput = h("input", { type: "file", accept: "image/*", capture: "environment", hidden: true });
  const stage = h("div", { class: "photo-stage" });
  const gpsLine = h("p", { class: "gps-line" }, "Location is added automatically.");
  const hint = h("p", { class: "hint" });
  const sizes = h("div", { class: "chips" });
  const sendBtn = h("button", { class: "btn primary big", disabled: true }, "Send report");
  const retake = h("button", { class: "btn ghost" }, "Retake");
  const start = h("button", { class: "btn primary big" }, "📷  Take a photo of the pothole");

  const empty = h("div", { class: "empty-state" },
    h("div", { class: "illus", "aria-hidden": "true" }, "◎"),
    h("h2", {}, "Report a pothole"),
    h("p", {}, "Stand safely off the road. Photograph the pothole, tap it on the photo, choose its size. That's it."),
    start,
  );
  const editor = h("div", { class: "editor", hidden: true }, stage, hint, sizes, gpsLine,
    h("div", { class: "row" }, retake, sendBtn));
  root.append(h("section", { class: "pane" }, empty, editor, fileInput));

  SIZES.forEach((s) => {
    const b = h("button", { class: "chip", "data-k": s.key }, h("b", {}, s.label), h("small", {}, s.hint));
    b.addEventListener("click", () => {
      size = s;
      sizes.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
      redraw();
      update();
    });
    sizes.append(b);
  });

  function update() {
    hint.textContent = !point ? "Tap the pothole on the photo." : !size ? "How big is it?" : "Looks good.";
    sendBtn.disabled = !(canvas && point && size && fix);
  }

  function showFix(f) {
    fix = f;
    gpsLine.textContent = f
      ? `📍 Location locked (±${Math.round(f.accuracy || 0)} m)${(f.accuracy || 0) > 50 ? " · weak signal, move into the open if you can" : ""}`
      : gps.error || "📍 Getting your location…";
    gpsLine.classList.toggle("warn", !f || (f.accuracy || 0) > 50);
    update();
  }

  function boxFor(p, s) {
    const bw = canvas.width * clamp(Math.sqrt(s.area) * 2.2, 0.18, 0.6);
    const bh = bw * 0.7;
    return { x1: p.x - bw / 2, y1: p.y - bh / 2, x2: p.x + bw / 2, y2: p.y + bh / 2 };
  }

  function redraw() {
    if (!canvas) return;
    const view = stage.querySelector("canvas");
    const ctx = view.getContext("2d");
    ctx.drawImage(canvas, 0, 0);
    if (!point) return;
    const b = boxFor(point, size || SIZES[1]);
    ctx.lineWidth = Math.max(3, canvas.width / 250);
    ctx.strokeStyle = "#F5A623";
    ctx.setLineDash([12, 8]);
    ctx.strokeRect(b.x1, b.y1, b.x2 - b.x1, b.y2 - b.y1);
  }

  start.addEventListener("click", () => {
    fixPromise = gps.fixOnce(25, 20000); // start warming GPS while the camera is open
    fileInput.value = "";
    fileInput.click();
  });
  retake.addEventListener("click", () => start.click());

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    const img = new Image();
    img.src = URL.createObjectURL(file);
    try {
      await img.decode(); // browsers apply EXIF rotation to <img> automatically
    } catch {
      toast("Could not read that photo.", "error");
      return;
    }
    const s = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));
    canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * s);
    canvas.height = Math.round(img.naturalHeight * s);
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(img.src);
    point = null;
    size = null;
    sizes.querySelectorAll(".chip").forEach((c) => c.classList.remove("on"));
    const view = h("canvas", { width: canvas.width, height: canvas.height, "aria-label": "Your photo. Tap the pothole." });
    view.addEventListener("click", (e) => {
      const r = view.getBoundingClientRect();
      point = { x: ((e.clientX - r.left) / r.width) * canvas.width, y: ((e.clientY - r.top) / r.height) * canvas.height };
      redraw();
      update();
    });
    stage.replaceChildren(view);
    redraw();
    empty.hidden = true;
    editor.hidden = false;
    showFix(gps.track.last);
    const f = await (fixPromise || gps.fixOnce(25, 20000));
    showFix(f || gps.track.last);
  });

  const unsub = gps.onChange((f) => {
    if (!editor.hidden && f && (!fix || (f.accuracy || 999) < (fix.accuracy || 999))) showFix(f);
  });

  sendBtn.addEventListener("click", async () => {
    sendBtn.disabled = true;
    try {
      const blob = await toJpeg(redact(canvas, boxFor(point, size), 0.3));
      const meta = {
        rider_id: getRiderId(),
        lat: fix.lat,
        lon: fix.lon,
        area_frac: size.area,
        model_conf: 0.75,
        jolt_g: null,
        speed_kmh: null,
        gps_accuracy_m: fix.accuracy ? Math.round(fix.accuracy * 10) / 10 : null,
        captured_at: new Date().toISOString(),
        client_id: uuid(),
        source: "manual",
      };
      await uploader.add(meta, blob);
      toast(navigator.onLine ? "Report saved and sending…" : "Saved. It will send when you're back online.", "ok");
      canvas = point = size = null;
      editor.hidden = true;
      empty.hidden = false;
    } catch (e) {
      toast(e.message, "error");
      sendBtn.disabled = false;
    }
  });

  return { destroy: unsub };
}
