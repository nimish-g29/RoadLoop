// Small shared helpers. Pure functions here are unit-tested in Node (tests/js).

export const EARTH_R = 6371008.8;

export function haversine(lat1, lon1, lat2, lon2) {
  const toRad = (d) => (d * Math.PI) / 180;
  const p1 = toRad(lat1), p2 = toRad(lat2);
  const dp = p2 - p1, dl = toRad(lon2 - lon1);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_R * Math.asin(Math.sqrt(a));
}

export function uuid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID().replace(/-/g, "");
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

export function median(values) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

// --------------------------------------------------------------- DOM (browser only)
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

let toastTimer;
export function toast(msg, kind = "") {
  let t = document.getElementById("toast");
  if (!t) {
    t = h("div", { id: "toast", role: "status", "aria-live": "polite" });
    document.body.append(t);
  }
  t.textContent = msg;
  t.className = `show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 3500);
}

export const STATE_LABEL = {
  candidate: "Waiting for a 2nd reporter",
  confirmed: "Confirmed: needs your approval",
  filing: "Filing on the portal…",
  filing_failed: "Needs you (OTP / retry)",
  filed: "Complaint filed",
  in_progress: "Portal: work in progress",
  awaiting_verification: "Portal says fixed: re-checking",
  verified_fixed: "Verified fixed ✓",
  reopened: "Still broken: reopened",
  escalated: "Escalated",
};
