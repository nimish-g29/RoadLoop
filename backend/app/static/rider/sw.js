// Network-first for the app shell (always fresh when online, still opens offline).
// API calls and photos are never cached; uploads wait in IndexedDB instead.
const CACHE = "roadloop-v2";
const SHELL = [
  "./", "index.html", "styles.css", "manifest.webmanifest", "icon-192.png",
  "js/app.js", "js/api.js", "js/drive.js", "js/queue.js", "js/redact.js", "js/report.js",
  "js/reports.js", "js/sensors.js", "js/tracker.js", "js/util.js", "js/yolo.js",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/app/")) return;
  if (url.pathname.endsWith(".onnx")) {
    // The model is large and rarely changes: cache-first.
    e.respondWith(caches.open(CACHE).then(async (c) => (await c.match(e.request)) ||
      fetch(e.request).then((r) => { if (r.ok) c.put(e.request, r.clone()); return r; })));
    return;
  }
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok) caches.open(CACHE).then((c) => c.put(e.request, r.clone()));
        return r;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match("index.html"))),
  );
});
