"""
Novedades de streaming de la semana en tus plataformas (Disney+, Filmin,
Movistar Plus+, Netflix, Prime Video) para España.

HISTORIAL DE ESTE FICHERO — importante para entender por qué está así:
La primera versión usaba la librería "simple-justwatch-python-api" (la API
GraphQL que usa justwatch.com internamente) para buscar "estrenos recientes"
por AÑO DE ESTRENO EN CINE (`release_date`/`min_release_year`). El problema,
confirmado en la práctica: eso NO es lo mismo que "recién añadido a tu
plataforma". La mayoría de lo que Netflix/Prime/etc. añaden cada semana es
catálogo (películas de hace años que se licencian esta semana), no estrenos
de cine recientes — así que casi todo quedaba fuera del filtro, y lo poco
que pasaba muchas veces ni siquiera era relevante.

La solución: JustWatch tiene, en su propia web (no en la librería/API
pública), una página por plataforma que SÍ muestra la fecha real en que
cada título se añadió — "https://www.justwatch.com/es/proveedor/{slug}/nuevo/peliculas"
(comprobado a mano, título por título, con fechas como "Ayer", "25 de
agosto de 2026"...). Así que ahora leemos esas páginas directamente, como ya
hacíamos con la cartelera de cines — mismo estilo de scraper.

Aviso de fragilidad honesto: los "slugs" (el trozo de la URL que identifica
cada plataforma) de Netflix/Disney+/Amazon/Filmin son estables, pero el de
Movistar Plus+ incluye el PRECIO de la suscripción en la propia URL
("movistar-plus-eu9-99") — si Movistar sube la tarifa, esa URL cambiará y
ese proveedor concreto empezará a devolver 0 resultados hasta que se
actualice el slug aquí. Está diseñado para fallar solo en ESE proveedor
(con un aviso claro en el log), no en toda la ejecución.
"""
import re
import time
import unicodedata
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from utils import HEADERS, REQUEST_DELAY, best_guess_imdb

# slug de la URL -> nombre bonito que ya usa el resto de la app. Varias
# entradas pueden compartir nombre (p.ej. Filmin normal y Filmin Plus): si
# tienes cualquiera de los dos, cuenta como "Filmin".
NEW_RELEASES_PROVIDERS = [
    ("netflix", "Netflix"),
    ("disney-plus", "Disney Plus"),
    ("amazon-prime-video", "Amazon Prime Video"),
    ("filmin", "Filmin"),
    ("filmin-plus", "Filmin"),
    # Frágil a propósito (ver aviso arriba): incluye el precio actual de la
    # suscripción en la URL.
    ("movistar-plus-eu9-99", "Movistar Plus+"),
]

# Cuánto de "reciente" cuenta como "nuevo en tu plataforma" para el finde:
# los últimos 7 días, incluido el propio día de la consulta (viernes) — así
# lo pediste. Ahora que la fecha es de verdad "cuándo se añadió", 7 días
# tiene sentido (antes, con fecha de estreno en cine, una ventana así de
# corta dejaba casi todo fuera).
RECENCY_WINDOW_DAYS = 7

_MONTH_ABBR_EN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_FULL_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

_TITLE_LINK_RE = re.compile(r"^/es/pelicula/[^/]+/?$")


def _get_page(url: str, label: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        time.sleep(REQUEST_DELAY)
        print(f"    [{label}] GET {url} -> HTTP {r.status_code}, {len(r.text)} bytes")
        if r.status_code != 200:
            return ""
        return r.text
    except Exception as e:
        print(f"    [{label}] ERROR al pedir {url}: {e!r}")
        return ""


def _parse_date_heading(text: str, today: date):
    """
    Intenta leer una cabecera de fecha de la página de JustWatch como fecha
    real. Soporta varios formatos a propósito (no sabemos al 100% cuál usa
    la web exactamente en cada caso) — si no reconoce el texto, devuelve
    None y ese título simplemente no se fecha (no rompe nada).
    """
    t = (text or "").strip().lower()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    if not t:
        return None
    if t == "hoy":
        return today
    if t == "ayer":
        return today - timedelta(days=1)

    # "Aug 25, 2026" / "Aug. 25 2026"
    m = re.match(r"^([a-z]{3})\.?\s+(\d{1,2}),?\s+(\d{4})$", t)
    if m:
        mon = _MONTH_ABBR_EN.get(m.group(1))
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                return None

    # "25 de agosto de 2026" / "25 de agosto"
    m = re.match(r"^(\d{1,2})\s+de\s+([a-z]+)(?:\s+de\s+(\d{4}))?$", t)
    if m:
        mon = _MONTH_FULL_ES.get(m.group(2))
        if mon:
            year = int(m.group(3)) if m.group(3) else today.year
            try:
                d = date(year, mon, int(m.group(1)))
                if d > today:
                    d = date(year - 1, mon, int(m.group(1)))
                return d
            except ValueError:
                return None

    # ISO suelto, por si acaso: "2026-08-25"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    return None


def _scrape_provider_new_movies(slug: str, label: str, today: date):
    """
    Lee https://www.justwatch.com/es/proveedor/{slug}/nuevo/peliculas y
    devuelve [{title, date}, ...] — un pase lineal por todas las etiquetas
    de la página EN EL ORDEN EN QUE APARECEN: cada vez que encontramos un
    texto que parece una cabecera de fecha ("Ayer", "25 de agosto de
    2026"...) lo recordamos como "fecha activa", y cada enlace a una ficha
    de película (/es/pelicula/...) que aparece después se etiqueta con esa
    fecha — así es como la propia página organiza los títulos por día.
    """
    url = f"https://www.justwatch.com/es/proveedor/{slug}/nuevo/peliculas"
    html = _get_page(url, f"JustWatch nuevo · {label} ({slug})")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    items = []
    current_date = None
    for tag in soup.find_all(True):
        if tag.name == "a":
            href = tag.get("href", "")
            if _TITLE_LINK_RE.match(href):
                title = tag.get_text(strip=True)
                if title and current_date:
                    items.append({"title": title, "date": current_date})
            continue
        # Cabecera de fecha candidata: un elemento sin enlaces dentro (para
        # no confundir un bloque contenedor entero con una simple etiqueta
        # de fecha) cuyo texto entero casa con alguno de los formatos.
        if tag.find("a") is not None:
            continue
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 40:
            continue
        d = _parse_date_heading(text, today)
        if d:
            current_date = d
    return items


def get_weekly_streaming_releases():
    """
    Devuelve lista de dicts: {title, platform, imdb_id, tmdb_id, poster,
    release_date, release_year}, ordenada de más reciente a menos reciente,
    ya filtrada a los últimos RECENCY_WINDOW_DAYS días desde que se AÑADIÓ a
    la plataforma (no desde su estreno en cine — ver cabecera del fichero).
    Best-effort por proveedor: si uno falla (p.ej. cambia su URL), se avisa
    en el log y se sigue con el resto, no se rompe toda la ejecución.
    """
    today = date.today()
    cutoff = today - timedelta(days=RECENCY_WINDOW_DAYS)

    all_items = []
    seen = set()
    for slug, label in NEW_RELEASES_PROVIDERS:
        try:
            raw = _scrape_provider_new_movies(slug, label, today)
        except Exception as e:
            print(f"    [{label} ({slug})] ERROR inesperado: {e!r}")
            raw = []
        print(f"    [{label} ({slug})] {len(raw)} títulos con fecha detectada en la página")

        kept = 0
        for it in raw:
            if it["date"] < cutoff:
                continue
            guess = best_guess_imdb(it["title"], year=str(it["date"].year))
            imdb_id = guess["imdb_id"] if guess else None
            if not imdb_id:
                print(f"      sin imdb_id para: {it['title']!r} ({label})")
                continue
            key = (imdb_id, label)
            if key in seen:
                continue
            seen.add(key)
            kept += 1
            all_items.append(
                {
                    "title": guess.get("title") or it["title"],
                    "platform": label,
                    "imdb_id": imdb_id,
                    # Ya no viene gratis de una búsqueda de JustWatch (antes sí,
                    # con search()) — match_engine sigue funcionando igual,
                    # simplemente resuelve el tmdb_id él mismo a partir del
                    # imdb_id, un paso más pero sin coste real (TMDB no bloquea).
                    "tmdb_id": None,
                    "poster": guess.get("poster"),
                    "release_date": it["date"].isoformat(),
                    "release_year": guess.get("year"),
                }
            )
        print(
            f"    [{label} ({slug})] {kept} dentro de los últimos "
            f"{RECENCY_WINDOW_DAYS} días y resueltos a un imdb_id"
        )

    all_items.sort(key=lambda i: i["release_date"], reverse=True)
    print(
        f"    {len(all_items)} títulos añadidos recientemente en tus plataformas "
        f"(vía páginas 'nuevo' de JustWatch, no por fecha de estreno en cine)"
    )
    return all_items


if __name__ == "__main__":
    import json

    print(json.dumps(get_weekly_streaming_releases(), ensure_ascii=False, indent=2))
