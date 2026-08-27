"""
Novedades de streaming de la semana en tus plataformas (Disney+, Filmin,
Movistar Plus+, Netflix, Prime Video) para España.

HISTORIAL DE ESTE FICHERO — importante para entender por qué está así (el
nombre del fichero se queda como estaba, "justwatch_streaming.py", para no
tener que tocar los imports en build_site.py, pero ya NO usa JustWatch):

Versión 1: la librería "simple-justwatch-python-api" (API GraphQL de
justwatch.com), buscando por AÑO DE ESTRENO EN CINE. Mal: la mayoría de lo
que las plataformas añaden cada semana es catálogo antiguo, no estrenos de
cine recientes, así que casi todo quedaba fuera del filtro.

Versión 2: scraping directo de las páginas "nuevo/peliculas" de la propia
web de justwatch.com, que sí muestran fecha real de alta. En la práctica
(dos ejecuciones reales seguidas) esto dio 0 resultados: probablemente esa
página solo expone en el HTML estático (sin ejecutar JavaScript) un primer
lote fijo de títulos, sin garantía de que sea el más reciente en el momento
exacto de la consulta — o hay algo en su plantilla que no coincidía con lo
que esperaba mi analizador. No se pudo confirmar la causa exacta a tiempo,
así que en vez de intentar un tercer parche a ciegas sobre JustWatch, se
cambió de fuente por completo, como tú mismo propusiste.

Versión 3 (ESTA): FilmAffinity tiene, para cada plataforma, una página de
"novedades" (`category.php?id=new_XXX` o `cat_new_XXX.html`, según la
plataforma) que lista títulos con su fecha real de alta, ordenados del más
reciente al más antiguo — comprobado a mano título por título antes de
escribir esto. Es la misma web que YA usábamos como respaldo para 5 cines
(cines_madrid.py), así que no es una fuente nueva y desconocida: ya sabemos
que responde con HTML normal (sin bloquear peticiones automatizadas) y que
usa enlaces con el patrón "/es/filmNNNNN.html" tanto para películas como
para series — la única forma de distinguirlas es el texto junto al título
("(Serie de TV)", "(Miniserie de TV)"...), así que esas se descartan por
texto, no por URL.

Aviso de fragilidad honesto: no he podido inspeccionar el HTML real de estas
páginas byte a byte (mi entorno de desarrollo no tiene salida de red directa
a filmaffinity.com, solo lo he podido comprobar vía una herramienta de
lectura de páginas que me da el contenido ya interpretado, no el HTML en
crudo). Por eso el analizador de abajo está escrito para ser TOLERANTE con
dos estructuras posibles a la vez — fecha pegada al propio enlace del
título ("27 ago. Título") o fecha en una cabecera aparte que agrupa varios
títulos debajo — en vez de apostar por una sola. Si en la primera ejecución
real el log muestra 0 títulos con fecha para TODAS las plataformas a la vez
(a diferencia de "0 dentro de la ventana de 7 días", que sí puede pasar sin
más), es señal de que ninguna de las dos estructuras coincide con la
plantilla real y hay que revisarlo con el log en la mano.
"""
import re
import time
import unicodedata
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from utils import HEADERS, REQUEST_DELAY, best_guess_imdb

# id de categoría de FilmAffinity -> nombre bonito que ya usa el resto de la
# app. Varias entradas pueden compartir nombre (p.ej. Filmin normal no tiene
# variante "plus" separada aquí, a diferencia de JustWatch).
NEW_RELEASES_PROVIDERS = [
    ("new_netflix", "Netflix"),
    ("disneyplus", "Disney Plus"),
    ("new_amazon_es", "Amazon Prime Video"),
    ("new_filmin", "Filmin"),
    ("new_movistar_f", "Movistar Plus+"),
]

# Cuánto de "reciente" cuenta como "nuevo en tu plataforma" para el finde:
# los últimos 7 días, incluido el propio día de la consulta (viernes) —
# pedido explícitamente así.
RECENCY_WINDOW_DAYS = 7

_MONTH_ABBR_ES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}
_MONTH_FULL_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

_FILM_LINK_RE = re.compile(r"/es/film\d+\.html$")

# Marca de que el título es una serie/miniserie, no una película — FilmAffinity
# usa el mismo patrón de URL (filmNNNNN.html) para ambos, así que la única
# forma fiable de descartarlas es por este texto.
_SERIES_MARKER_RE = re.compile(r"\(\s*(mini)?serie\b", re.IGNORECASE)

# "27 ago." o "27 ago" al principio del texto de un enlace/cabecera — el
# "\s*" (no "\s+") al final es a propósito: tiene que casar tanto si detrás
# viene un título ("27 ago. Título") como si el texto es SOLO la fecha
# ("27 ago.", el caso normal de las cabeceras/etiquetas de fecha sueltas).
_DATE_ABBR_PREFIX_RE = re.compile(
    r"^(\d{1,2})\s+([a-záéíóú]{3})\.?\s*", re.IGNORECASE
)
# "27 de agosto de 2026" / "27 de agosto" al principio del texto.
_DATE_FULL_PREFIX_RE = re.compile(
    r"^(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+de\s+(\d{4}))?\s*", re.IGNORECASE
)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _year_for(month: int, day: int, today: date) -> int:
    """FilmAffinity no siempre da el año (p.ej. "27 ago.") — asumimos el año
    actual, y si eso cae en el futuro, el año anterior (mismo criterio que ya
    usábamos para JustWatch)."""
    try:
        d = date(today.year, month, day)
    except ValueError:
        return today.year
    return today.year - 1 if d > today else today.year


def _match_leading_date(text: str, today: date):
    """
    Si `text` EMPIEZA por una fecha reconocible ("27 ago. ..." o "27 de
    agosto de 2026 ..."), devuelve (fecha, resto_del_texto_sin_la_fecha).
    Si no, devuelve (None, text) tal cual — así una misma función sirve
    tanto para leer una cabecera de fecha suelta como para leer la fecha
    pegada directamente al texto de un enlace de título.
    """
    t = text.strip()
    norm = _strip_accents(t.lower())

    m = _DATE_ABBR_PREFIX_RE.match(norm)
    if m:
        mon = _MONTH_ABBR_ES.get(m.group(2))
        if mon:
            day = int(m.group(1))
            try:
                d = date(_year_for(mon, day, today), mon, day)
                return d, t[m.end():].strip()
            except ValueError:
                pass

    m = _DATE_FULL_PREFIX_RE.match(norm)
    if m:
        mon = _MONTH_FULL_ES.get(m.group(2))
        if mon:
            day = int(m.group(1))
            year = int(m.group(3)) if m.group(3) else _year_for(mon, day, today)
            try:
                d = date(year, mon, day)
                return d, t[m.end():].strip()
            except ValueError:
                pass

    return None, t


def _parse_date_heading(text: str, today: date):
    """Como _match_leading_date, pero exige que la fecha sea TODO el texto
    (para detectar cabeceras de fecha sueltas, sin título pegado)."""
    d, rest = _match_leading_date(text, today)
    if d and not rest:
        return d
    return None


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


def _scrape_provider_new_movies(category_id: str, label: str, today: date):
    """
    Lee la página de novedades de FilmAffinity para una plataforma y devuelve
    [{title, date}, ...] SOLO para películas (se descartan series/miniseries
    por el texto, ver _SERIES_MARKER_RE).

    Tolerante con dos estructuras de página distintas a la vez (ver aviso de
    fragilidad en la cabecera del fichero):
      (a) la fecha va pegada al propio texto del enlace del título
          ("27 ago. Título (Serie de TV)")
      (b) la fecha va en una cabecera aparte, sin enlace dentro, y varios
          títulos aparecen debajo de ella hasta la siguiente cabecera.
    """
    url = f"https://www.filmaffinity.com/es/category.php?id={category_id}"
    html = _get_page(url, f"FilmAffinity novedades · {label} ({category_id})")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    items = []
    current_date = None
    for tag in soup.find_all(True):
        if tag.name == "a":
            href = tag.get("href", "")
            if not _FILM_LINK_RE.search(href):
                continue
            raw_title = tag.get_text(" ", strip=True)
            if not raw_title:
                continue
            # Caso (a): fecha pegada al propio enlace.
            d, rest_title = _match_leading_date(raw_title, today)
            if d and not rest_title:
                # Todo el texto de este enlace era SOLO la fecha (p.ej. el
                # enlace que envuelve la miniatura, con la fecha superpuesta
                # encima de la imagen a modo de "sello" — visto en las
                # capturas que me pasaste) — no es un título de verdad, solo
                # actualiza la fecha activa para el siguiente enlace real.
                current_date = d
                continue
            # "(Serie)"/"(Miniserie)" puede venir pegado al propio texto del
            # título, pero en las páginas de rejilla (ver capturas) suele ir
            # en una etiqueta APARTE justo al lado (mismo contenedor que el
            # enlace, no dentro de él) — por eso miramos también el texto del
            # contenedor padre, no solo el del enlace.
            parent_text = tag.parent.get_text(" ", strip=True) if tag.parent else raw_title
            is_series = bool(_SERIES_MARKER_RE.search(raw_title)) or bool(
                _SERIES_MARKER_RE.search(parent_text)
            )
            if d:
                title = rest_title
            else:
                # Caso (b): usamos la última cabecera/sello de fecha visto.
                d = current_date
                title = raw_title
            title = _SERIES_MARKER_RE.split(title)[0].strip(" -–·(")
            if d and title and not is_series:
                items.append({"title": title, "date": d})
            continue
        # Cabecera de fecha candidata: sin enlaces dentro, texto corto que es
        # ÍNTEGRAMENTE una fecha (si tuviera más texto detrás, sería un
        # título del caso (a), no una cabecera del caso (b).
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
    ya filtrada a los últimos RECENCY_WINDOW_DAYS días desde que se AÑADÓ a
    la plataforma (no desde su estreno en cine — ver cabecera del fichero).
    Best-effort por proveedor: si uno falla, se avisa en el log y se sigue
    con el resto, no se rompe toda la ejecución.
    """
    today = date.today()
    cutoff = today - timedelta(days=RECENCY_WINDOW_DAYS)

    all_items = []
    seen = set()
    for category_id, label in NEW_RELEASES_PROVIDERS:
        try:
            raw = _scrape_provider_new_movies(category_id, label, today)
        except Exception as e:
            print(f"    [{label} ({category_id})] ERROR inesperado: {e!r}")
            raw = []
        print(f"    [{label} ({category_id})] {len(raw)} películas con fecha detectada en la página")

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
                    "tmdb_id": None,
                    "poster": guess.get("poster"),
                    "release_date": it["date"].isoformat(),
                    "release_year": guess.get("year"),
                }
            )
        print(
            f"    [{label} ({category_id})] {kept} dentro de los últimos "
            f"{RECENCY_WINDOW_DAYS} días y resueltos a un imdb_id"
        )

    all_items.sort(key=lambda i: i["release_date"], reverse=True)
    print(
        f"    {len(all_items)} títulos añadidos recientemente en tus plataformas "
        f"(vía novedades por plataforma de FilmAffinity)"
    )
    return all_items


if __name__ == "__main__":
    import json

    print(json.dumps(get_weekly_streaming_releases(), ensure_ascii=False, indent=2))
