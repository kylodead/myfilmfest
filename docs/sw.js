// Service worker mínimo: cachea el shell de la app para que funcione
// offline con la última versión que se llegó a descargar. data.json siempre
// se pide a red (ver app.js, fetch con cache: "no-store").
//
// IMPORTANTE — bug real que tenía esto antes: el resto del shell (app.js,
// styles.css...) se servía "caché primero" bajo un nombre de caché fijo
// ("v1") que nunca cambiaba entre despliegues. Como el propio fichero sw.js
// no cambiaba de una semana a otra, el navegador nunca volvía a ejecutar
// "install" para refrescar esa caché — así que una vez guardado un app.js
// o styles.css antiguo, se podía quedar sirviendo esa versión vieja
// indefinidamente, aunque GitHub Pages ya tuviera la nueva. Esto es muy
// probablemente la explicación real de por qué el arreglo del CSS tardó en
// verse (no solo la caché normal del navegador).
//
// Arreglado a "red primero, caché solo si no hay conexión": así, estando
// online (el caso normal), siempre se pide la versión más reciente; la
// caché queda solo como red de seguridad para cuando no hay internet.
const CACHE_NAME = "myfilmfest-shell";
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
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches
          .open(CACHE_NAME)
          .then((cache) => cache.put(event.request, copy))
          .catch(() => {});
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
