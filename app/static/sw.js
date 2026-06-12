// Service worker: установка PWA + кэш статики + офлайн-заглушка.
const CACHE = "nvrmon-v1";
const SHELL = [
  "/static/style.css", "/static/app.js", "/static/notify.js",
  "/static/icons/icon-192.png", "/offline",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // действия (POST/PUT) — всегда напрямую в сеть
  const url = new URL(req.url);
  // статика — из кэша, потом сеть
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(req).then((r) => r || fetch(req).then((resp) => {
      caches.open(CACHE).then((c) => c.put(req, resp.clone()));
      return resp;
    })));
    return;
  }
  // страницы — сеть, при оффлайне → заглушка
  e.respondWith(fetch(req).catch(() => caches.match(req).then((r) => r || caches.match("/offline"))));
});
