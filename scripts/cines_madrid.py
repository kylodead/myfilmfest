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
    - Sala Equis                            -> tiene web propia (salaequis.es)
      pero sin cartelera estructurada en HTML estático que leer — el enlace
      que se muestra al usuario SÍ es esa web (salaequis.es), solo que los
      horarios en sí se siguen sacando de FilmAffinity por dentro.
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
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from utils import HEADERS, REQUEST_DELAY, best_guess_imdb

TIME_PATTERN = re.compile(r"(\d{1,2})[:h](\d{2})h?")

# Enlace a ficha de FilmAffinity (patrón compartido por varios helpers de
# este fichero para identificar el enlace de una película).
_FILM_LINK_RE_GENERIC = re.compile(r"film\d+\.html$")

# Año de estreno plausible cerca del título (p.ej. "2010" junto a "La
# conspiración" en el Doré, o "2026" junto a un estreno reciente en
# FilmAffinity) — usarlo de verdad, en vez de asumir "el año actual" para
# TODOS los cines, es lo que arregla el caso real que reportaste: el Doré es
# una filmoteca, programa constantemente películas antiguas, así que asumir
# "este año" para una peli suya podía llevar a IMDb a devolver un homónimo
# reciente en vez de la película antigua correcta.
_YEAR_NEARBY_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# En las páginas de FilmAffinity, el nombre del director junto a cada título
# va en un enlace con este patrón de URL — lo usamos como "pista de
# director" para desambiguar homónimos (ver best_guess_imdb en utils.py).
_FA_DIRECTOR_HREF_RE = re.compile(r"name\.php\?name-id=")

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

# El propio texto del enlace en la Filmoteca lleva el año real entre
# paréntesis al final ("La boda (1973)") — hay que sacarlo ANTES de limpiar
# el título (_clean_title recorta justo ese paréntesis, ver
# _TRAILING_ANNOTATION_RE, porque en el resto de cines ahí solo va la
# versión/formato, no un año). Caso real que motivó esto: sin el año, "La
# boda" (Andrzej Wajda, 1973, en la Filmoteca) se resolvía a otra "La boda"
# homónima de 2026 que sí estaba en tu lista de pendientes — falso positivo
# real, detectado por David.
_DORE_YEAR_IN_TITLE_RE = re.compile(r"\((\d{4})\)\s*$")


def _find_dore_title_and_director_nearby(link_tag, max_levels: int = 4):
    """
    Muchas tarjetas de la Filmoteca envuelven SOLO una imagen dentro del
    enlace (`<a><img alt="La boda (1973)"></a>`, sin texto propio en el
    enlace) — confirmado viendo el HTML real de la página. Con
    `link.get_text()` vacío, el código caía al slug de la URL
    ("la-boda-9" -> "la boda 9"), y el "9" (sufijo de desambiguación de
    sacatuentrada.es para homónimos, NO parte real del título) se recortaba
    después por parecer un número suelto al final — dejando "la boda", sin
    año ni director, que resolvía a la película homónima equivocada (bug
    real reportado por David).

    El título real (CON el año entre paréntesis) va en un <h2> dentro de la
    misma tarjeta, y el director en un <h3> justo debajo — confirmado con el
    HTML literal de la página. Se devuelve (título, director) juntos, desde
    el mismo contenedor, para no arriesgarse a coger el <h3> de la tarjeta
    de OTRA sesión si se buscaran por separado en distintas pasadas.
    Puramente aditivo: si no hay <h2>, se devuelve (None, None) y el resto
    del sistema sigue con su respaldo habitual (slug/año/similitud de
    texto).
    """
    container = link_tag.parent
    for _ in range(max_levels):
        if container is None:
            break
        h2 = container.find("h2")
        if h2:
            title_text = h2.get_text(strip=True)
            if title_text:
                h3 = container.find("h3")
                director_text = h3.get_text(strip=True) if h3 else None
                return title_text, director_text
        container = container.parent
    return None, None


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

    REVERTIDO (25 sept 2026): tuvo una salvaguarda anti-contaminación entre
    tarjetas (parar de subir si el contenedor agrupa más de un enlace a
    ficha de película) pensada para el bug de "Hoppers"/"Super Mario
    Galaxy" en Yelmo Cines Ideal. Causó una regresión grave confirmada por
    David (resultados de 8-10 a solo 4): en la estructura REAL de la página
    de cartelera de FilmAffinity, aparentemente cualquier contenedor lo
    bastante grande como para contener el horario de una película también
    contiene enlaces a otras películas (es una tabla/lista de cartelera
    completa), así que esta salvaguarda paraba de subir casi siempre antes
    de encontrar el horario real — dejando la inmensa mayoría de películas
    sin horario y por tanto excluidas. Retirada hasta tener HTML literal
    real que confirme la estructura exacta antes de tocar esto de nuevo.
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


# Pedido explícito de David (tras varios intentos fallidos con "La bola
# negra" — el gran estreno de esa semana quedando fuera igualmente): ya NO
# se detecta ni filtra preventa/estreno futuro aquí. Antes había dos capas
# (una insignia de texto "preventa"/"estreno"/"en cartelera", y una búsqueda
# de fechas completas con mes cerca del enlace) y NINGUNA de las dos
# funcionó de forma fiable en la práctica real — la segunda incluso llegó a
# "contaminar" películas correctas con la fecha de una vecina en la misma
# rejilla. Criterio actual, mucho más simple y pedido así explícitamente:
# lo que aparece en la cartelera scrapeada se incluye tal cual, sin intentar
# adivinar si es preventa o no; la decisión de qué mostrarte de verdad ya la
# toma match_engine.py con tus criterios reales (pendientes / talento que te
# gusta), no una insignia de una web de terceros. Ver HANDOFF.md para el
# historial completo de estos intentos.


def _find_year_and_director_nearby(link_tag, max_levels: int = 6):
    """
    Sube desde el enlace del título buscando, en el mismo contenedor: un año
    plausible de estreno (4 dígitos, entre 1900 y el año que viene) y, si la
    página lo da (de momento solo confirmado en FilmAffinity), el nombre del
    director vía su propio enlace (`_FA_DIRECTOR_HREF_RE`).

    Puramente aditivo, igual que _find_dated_showtimes_nearby: si no se
    encuentra nada, se devuelve (None, None) y el resto del sistema sigue
    funcionando con su respaldo habitual (año actual/anterior, sin pista de
    director) — nunca se descarta una película por no poder fecharla o
    identificar a su director.

    Año y director se buscan cada uno POR SEPARADO, parando cada uno en
    cuanto encuentra algo, en vez de exigir que ambos aparezcan al mismo
    nivel del árbol: si se acoplaran, uno de los dos casi siempre tendría
    que subir más niveles que el otro, y en una rejilla con varias películas
    seguidas eso arriesga "robarle" el director a la película de al lado en
    vez de a la que estamos mirando.
    """
    max_year = date.today().year + 1

    year = None
    container = link_tag.parent
    for _ in range(max_levels):
        if container is None:
            break
        text = container.get_text(" ", strip=True)
        for m in _YEAR_NEARBY_RE.finditer(text):
            y = int(m.group(1))
            if 1900 <= y <= max_year:
                year = str(y)
                break
        if year:
            break
        container = container.parent

    director = None
    container = link_tag.parent
    for _ in range(max_levels):
        if container is None:
            break
        dir_link = container.find("a", href=_FA_DIRECTOR_HREF_RE)
        if dir_link:
            director = dir_link.get_text(strip=True) or None
            break
        container = container.parent

    return year, director


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
        # El enlace en sí a veces envuelve solo una imagen (sin texto propio)
        # — ver _find_dore_title_and_director_nearby para el bug real que
        # esto corrige. Se intenta primero el <h2> de la tarjeta (título CON
        # año real) antes de caer al texto del enlace o, en último caso, al
        # slug de la URL.
        h2_title, h2_director = _find_dore_title_and_director_nearby(link)
        title = link.get_text(strip=True) or h2_title or slug.replace("-", " ")
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            day_label = f"{d.day:02d}/{d.month:02d}"
        except Exception:
            day_label = date_str
        times = _find_showtimes_nearby(link) or [""]
        # _find_year_and_director_nearby está pensado para el patrón de
        # FilmAffinity (director como enlace name.php?name-id=) y aquí nunca
        # encuentra nada — el año real y el director de la Filmoteca se sacan
        # del <h2>/<h3> de la propia tarjeta (ver _DORE_YEAR_IN_TITLE_RE y
        # _find_dore_title_and_director_nearby).
        year_hint, director_hint = _find_year_and_director_nearby(link)
        year_in_title = _DORE_YEAR_IN_TITLE_RE.search(title)
        if not year_hint and year_in_title:
            year_hint = year_in_title.group(1)
        if not director_hint:
            director_hint = h2_director
        entry = grouped.setdefault(
            slug,
            {
                "title": _clean_dore_title(title),
                "showtimes": set(),
                "listing_url": "https://entradasfilmoteca.sacatuentrada.es" + link["href"]
                if link["href"].startswith("/")
                else link["href"],
                # El Doré es una filmoteca: programa constantemente películas
                # antiguas, así que NO vale asumir "año actual" como en un
                # cine comercial — de ahí que aquí sea aún más importante
                # capturar el año real (y el director, si se encuentra) para
                # no confundir el título con un homónimo más reciente.
                "year": year_hint,
                "director_hint": director_hint,
            },
        )
        for t in times:
            entry["showtimes"].add(f"{day_label} {t}".strip())
        if year_hint and not entry.get("year"):
            entry["year"] = year_hint
        if director_hint and not entry.get("director_hint"):
            entry["director_hint"] = director_hint
    return [
        {
            "title": e["title"],
            "showtimes": sorted(e["showtimes"]),
            "listing_url": e["listing_url"],
            "year": e.get("year"),
            "director_hint": e.get("director_hint"),
        }
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

# El enlace que ve el usuario en la tarjeta NUNCA debe llevar a la ficha de
# FilmAffinity (eso confundía — parecía un error, "¿por qué me manda a otra
# web?"): FilmAffinity aquí es solo la fuente de datos que usamos por dentro
# para estos 5 cines (no tienen cartelera propia fácil de leer), pero el
# enlace visible tiene que ser la web real del cine, comprobada a mano.
CINEMA_OFFICIAL_URLS = {
    "Yelmo Cines Ideal": "https://yelmocines.es/cartelera/madrid/yelmo-cines-ideal/",
    "Cines Embajadores": "https://cinesembajadores.es/madrid/cartelera-del-dia/",
    "Sala Equis": "https://salaequis.es",
    "Círculo de Bellas Artes (Cine Estudio)": "https://www.circulobellasartes.com/cine-estudio/",
    "Cinesa Proyecciones": "https://www.cinesa.es/cines/proyecciones/",
}


def scrape_via_filmaffinity(cinema_name: str):
    """
    Pedido explícito de David: ya no se intenta adivinar aquí si una
    película está "en preventa" o no (ver el comentario justo encima de
    _FILM_LINK_RE_GENERIC más arriba para el historial de los dos intentos
    que se probaron y no funcionaron de forma fiable). Se listan tal cual
    todas las películas que aparecen en la página de cartelera de
    FilmAffinity para este cine — es match_engine.py quien decide qué
    enseñarte de verdad, con tus criterios (pendientes / talento que te
    gusta), no una insignia de una web de terceros.
    """
    theater_id = FILMAFFINITY_FALLBACK_IDS[cinema_name]
    url = f"https://www.filmaffinity.com/es/theater-showtimes.php?id={theater_id}"
    label = f"{cinema_name} (vía FilmAffinity)"
    html = _get(url, label)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    official_url = CINEMA_OFFICIAL_URLS.get(cinema_name)
    href_re = _FILM_LINK_RE_GENERIC

    films = []
    seen_ids = set()
    for tag in soup.find_all("a"):
        href = tag.get("href", "")
        if not href_re.search(href):
            continue
        title = tag.get_text(strip=True)
        if not title:
            continue
        m = re.search(r"film(\d+)\.html", href)
        film_key = m.group(1) if m else title
        if film_key in seen_ids:
            continue
        seen_ids.add(film_key)
        year_hint, director_hint = _find_year_and_director_nearby(tag)
        showtimes = _find_dated_showtimes_nearby(tag)
        # REVERTIDO (25 sept 2026): el intento de descartar aquí las
        # películas sin ningún horario detectado cerca (pensado para el
        # carrusel de "destacados" de FilmAffinity — ver historial en
        # HANDOFF.md) causó una regresión grave confirmada por David: el
        # número de resultados cayó de 8-10 a solo 4, señal de que
        # _find_dated_showtimes_nearby (con la salvaguarda anti-contaminación
        # añadida en el mismo cambio) no encontraba horario para la MAYORÍA
        # de películas realmente en cartelera, no solo para las del carrusel
        # — es decir, la hipótesis sobre la estructura real de la página
        # (nunca confirmada con HTML literal, solo con WebFetch resumido, ya
        # antes probado poco fiable para este sitio) era incorrecta. Se
        # vuelve a listar tal cual todo lo que aparece con enlace de ficha,
        # sin exigir horario, hasta tener HTML literal real que confirme por
        # qué "Hoppers"/"Super Mario Galaxy" aparecían sin estar en
        # cartelera.
        films.append(
            {
                "title": _clean_title(title),
                "showtimes": showtimes,
                "listing_url": official_url,
                "year": year_hint,
                "director_hint": director_hint,
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


def _resolve_cinema_title(title: str, year_hint: str = None, director_hint: str = None):
    """
    Resuelve el título de una peli en cartelera a su ficha de IMDb.

    Si sacamos un año real de la propia web (`year_hint` — cada vez más
    habitual ahora que _find_year_and_director_nearby lo intenta en Doré y
    FilmAffinity), lo usamos tal cual: es un dato real, no una suposición.
    `director_hint`, si lo tenemos, se comprueba ANTES que nada (ver
    best_guess_imdb en utils.py) — es la señal más fiable para no confundir
    dos películas homónimas (caso real: "La conspiración" en el Doré es "The
    Conspirator" de Robert Redford, no otra peli con el mismo título).

    Si no hay año real, cae al respaldo anterior: una peli en cartelera
    comercial es casi siempre un estreno de este año o del anterior (sigue
    en cartelera por encima de año nuevo) — probamos los dos antes de caer
    al criterio sin año como último recurso. Ojo: esta suposición NO vale
    para el Doré (filmoteca, programa constantemente películas antiguas) —
    por eso ahí es más importante que en ningún otro sitio haber sacado el
    año real en vez de depender de este respaldo.
    """
    if year_hint:
        guess = best_guess_imdb(title, year=year_hint, director_hint=director_hint)
        if guess:
            return guess

    this_year = date.today().year
    for year in (this_year, this_year - 1):
        guess = best_guess_imdb(title, year=str(year), director_hint=director_hint)
        if guess:
            return guess
    return best_guess_imdb(title, director_hint=director_hint)


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
            guess = _resolve_cinema_title(f["title"], f.get("year"), f.get("director_hint"))
            f["imdb_id"] = guess["imdb_id"] if guess else None
            f["imdb_hint_title"] = guess.get("title") if guess else None
            f["imdb_hint_poster"] = guess.get("poster") if guess else None
            # Log de diagnóstico a propósito (falsos positivos reportados en
            # Yelmo Cines Ideal: películas que aparecían en la app pero que
            # David no encontraba ni en la web del cine ni en FilmAffinity) —
            # sin ver el título CRUDO que se scrapeó frente al título al que
            # se resolvió, no se puede saber si el fallo es un scrape
            # confuso (título mal extraído) o un match erróneo (título bien
            # extraído pero resuelto al imdb_id de otra película). Con esto
            # en el log, la próxima vez que pase se ve de un vistazo cuál es.
            if guess and guess.get("title") and f["title"] != guess.get("title"):
                print(
                    f"      '{f['title']}' (scrapeado) -> resuelto a "
                    f"'{guess.get('title')}' ({f['imdb_id']}, año {guess.get('year')})"
                )
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
