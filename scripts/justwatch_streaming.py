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

from utils import (
    HEADERS,
    REQUEST_DELAY,
    best_guess_imdb,
    get_title_metadata,
    madrid_today,
    platform_label_matches_provider,
    tmdb_flatrate_providers_es,
)

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
# pedido explícitamente así, como ventana "normal" de primera pasada.
RECENCY_WINDOW_DAYS = 7

# Si en esos 7 días no hay (suficientes) títulos que encajen de verdad con
# tus gustos, build_site.py va ampliando la ventana de 7 en 7 días (14, 21,
# 28...) antes de rendirse y ofrecer "lo último aunque no encaje" — pedido
# explícitamente así: con el catálogo tan grande que tienen tus plataformas,
# casi siempre habrá algo que sí encaje si se mira más atrás en el tiempo.
# Este es el tope de esa ampliación: de aquí para atrás ya no se mira más,
# aunque tampoco haya 3 matches — 9 semanas es más que suficiente margen sin
# acabar recomendando catálogo tan viejo que ya no se sienta "novedad".
MAX_RECENCY_WINDOW_DAYS = 63

# OJO: "sept" (septiembre) tiene 4 letras, no 3 como el resto de meses —
# FilmAffinity la escribe "sept." (no "sep."). Bug real encontrado en
# producción: la regex de abajo exigía EXACTAMENTE 3 letras, así que para
# cualquier título con fecha de septiembre el "t" final de "sept" se quedaba
# pegado al principio del título (p.ej. "2 sept. The Mandalorian and Grogu"
# se leía como fecha 2/sep + título "t. The Mandalorian and Grogu"), lo que
# rompía la búsqueda en IMDb y hacía que la película desapareciera sin más
# (caso real reportado: "The Mandalorian and Grogu", estreno en Disney+ el
# 2 de septiembre, en watchlist, y aun así no aparecía en las propuestas).
# Peor aún: cuando la fecha iba SOLA en el texto (el "sello" superpuesto al
# póster, sin título pegado, ver _match_leading_date), la comprobación de
# "todo el texto era la fecha" tampoco cuadraba por la misma letra suelta, así
# que ni siquiera se actualizaba la fecha activa para los títulos de debajo.
# Se mantiene también "sep" (3 letras) por si alguna otra plataforma lo usa.
_MONTH_ABBR_ES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12,
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
# "{3,4}" y no "{3}" fijo: casi todos los meses abrevian a 3 letras, pero
# "sept" (septiembre) son 4 — con un tamaño fijo se comía solo "sep" y dejaba
# la "t" suelta pegada al texto siguiente (ver el aviso en _MONTH_ABBR_ES).
# Es seguro ampliarlo a 4: el punto o el espacio que sigue a la abreviatura
# corta igualmente la captura si el mes real es de 3 letras, así que nunca
# se come de más del texto del título.
_DATE_ABBR_PREFIX_RE = re.compile(
    r"^(\d{1,2})\s+([a-záéíóú]{3,4})\.?\s*", re.IGNORECASE
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

    IMPORTANTE (corrección de David, 25 sept 2026 — bug real de
    contaminación, no de preventa/taquilla): la versión anterior de esta
    función recorría TODA la página (`soup.find_all(True)`, cualquier
    etiqueta, sin acotar a ningún contenedor) buscando enlaces a fichas de
    película. Eso incluye widgets totalmente ajenos a "novedades de esta
    plataforma" — comprobado en vivo que esta misma página tiene, además de
    la rejilla real de novedades, un carrusel de "Últimas películas
    consultadas" (recomendaciones/historial reciente, nada que ver con
    catálogo de la plataforma) en otro sitio de la página. Caso real: "hoy
    se estrena en Movistar Insidious" resultó ser exactamente esto — el
    enlace a "Insidious: Fuera del más allá" venía de ese carrusel, no de la
    rejilla de novedades (comprobado con el HTML real: NINGÚN enlace a esa
    ficha vive dentro de la rejilla `.movies-row`, los tres que hay están
    fuera). No era un problema de taquilla vs. suscripción — la película ni
    siquiera era una "novedad de Movistar" de verdad, veniamos leyendo un
    widget que no tiene nada que ver.

    Por eso ahora esta función se ACOTA al contenedor real y único de la
    rejilla de novedades, `<div class="... movies-row">` (confirmado en
    vivo, un solo contenedor con ese nombre en toda la página), y dentro de
    él a cada tarjeta `.col[data-movie-id]`, con su fecha en
    `.release-text` (ej. "25<br>sept.") y su título en el enlace
    `.movie-title`. Si esa rejilla no aparece (cambio de plantilla), se
    devuelve una lista vacía y se avisa en el log — NUNCA se cae a
    recorrer toda la página como antes, que es precisamente lo que causó
    esta contaminación.
    """
    url = f"https://www.filmaffinity.com/es/category.php?id={category_id}"
    html = _get_page(url, f"FilmAffinity novedades · {label} ({category_id})")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    grid = soup.find(class_="movies-row")
    if grid is None:
        print(
            f"    [{label} ({category_id})] AVISO: no se encontró la rejilla "
            f"real de novedades (.movies-row) — cambio de plantilla probable. "
            f"Devolviendo 0 títulos en vez de recorrer toda la página (eso "
            f"es lo que causó el bug real de 'Insidious' contaminando "
            f"Movistar Plus+, ver docstring de esta función)."
        )
        return []

    items = []
    for col in grid.find_all(attrs={"data-movie-id": True}):
        title_link = col.find(class_="movie-title") or col.find("a")
        if title_link is None:
            continue
        raw_title = title_link.get_text(" ", strip=True)
        if not raw_title:
            continue

        date_tag = col.find(class_="release-text")
        d = None
        if date_tag is not None:
            d = _parse_date_heading(date_tag.get_text(" ", strip=True), today)

        card_text = col.get_text(" ", strip=True)
        is_series = bool(_SERIES_MARKER_RE.search(card_text))

        title = _SERIES_MARKER_RE.split(raw_title)[0].strip(" -–·(")
        if d and title and not is_series:
            items.append({"title": title, "date": d})
    return items


def get_weekly_streaming_releases(window_days: int = MAX_RECENCY_WINDOW_DAYS):
    """
    Devuelve lista de dicts: {title, platform, imdb_id, tmdb_id, poster,
    release_date, release_year}, ordenada de más reciente a menos reciente,
    ya filtrada a los últimos `window_days` días desde que se AÑADIÓ a la
    plataforma (no desde su estreno en cine — ver cabecera del fichero).

    Por defecto se pide con el tope MAX_RECENCY_WINDOW_DAYS (no solo los 7
    "normales"): así una única pasada de scraping trae de golpe todo lo que
    build_site.py pueda necesitar para ir ampliando la ventana de 7 en 7 días
    si en los primeros 7 no hay suficientes matches con tus gustos — sin
    tener que volver a pedir las páginas de FilmAffinity en cada intento.
    build_site.py luego recorta ese resultado a la ventana que toque en cada
    vuelta con `filter_by_window()`.

    Best-effort por proveedor: si uno falla, se avisa en el log y se sigue
    con el resto, no se rompe toda la ejecución.
    """
    today = madrid_today()  # hora de Madrid, no UTC — ver utils.madrid_today
    cutoff = today - timedelta(days=window_days)

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

            # Pedido explícito de David (25 sept 2026), caso real:
            # "Insidious: Fuera del más allá" salió como novedad de Movistar
            # Plus+ sacado de esta misma página de FilmAffinity, pero solo
            # está ahí en taquilla/alquiler (una compra suelta), no en el
            # catálogo de SUSCRIPCIÓN de Movistar -- "eso no me vale". La
            # página de novedades de FilmAffinity no distingue esto en su
            # HTML (ni en la ficha ni en la propia página del cine se ve
            # ningún aviso de "alquiler"/"taquilla"), así que no hay forma
            # de detectarlo aquí sin consultar otra fuente: se usa el
            # catálogo real de TMDB (/watch/providers, España, apartado
            # "flatrate" = incluido en suscripción, NO "rent"/"buy") para
            # confirmarlo. Si no se puede comprobar (sin TMDB_API_KEY, o la
            # petición falla), NO se descarta por defecto -- mismo criterio
            # que en cines_madrid.py: nunca descartar solo por no haber
            # podido comprobarlo.
            tmdb_id = (get_title_metadata(imdb_id=imdb_id) or {}).get("tmdb_id")
            providers = tmdb_flatrate_providers_es(tmdb_id) if tmdb_id else None
            if not platform_label_matches_provider(label, providers):
                print(
                    f"      descartada {it['title']!r} ({label}) — TMDB dice "
                    f"que en España NO está en el catálogo de suscripción de "
                    f"{label} ahora mismo (solo alquiler/compra suelta, o en "
                    f"otra plataforma): {sorted(providers) if providers else providers}"
                )
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
            f"{window_days} días y resueltos a un imdb_id"
        )

    all_items.sort(key=lambda i: i["release_date"], reverse=True)
    print(
        f"    {len(all_items)} títulos añadidos recientemente en tus plataformas "
        f"(vía novedades por plataforma de FilmAffinity)"
    )
    return all_items


def filter_by_window(items, window_days: int):
    """Recorta `items` (ya traídos con get_weekly_streaming_releases, que
    pide de golpe hasta MAX_RECENCY_WINDOW_DAYS) a los que caen dentro de los
    últimos `window_days` días. Puramente en memoria, sin volver a scrapear
    nada — es lo que usa build_site.py para probar primero 7 días, luego 14,
    21... sin repetir peticiones a FilmAffinity en cada vuelta."""
    cutoff_iso = (madrid_today() - timedelta(days=window_days)).isoformat()
    return [i for i in items if i["release_date"] >= cutoff_iso]


if __name__ == "__main__":
    import json

    print(json.dumps(get_weekly_streaming_releases(), ensure_ascii=False, indent=2))
