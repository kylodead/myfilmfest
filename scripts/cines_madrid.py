"""
Scraper de cartelera de Madrid — va DIRECTO a la web de cada cine siempre que
es técnicamente posible (comprobado a mano, cine por cine, antes de escribir
esto), en vez de depender de un único agregador intermedio.

Estado real por cine (agosto 2026):

  DIRECTO A LA WEB OFICIAL (funciona con HTML estático, sin agregador):
    - Cineteca (Matadero Madrid)      -> cinetecamadrid.com/programacion
    - Filmoteca - Cine Doré           -> entradasfilmoteca.sacatuentrada.es
    - Cines Renoir (Plaza de España)  -> cinesrenoir.com
    - Cines Golem                     -> golem.es
    - Mk2 Cine Paz                    -> cinepazmadrid.es

  SIN SOLUCIÓN DIRECTA TODAVÍA (se apoyan en FilmAffinity como respaldo, con
  el motivo concreto anotado junto a cada uno — no es que no se haya
  intentado, es que la web oficial no lo permite con un scraper simple):
    - Yelmo Cines Ideal                     -> la cartelera se carga por
      JavaScript (React/Next.js), no hay HTML estático que leer sin un
      navegador real (Playwright). Posible pero mucho más caro de mantener.
    - Cines Embajadores                     -> su web no se pudo verificar
      todavía (fallo de conexión al comprobarla), pendiente de probar en
      real durante una ejecución.
    - Sala Equis                            -> no tiene web propia con
      cartelera estructurada (solo redes sociales/blogs de terceros).
    - Círculo de Bellas Artes (Cine Estudio) -> su programación se publica
      como PDF descargable, no como HTML.
    - Cinesa Proyecciones                   -> su web bloquea peticiones
      automatizadas (HTTP 403, protección anti-bot tipo Cloudflare).

Si quieres que invierta en resolver alguno de estos 5 con más profundidad
(sobre todo Yelmo y Cinesa, que son cadenas grandes), dímelo — es un cambio
de arquitectura mayor (headless browser) y prefiero que lo decidas tú.
"""
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from utils import HEADERS, REQUEST_DELAY, best_guess_imdb

TIME_PATTERN = re.compile(r"(\d{1,2})[:h](\d{2})h?")

# Anotaciones de versión/formato que solo ensucian el matching contra IMDb
# (V.O.S.E., 70mm, 3D...); se recortan del título antes de buscarlo.
_TRAILING_ANNOTATION_RE = re.compile(r"\s*[\(\[][^()\[\]]*[\)\]]\s*$")

_SPANISH_MONTHS = (
    "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre"
)
# Solo la Filmoteca mete la fecha en el propio texto del enlace ("la iguana
# 27 agosto") — se recorta igual que las anotaciones entre paréntesis, pero
# SOLO se aplica en scrape_dore() (ver _clean_dore_title), no en el resto de
# cines: un número suelto al final de un título SÍ puede ser parte real del
# nombre (p.ej. "Prácticamente magia 2"), así que no se recorta a lo tonto
# en todos los sitios.
_TRAILING_DATE_RE = re.compile(rf"\s+\d{{1,2}}\s+(?:{_SPANISH_MONTHS})\s*$", re.IGNORECASE)
_TRAILING_LONE_NUMBER_RE = re.compile(r"\s+\d+\s*$")


def _clean_title(title: str) -> str:
    t = (title or "").strip()
    while True:
        new_t = _TRAILING_ANNOTATION_RE.sub("", t).strip()
        if not new_t or new_t == t:
            break
        t = new_t
    return t or (title or "").strip()


def _clean_dore_title(title: str) -> str:
    """Como _clean_title, pero además quita la fecha y el número de sesión
    sueltos que la Filmoteca añade al texto del enlace — seguro aplicarlo
    solo aquí porque sabemos que este sitio en concreto hace eso."""
    t = _clean_title(title)
    t = _TRAILING_DATE_RE.sub("", t).strip()
    t = _TRAILING_LONE_NUMBER_RE.sub("", t).strip()
    return t or _clean_title(title)


def _get(url: str, label: str) -> str:
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


def _find_showtimes_nearby(link_tag, max_levels: int = 6):
    """
    Sube desde el enlace hasta encontrar el contenedor más pequeño que
    contenga texto con pinta de hora (admite "20:00", "20:00h", "20h00").
    Buscamos en el texto completo del contenedor (no nodo a nodo) porque cada
    web separa el título y las horas de forma distinta.
    """
    container = link_tag.parent
    for _ in range(max_levels):
        if container is None:
            break
        text = container.get_text(" ", strip=True)
        times = set()
        for m in TIME_PATTERN.finditer(text):
            hh, mm = int(m.group(1)), m.group(2)
            if 0 <= hh <= 23:
                times.add(f"{hh:02d}:{mm}")
        if times:
            return sorted(times)
        container = container.parent
    return []


# "Hoy", "Mañana" o el nombre del día, con o sin el número de fecha detrás
# ("Jueves 27", "Sábado 29 de agosto"...). Usado SOLO por _find_dated_showtimes_nearby
# (cines que agregan varios días en una sola página, como los que van vía
# FilmAffinity) — Doré ya tiene su propia fecha exacta sacada de la URL de
# cada sesión y no necesita esto.
_DAY_LABEL_RE = re.compile(
    r"\b(hoy|mañana|lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
    r"(?:\s+\d{1,2}(?:\s+de\s+[a-záéíóúñ]+)?)?",
    re.IGNORECASE,
)


def _find_dated_showtimes_nearby(link_tag, max_levels: int = 6):
    """
    Como _find_showtimes_nearby, pero además intenta anteponer a cada hora el
    día al que pertenece ("Jueves 27 · 20:00"), leyendo el texto que hay
    ANTES de esa hora en el mismo contenedor — así es como FilmAffinity y
    páginas similares organizan varios días en una sola lista ("Hoy Jueves
    27 16:00 18:10 · Mañana Viernes 28 16:00 19:00 · Sábado 29 de agosto...").

    Es puramente ADITIVO: si no se detecta ningún día cerca, se devuelve la
    hora sola igual que antes — nunca descarta un horario solo por no poder
    fecharlo, para no arriesgarse a dejar cines a 0 resultados por un texto
    con un formato distinto al esperado.
    """
    container = link_tag.parent
    for _ in range(max_levels):
        if container is None:
            break
        text = container.get_text(" ", strip=True)
        time_matches = list(TIME_PATTERN.finditer(text))
        if not time_matches:
            container = container.parent
            continue
        day_matches = list(_DAY_LABEL_RE.finditer(text))
        results = []
        seen = set()
        for tm in time_matches:
            hh, mm = int(tm.group(1)), tm.group(2)
            if not (0 <= hh <= 23):
                continue
            # el día más reciente que aparece ANTES de esta hora en el texto
            # (mismo orden en que la página los publica)
            day_label = None
            for dm in day_matches:
                if dm.start() <= tm.start():
                    day_label = dm.group(0).strip().capitalize()
                else:
                    break
            label = f"{day_label} · {hh:02d}:{mm}" if day_label else f"{hh:02d}:{mm}"
            if label not in seen:
                seen.add(label)
                results.append(label)
        if results:
            return results
        container = container.parent
    return []


def _generic_direct_scrape(url: str, label: str, href_re: re.Pattern, base_domain: str):
    """
    Sirve para los cines cuya web muestra la cartelera como HTML estático con
    enlaces a fichas de película que casan con `href_re`. Cada cine tiene su
    propio regex porque cada web usa un patrón de URL distinto.
    """
    html = _get(url, label)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    films = []
    seen = set()
    for link in soup.find_all("a", href=href_re):
        title = link.get_text(strip=True)
        if not title:
            continue
        href = link.get("href", "")
        key = href or title
        if key in seen:
            continue
        seen.add(key)
        full_url = href if href.startswith("http") else base_domain + href
        films.append(
            {
                "title": _clean_title(title),
                "showtimes": _find_dated_showtimes_nearby(link),
                "listing_url": full_url,
            }
        )
    return films


def scrape_cineteca():
    # Sin "^" al principio (ver nota en scrape_mk2paz): así funciona tanto
    # si el enlace es relativo como si algún día pasa a ser absoluto.
    return _generic_direct_scrape(
        "https://www.cinetecamadrid.com/programacion",
        "Cineteca (Matadero Madrid)",
        re.compile(r"/programacion/[^/]+$"),
        "https://www.cinetecamadrid.com",
    )


def scrape_renoir():
    return _generic_direct_scrape(
        "https://www.cinesrenoir.com/cine/renoir-plaza-de-espana/cartelera/",
        "Cines Renoir (Plaza de España)",
        re.compile(r"/pelicula/[^/]+/?$"),
        "https://www.cinesrenoir.com",
    )


def scrape_golem():
    return _generic_direct_scrape(
        "https://www.golem.es/golem/golem-madrid",
        "Cines Golem",
        re.compile(r"/golem/pelicula/[^/]+$"),
        "https://www.golem.es",
    )


def scrape_mk2paz():
    # Sin "^" al principio: sus enlaces son absolutos
    # (https://www.cinepazmadrid.es/es/detalles/...), no relativos como los
    # demás cines — con el ancla al inicio nunca casaban y por eso salía 0.
    return _generic_direct_scrape(
        "https://www.cinepazmadrid.es/es/cartelera",
        "Mk2 Cine Paz",
        re.compile(r"/es/detalles/[^/]+/[^/]+/?$"),
        "https://www.cinepazmadrid.es",
    )


def scrape_dore():
    """
    La Filmoteca vende entrada por sesión y fecha concreta (un enlace por
    cada día que echan la película), así que aquí agrupamos por título/slug
    en vez de por enlace, y anotamos el día junto a la hora en el propio
    texto del horario.
    """
    label = "Filmoteca - Cine Doré"
    html = _get("https://entradasfilmoteca.sacatuentrada.es/", label)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    link_re = re.compile(r"/es/entradas/([^/]+)/(\d{4}-\d{2}-\d{2})")
    grouped = {}
    for link in soup.find_all("a", href=True):
        m = link_re.search(link["href"])
        if not m:
            continue
        slug, date_str = m.group(1), m.group(2)
        title = link.get_text(strip=True) or slug.replace("-", " ")
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            day_label = f"{d.day:02d}/{d.month:02d}"
        except Exception:
            day_label = date_str
        times = _find_showtimes_nearby(link) or [""]
        entry = grouped.setdefault(
            slug,
            {
                "title": _clean_dore_title(title),
                "showtimes": set(),
                "listing_url": "https://entradasfilmoteca.sacatuentrada.es" + link["href"]
                if link["href"].startswith("/")
                else link["href"],
            },
        )
        for t in times:
            entry["showtimes"].add(f"{day_label} {t}".strip())
    return [
        {"title": e["title"], "showtimes": sorted(e["showtimes"]), "listing_url": e["listing_url"]}
        for e in grouped.values()
    ]


# Cines donde, de momento, no hay scraper directo fiable (ver motivo en la
# cabecera del fichero) — se apoyan en FilmAffinity, que agrega todos los
# cines de Madrid, como fuente puente en vez de dejar el cine sin datos.
FILMAFFINITY_FALLBACK_IDS = {
    "Yelmo Cines Ideal": "433",
    "Cines Embajadores": "1254",
    "Sala Equis": "1261",
    "Círculo de Bellas Artes (Cine Estudio)": "266",
    "Cinesa Proyecciones": "271",
}


def scrape_via_filmaffinity(cinema_name: str):
    theater_id = FILMAFFINITY_FALLBACK_IDS[cinema_name]
    url = f"https://www.filmaffinity.com/es/theater-showtimes.php?id={theater_id}"
    label = f"{cinema_name} (vía FilmAffinity)"
    html = _get(url, label)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    films = []
    seen_ids = set()
    for link in soup.find_all("a", href=re.compile(r"film\d+\.html$")):
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title:
            continue
        m = re.search(r"film(\d+)\.html", href)
        film_key = m.group(1) if m else title
        if film_key in seen_ids:
            continue
        seen_ids.add(film_key)
        url_full = href if href.startswith("http") else "https://www.filmaffinity.com" + href
        films.append(
            {
                "title": _clean_title(title),
                "showtimes": _find_dated_showtimes_nearby(link),
                "listing_url": url_full,
            }
        )
    return films


# Orden de presentación (igual que siempre te lo he mostrado) + qué scraper
# usa cada uno.
CINEMA_SCRAPERS = {
    "Yelmo Cines Ideal": lambda: scrape_via_filmaffinity("Yelmo Cines Ideal"),
    "Cines Embajadores": lambda: scrape_via_filmaffinity("Cines Embajadores"),
    "Cineteca (Matadero Madrid)": scrape_cineteca,
    "Filmoteca - Cine Doré": scrape_dore,
    "Sala Equis": lambda: scrape_via_filmaffinity("Sala Equis"),
    "Círculo de Bellas Artes (Cine Estudio)": lambda: scrape_via_filmaffinity(
        "Círculo de Bellas Artes (Cine Estudio)"
    ),
    "Cines Renoir (Plaza de España)": scrape_renoir,
    "Cines Golem": scrape_golem,
    "Mk2 Cine Paz": scrape_mk2paz,
    "Cinesa Proyecciones": lambda: scrape_via_filmaffinity("Cinesa Proyecciones"),
}


def get_madrid_billboard():
    """
    Devuelve: { cinema_name: [ {title, showtimes, listing_url, imdb_id,
    imdb_hint_title, imdb_hint_poster}, ... ] }
    Intenta resolver cada película a su ficha de IMDb vía búsqueda por título.
    `imdb_hint_title`/`imdb_hint_poster` vienen del propio resultado de esa
    búsqueda (rápida) y sirven de respaldo si luego la ficha completa de
    IMDb falla al cargar (para no dejar la tarjeta sin título ni póster).
    """
    billboard = {}
    for cinema_name, scraper in CINEMA_SCRAPERS.items():
        try:
            films = scraper()
        except Exception as e:
            print(f"    [{cinema_name}] ERROR inesperado en el scraper: {e!r}")
            films = []
        print(f"    [{cinema_name}] {len(films)} películas encontradas en la página")
        enriched = []
        resolved = 0
        for f in films:
            guess = best_guess_imdb(f["title"])
            f["imdb_id"] = guess["imdb_id"] if guess else None
            f["imdb_hint_title"] = guess.get("title") if guess else None
            f["imdb_hint_poster"] = guess.get("poster") if guess else None
            if f["imdb_id"]:
                resolved += 1
            else:
                print(f"      sin imdb_id para: {f['title']!r}")
            enriched.append(f)
        print(f"    [{cinema_name}] {resolved}/{len(films)} resueltas a un imdb_id")
        # Diagnóstico para saber si _find_dated_showtimes_nearby está
        # detectando el día (p.ej. "Jueves · 20:00") o solo la hora sola —
        # así lo veo en el log sin tener que adivinar el HTML real de cada
        # web a ciegas.
        with_day = sum(
            1 for f in enriched for s in (f.get("showtimes") or []) if "·" in s
        )
        total_showtimes = sum(len(f.get("showtimes") or []) for f in enriched)
        if total_showtimes:
            print(
                f"    [{cinema_name}] horarios con día detectado: {with_day}/{total_showtimes}"
            )
        billboard[cinema_name] = enriched
    return billboard


if __name__ == "__main__":
    import json

    print(json.dumps(get_madrid_billboard(), ensure_ascii=False, indent=2)[:3000])
