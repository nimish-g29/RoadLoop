const BASE = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

async function req(path, opts = {}) {
  const r = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail || detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return r.json();
}

export const api = {
  queue: (params = {}) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v === undefined || v === null || v === "") return;
      (Array.isArray(v) ? v : [v]).forEach((x) => q.append(k, x));
    });
    return req(`/v1/hotspots?${q}`);
  },
  hotspot: (id) => req(`/v1/hotspots/${id}`),
  stats: () => req("/v1/stats"),
  runJob: (job) => req(`/v1/admin/run/${job}`, { method: "POST" }),
};
