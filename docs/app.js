(function () {
  "use strict";

  function el(tag, className, html) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }

  // Placeholder de cartel: un tile con gradiente derivado del título (nunca
  // el mismo gris "roto" para todo) + icono de claqueta. Solo se usa si de
  // verdad no hay póster (en producción casi siempre lo habrá).
  function hashHue(str) {
    let h = 0;
    for (let i = 0; i < (str || "").length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
    return h % 360;
  }

  function posterOrPlaceholder(url, title) {
    if (url) return url;
    const hue = hashHue(title || "film");
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300">` +
      `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">` +
      `<stop offset="0" stop-color="hsl(${hue},38%,22%)"/>` +
      `<stop offset="1" stop-color="hsl(${(hue + 40) % 360},32%,12%)"/>` +
      `</linearGradient></defs>` +
      `<rect width="100%" height="100%" fill="url(#g)"/>` +
      `<text x="50%" y="54%" font-size="46" text-anchor="middle" font-family="sans-serif">🎬</text>` +
      `</svg>`;
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  }

  function posterImg(url, title) {
    const img = el("img", "card-poster");
    img.src = posterOrPlaceholder(url, title);
    img.alt = title || "";
    img.draggable = false;
    img.loading = "lazy";
    img.addEventListener("error", () => {
      img.src = posterOrPlaceholder(null, title);
    });
    return img;
  }

  function buildStreamingCard(item) {
    const card = el("div", "card");
    card.appendChild(posterImg(item.poster, item.title));
    const body = el("div", "card-body");
    const dayLabel = item.day ? item.day.charAt(0).toUpperCase() + item.day.slice(1) : "";
    body.appendChild(el("span", "card-badge", [dayLabel, item.platform].filter(Boolean).join(" · ")));
    body.appendChild(el("p", "card-title", item.title || ""));
    body.appendChild(el("p", "card-reason", item.reason || ""));
    card.appendChild(body);
    card.addEventListener("click", () => openStreamingModal(item));
    return card;
  }

  // Las fichas de cine son una FILA (póster + info + cines/horarios visibles
  // directamente), no una tarjeta cuadrada — así se ve de un vistazo dónde y
  // a qué hora, sin tener que tocar nada.
  function buildCinemaRow(item) {
    const row = el("div", "cinema-item");
    row.appendChild(posterImg(item.poster, item.title));

    const info = el("div", "cinema-item-info");
    info.appendChild(el("p", "cinema-item-title", item.title || ""));
    info.appendChild(el("p", "card-reason", item.reason || ""));

    const list = el("div", "cinema-item-showings");
    (item.cinemas || []).forEach((c) => {
      const line = el("div", "showing-line");
      const name = el("span", "showing-cinema", c.name);
      line.appendChild(name);
      const times = el("span", "showing-times", (c.showtimes || []).join(" · ") || "horario en la web del cine");
      line.appendChild(times);
      if (c.listing_url) {
        line.style.cursor = "pointer";
        line.title = "Ver horarios y entradas";
        line.addEventListener("click", (ev) => {
          ev.stopPropagation();
          window.open(c.listing_url, "_blank", "noopener");
        });
      }
      list.appendChild(line);
    });
    info.appendChild(list);

    const link = el("a", "imdb-inline-link", "Ficha en IMDb ↗");
    link.href = item.imdb_url || "#";
    link.target = "_blank";
    link.rel = "noopener";
    link.addEventListener("click", (ev) => ev.stopPropagation());
    info.appendChild(link);

    row.appendChild(info);
    return row;
  }

  function openModal(contentEl) {
    const backdrop = document.getElementById("modal-backdrop");
    const content = document.getElementById("modal-content");
    content.innerHTML = "";
    const closeBtn = el("button", "close-btn", "✕");
    closeBtn.addEventListener("click", closeModal);
    content.appendChild(closeBtn);
    content.appendChild(contentEl);
    backdrop.hidden = false;
  }

  function closeModal() {
    document.getElementById("modal-backdrop").hidden = true;
  }

  document.getElementById("modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
  });

  function openStreamingModal(item) {
    const wrap = el("div");
    wrap.appendChild(el("h3", "", item.title || ""));
    const dayLabel = item.day ? item.day.charAt(0).toUpperCase() + item.day.slice(1) + " · " : "";
    wrap.appendChild(
      el(
        "p",
        "modal-meta",
        `${dayLabel}Disponible en ${item.platform || "—"}${item.rating ? " · IMDb " + item.rating : ""}`
      )
    );
    if (item.reason) wrap.appendChild(el("p", "", item.reason));
    const a = el("a", "imdb-link", "Ver ficha en IMDb");
    a.href = item.imdb_url || "#";
    a.target = "_blank";
    a.rel = "noopener";
    wrap.appendChild(a);
    openModal(wrap);
  }

  async function main() {
    let data;
    try {
      const res = await fetch("data.json", { cache: "no-store" });
      data = await res.json();
    } catch (e) {
      document.getElementById("loading").textContent =
        "No se pudo cargar la propuesta de esta semana. Vuelve a intentarlo más tarde.";
      return;
    }

    document.getElementById("loading").hidden = true;
    document.getElementById("week-label").textContent = data.week_label || "";
    document.getElementById("generated-at").textContent = data.generated_at
      ? "Generado " + data.generated_at.replace("T", " ")
      : "";

    // Weekend / streaming
    const weekendSection = document.getElementById("weekend-section");
    weekendSection.hidden = false;
    document.getElementById("weekend-range").textContent =
      "(" + ((data.weekend && data.weekend.range) || "") + ")";
    const weekendGrid = document.getElementById("weekend-grid");
    const weekendPicks = (data.weekend && data.weekend.picks) || [];
    if (weekendPicks.length === 0) {
      document.getElementById("weekend-empty").hidden = false;
    } else {
      weekendPicks.forEach((p) => weekendGrid.appendChild(buildStreamingCard(p)));
    }

    // Cinema week — lista de filas, no grid de tarjetas
    const cinemaSection = document.getElementById("cinema-section");
    cinemaSection.hidden = false;
    document.getElementById("cinema-range").textContent =
      "(" + ((data.cinema_week && data.cinema_week.range) || "") + ")";
    const cinemaList = document.getElementById("cinema-grid");
    cinemaList.classList.add("cinema-list");
    const cinemaPicks = (data.cinema_week && data.cinema_week.picks) || [];
    if (cinemaPicks.length === 0) {
      document.getElementById("cinema-empty").hidden = false;
    } else {
      cinemaPicks.forEach((p) => cinemaList.appendChild(buildCinemaRow(p)));
    }

    // Errors (best-effort, no bloqueante)
    if (data.errors && data.errors.length) {
      const errBox = document.getElementById("errors");
      errBox.hidden = false;
      errBox.textContent =
        "Aviso: algunas fuentes fallaron al generar esta propuesta (" +
        data.errors.join(" · ") +
        ")";
    }
  }

  // Cartela de inicio: 3 segundos fijos, en paralelo a la carga de datos
  // (no la retrasa ni depende de ella) — arranca el contador nada más
  // ejecutarse el script, no dentro de main(), para que los 3s sean
  // siempre los mismos independientemente de lo que tarde el fetch.
  const SPLASH_DURATION_MS = 3000;
  window.setTimeout(() => {
    const splash = document.getElementById("splash");
    if (!splash) return;
    splash.classList.add("splash-hide");
    window.setTimeout(() => {
      splash.hidden = true;
    }, 450); // deja terminar la transición de opacidad del CSS antes de quitarla del todo
  }, SPLASH_DURATION_MS);

  main();

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("sw.js").catch(() => {});
    });
  }
})();
