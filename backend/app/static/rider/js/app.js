// RoadLoop volunteer app: onboarding, three tabs (Drive / Report / My reports), offline queue.
import { api } from "./api.js";
import { DriveSession } from "./drive.js";
import { Uploader } from "./queue.js";
import { mountReport } from "./report.js";
import { mountReports } from "./reports.js";
import { Gps, Motion } from "./sensors.js";
import { STATE_LABEL, h, toast } from "./util.js";

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* ignore */ } },
};

const state = { riderId: store.get("rl.rider"), riderName: store.get("rl.name"), config: null };
const gps = new Gps();
const motion = new Motion();
const uploader = new Uploader();
const $ = (sel) => document.querySelector(sel);

// ------------------------------------------------------------------ onboarding
function showOnboarding() {
  $("#app").hidden = true;
  const root = $("#onboarding");
  root.hidden = false;
  const name = h("input", { id: "ob-name", required: true, autocomplete: "name", placeholder: "Your name", maxlength: 120 });
  const phone = h("input", { id: "ob-phone", required: true, inputmode: "tel", autocomplete: "tel",
    placeholder: "10-digit mobile number", pattern: "\\+?[0-9]{10,13}" });
  const checks = [
    "My camera is processed on this phone. Only photos of potholes are uploaded, with everything else blurred.",
    "My location is attached to each report.",
    "A complaint is filed in my name only after I tap Approve.",
  ].map((t, i) => h("label", { class: "check" }, h("input", { type: "checkbox", required: true, id: `ob-c${i}` }), h("span", {}, t)));
  const err = h("p", { class: "error", hidden: true });
  const submit = h("button", { class: "btn primary big", type: "submit" }, "Start helping");
  const form = h("form", { class: "ob-form" },
    h("div", { class: "brand big" }, h("span", { class: "logo" }, "◎"), "RoadLoop"),
    h("p", { class: "lead" }, "Potholes you pass get reported, filed with the city and followed up until they're actually fixed."),
    h("label", { for: "ob-name" }, "Name"), name,
    h("label", { for: "ob-phone" }, "Mobile number"), phone,
    h("p", { class: "muted small" }, "Grievance portals require a mobile number. It is used only for complaints you approve."),
    h("fieldset", {}, h("legend", {}, "Before you start"), ...checks),
    err, submit,
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    err.hidden = true;
    try {
      const r = await api.register(name.value.trim(), phone.value.replace(/[\s-]/g, ""));
      state.riderId = r.id;
      state.riderName = r.display_name;
      store.set("rl.rider", r.id);
      store.set("rl.name", r.display_name);
      root.hidden = true;
      startApp();
    } catch (ex) {
      err.textContent = ex.status === 0 ? "No connection to the RoadLoop server." : `Could not register: ${ex.message}`;
      err.hidden = false;
      submit.disabled = false;
    }
  });
  root.replaceChildren(form);
}

// ------------------------------------------------------------------ drive tab
function mountDrive(root) {
  const video = h("video", { playsinline: true, muted: true, autoplay: true });
  const overlay = h("canvas", { class: "overlay" });
  const pills = {
    gps: h("span", { class: "pill" }, "GPS –"),
    speed: h("span", { class: "pill" }, "– km/h"),
    ai: h("span", { class: "pill" }, "AI –"),
    jolt: h("span", { class: "pill" }, "jolt –"),
    sent: h("span", { class: "pill" }, "0 sent"),
  };
  const modelNote = h("p", { class: "model-note", hidden: true });
  const startBtn = h("button", { class: "btn primary big" }, "Start driving");
  const stopBtn = h("button", { class: "btn danger big", hidden: true }, "Stop");
  const markBtn = h("button", { class: "btn mark big", hidden: true, "aria-label": "Mark a pothole now" }, "Mark pothole (passenger)");

  // Demo / testing options
  const srcCam = h("input", { type: "radio", name: "src", value: "camera", checked: true });
  const srcFile = h("input", { type: "radio", name: "src", value: "file" });
  const fileIn = h("input", { type: "file", accept: "video/*" });
  const simOn = h("input", { type: "checkbox" });
  const simLat = h("input", { value: store.get("rl.sim") || "28.54612,77.27215", "aria-label": "Start lat,lon" });
  const simHead = h("input", { type: "number", value: "90", "aria-label": "Heading degrees" });
  const simSpeed = h("input", { type: "number", value: "25", "aria-label": "Speed km/h" });
  const options = h("details", { class: "options" }, h("summary", {}, "Demo options"),
    h("label", { class: "check" }, srcCam, h("span", {}, "Phone camera")),
    h("label", { class: "check" }, srcFile, h("span", {}, "Recorded dashcam video "), fileIn),
    h("label", { class: "check" }, simOn, h("span", {}, "Simulated GPS (for laptops / indoor demo)")),
    h("div", { class: "row" }, simLat, simHead, simSpeed),
    h("p", { class: "muted small" }, "lat,lon · heading° · km/h"),
  );

  const idle = h("div", { class: "idle" },
    h("h2", {}, "Drive mode"),
    h("ul", { class: "how" },
      h("li", {}, "Mount the phone facing the road, plugged in to charge."),
      h("li", {}, "The camera stays on, but nothing is recorded or uploaded unless a pothole is seen."),
      h("li", {}, "Hands-free: hit a pothole and it is captured automatically. Never touch the phone while riding."),
      h("li", {}, "Each pothole sends one photo (everything else blurred), its location and how hard you hit it."),
    ),
    modelNote, options,
  );
  const cam = h("div", { class: "cam" }, video, overlay, h("div", { class: "hud" }, ...Object.values(pills)), idle);
  root.append(h("section", { class: "pane drive" }, cam, h("div", { class: "drive-controls" }, startBtn, markBtn, stopBtn)));

  let session = null;
  const setNote = (text, kind = "") => {
    modelNote.hidden = !text;
    modelNote.textContent = text;
    modelNote.className = `model-note ${kind}`;
  };
  if (!state.config?.model_url) {
    setNote("No pothole model installed yet: potholes are captured hands-free when you hit one (jolt). A passenger can also tap “Mark pothole”.", "warn");
  }

  gps.onChange((f, e) => {
    pills.gps.textContent = f ? `GPS ±${Math.round(f.accuracy || 0)} m` : e ? "GPS ✕" : "GPS …";
    pills.gps.classList.toggle("bad", !f || (f.accuracy || 0) > 30);
    pills.speed.textContent = f?.speedKmh != null ? `${Math.round(f.speedKmh)} km/h` : "– km/h";
  });

  startBtn.addEventListener("click", () => {
    const useFile = srcFile.checked;
    let sim = null;
    if (simOn.checked) {
      const [lat, lon] = simLat.value.split(",").map(Number);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return toast("Simulated GPS needs lat,lon", "error");
      store.set("rl.sim", simLat.value);
      sim = { lat, lon, heading: Number(simHead.value) || 0, speed: Number(simSpeed.value) || 25 };
    }
    session = new DriveSession({ video, overlay, gps, motion, uploader, riderId: state.riderId, config: state.config });
    session.addEventListener("stats", (e) => {
      const s = e.detail;
      pills.ai.textContent = session.model ? `AI ${s.fps} fps · ${s.inferMs} ms` : "AI off";
      pills.jolt.textContent = s.jolt == null ? "jolt n/a" : `jolt ${s.jolt.toFixed(2)} g`;
      pills.sent.textContent = `${s.sent} sent${s.passes ? ` · ${s.passes} re-checked` : ""}`;
    });
    session.addEventListener("status", (e) => {
      const m = e.detail.model;
      if (m === "loading") setNote("Loading the pothole model…");
      else if (m === "ready") setNote("");
      else if (m === "error") setNote(`Model failed: ${e.detail.error}. Tap “Mark pothole” instead.`, "warn");
    });
    session.addEventListener("detection", () => toast("Pothole captured ✓", "ok"));
    session.addEventListener("bump", () => navigator.vibrate?.([40, 60, 40]));
    session.addEventListener("pass", () => toast("Re-checked a repaired spot: road looks clear ✓", "ok"));
    session.addEventListener("error", (e) => toast(e.detail, "error"));
    session.addEventListener("state", (e) => {
      const on = e.detail.running;
      idle.hidden = on;
      startBtn.hidden = on;
      stopBtn.hidden = !on;
      markBtn.hidden = !on;
      document.body.classList.toggle("driving", on);
    });
    // Must be called synchronously in the tap: iOS motion permission + camera prompt.
    session.start({ source: useFile ? "file" : "camera", file: fileIn.files?.[0], sim }).catch((e) => {
      toast(e.name === "NotAllowedError" ? "Camera permission denied." : e.message, "error");
      session?.stop();
    });
  });
  stopBtn.addEventListener("click", () => session?.stop());
  markBtn.addEventListener("click", () => {
    if (session?.mark()) {
      navigator.vibrate?.(60);
      toast("Marked. Sending after the bump…", "ok");
    }
  });
  window.addEventListener("pagehide", () => session?.stop());
}

// --------------------------------------------------------------------- shell
let reportsView;

function switchTab(name) {
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
  store.set("rl.tab", name);
  if (name === "reports") reportsView?.refresh();
  if (name !== "drive") gps.start();
}

async function startApp() {
  $("#app").hidden = false;
  $("#who").textContent = state.riderName || "";
  try {
    state.config = await api.config();
  } catch {
    state.config = { model_url: null, model_meta: {}, verify_radius_m: 25 };
  }
  mountDrive($("#view-drive"));
  mountReport($("#view-report"), { gps, uploader, getRiderId: () => state.riderId });
  reportsView = mountReports($("#view-reports"), { getRiderId: () => state.riderId, uploader });

  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  switchTab(store.get("rl.tab") || "drive");

  const badge = $("#queue-badge");
  uploader.addEventListener("count", (e) => {
    badge.hidden = !e.detail;
    badge.textContent = `${e.detail} waiting`;
  });
  uploader.addEventListener("uploaded", (e) => {
    const st = e.detail.res?.hotspot_state;
    if (e.detail.meta.source === "manual" || st === "confirmed") toast(`Sent ✓ ${STATE_LABEL[st] || ""}`, "ok");
  });
  uploader.addEventListener("rejected", (e) => {
    if (/unknown rider/i.test(e.detail.error.message)) return resetIdentity();
    toast(`A report was rejected: ${e.detail.error.message}`, "error");
  });
  const net = () => $("#net").classList.toggle("off", !navigator.onLine);
  window.addEventListener("online", net);
  window.addEventListener("offline", net);
  net();
  uploader.flush();
}

function resetIdentity() {
  store.set("rl.rider", null);
  store.set("rl.name", null);
  state.riderId = null;
  toast("The server was reset. Please register again.", "error");
  setTimeout(() => location.reload(), 1500);
}

async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("./sw.js").catch(() => {});
  if (!window.isSecureContext) {
    $("#secure-warning").hidden = false; // camera + GPS are blocked on plain http (except localhost)
  }
  if (!state.riderId) return showOnboarding();
  try {
    await api.rider(state.riderId);
  } catch (e) {
    if (e.status === 404) {
      store.set("rl.rider", null);
      return showOnboarding();
    }
    // offline: carry on with the stored identity
  }
  startApp();
}

boot();
