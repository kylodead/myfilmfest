// Service worker mínimo: cachea el shell de la app para que abra rápido y
// funcione offline con la última propuesta descargada. data.json siempre se
// pide a red (ver app.js, fetch con cache: "no-store"), así que nunca verás
// una recomendación desactualizada por culpa de la caché.
const CACHE_NAME = "myfilmfest-shell-v1";
const SHELL_FILES = ["./", "./index.html", "./styles.css", "./app.js", "./manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.endsWith("data.json")) {
    return; // siempre red
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
