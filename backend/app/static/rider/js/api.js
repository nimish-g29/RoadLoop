// API client. Same origin as the backend (served at /app), so no CORS and one tunnel.
const BASE = (() => {
  try {
    return (localStorage.getItem("rl.api") || "").replace(/\/$/, "");
  } catch {
    return "";
  }
})();

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
  get permanent() {
    return this.status >= 400 && this.status < 500 && ![408, 429].includes(this.status);
  }
}

async function call(path, opts = {}) {
  let r;
  try {
    r = await fetch(BASE + path, opts);
  } catch (e) {
    throw new ApiError(0, "offline");
  }
  let body = null;
  try {
    body = await r.json();
  } catch {
    /* empty */
  }
  if (!r.ok) {
    const detail = body?.detail;
    throw new ApiError(r.status, typeof detail === "string" ? detail : JSON.stringify(detail || r.statusText));
  }
  return body;
}

const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const api = {
  config: () => call("/v1/config"),
  register: (name, phone) => call("/v1/riders", json("POST", { display_name: name, phone: phone || null, consent: true })),
  rider: (id) => call(`/v1/riders/${id}`),
  myHotspots: (id) => call(`/v1/riders/${id}/hotspots`),
  tasks: (id, lat, lon) => call(`/v1/riders/${id}/tasks?lat=${lat}&lon=${lon}`),
  approve: (hid, rid) => call(`/v1/hotspots/${hid}/approve`, json("POST", { rider_id: rid })),
  pass: (hid, rid, lat, lon) => call(`/v1/hotspots/${hid}/passes`, json("POST", { rider_id: rid, lat, lon })),
  uploadDetection(meta, blob) {
    const fd = new FormData();
    fd.append("meta", JSON.stringify(meta));
    if (blob) fd.append("photo", blob, "hazard.jpg");
    return call("/v1/detections", { method: "POST", body: fd });
  },
};
