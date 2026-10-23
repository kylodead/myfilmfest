"""
Scraper de cartelera de Madrid — va DIRECTO a la web de cada cine siempre que
es técnicamente posible (comprobado a mano, cine por cine, antes de escribir
esto), en vez de depender de un único agregador intermedio.

Estado real por cine (agosto 2026):

  DIRECTO A LA WEB OFICIAL (funciona con HTML estático, sin agregador):
    - Cineteca (Matadero Madrid)      -> cinetecamadrid.com/programacion
    - Filmoteca - Cine Doré           -> entradasfilmoteca.sacatuentrada.es
    - Cines Renoir (Plaza de España, Princesa y Retiro; los 3 de Madrid, todos
      los días que ofrece la web) -> cinesrenoir.com
    - Cines Embajadores               -> cinesembajadores.es (7 días; FilmAffinity
      solo de respaldo -- iba por detrás de la web real)
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
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from utils import HEADERS, REQUEST_DELAY, best_guess_imdb, madrid_today

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


_AIA_BUNDLES = {}


def _verify_bundle_for(url: str):
    """Ruta a un PEM (certifi + intermedio) con el que SÍ se verifica el
    certificado de `url`, o None.

    23 oct 2026: cinesembajadores.es sirve su certificado SIN el intermedio
    ("unable to get local issuer certificate") y falla en GitHub Actions
    aunque el navegador lo abre bien (los navegadores descargan el intermedio
    por AIA; Python no). Se hace lo mismo que el navegador: se lee de la hoja
    la URL del emisor (AIA), se descarga el intermedio y se verifica con
    certifi + ese intermedio. NO se desactiva la verificación TLS."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if host in _AIA_BUNDLES:
        return _AIA_BUNDLES[host]
    path = None
    try:
        import ssl
        import tempfile

        import certifi
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

        leaf_pem = ssl.get_server_certificate((host, urlparse(url).port or 443))  # solo para LEER la hoja
        leaf = x509.load_pem_x509_certificate(leaf_pem.encode())
        aia = leaf.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        issuer_urls = [
            d.access_location.value
            for d in aia
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
        ]
        extra = b""
        for u in issuer_urls[:2]:
            r = requests.get(u, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            data = r.content
            try:
                cert = x509.load_der_x509_certificate(data)
            except ValueError:
                cert = x509.load_pem_x509_certificate(data)
            extra += cert.public_bytes(serialization.Encoding.PEM)
        if extra:
            with open(certifi.where(), "rb") as f:
                base = f.read()
            tmp = tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False)
            tmp.write(base + b"\n" + extra)
            tmp.close()
            path = tmp.name
            print(f"    [TLS] {host}: certificado intermedio descargado por AIA ({len(issuer_urls)} URL)")
    except Exception as e:
        print(f"    [TLS] {host}: no se pudo completar la cadena por AIA: {e!r}")
    _AIA_BUNDLES[host] = path
    return path


def _get(url: str, label: str) -> str:
    verify = True
    for attempt in range(1, 4):
        try:
            try:
                r = requests.get(url, headers=HEADERS, timeout=15, verify=verify)
            except requests.exceptions.SSLError:
                bundle = _verify_bundle_for(url) if verify is True else None
                if not bundle:
                    raise
                verify = bundle
                r = requests.get(url, headers=HEADERS, timeout=15, verify=verify)
            time.sleep(REQUEST_DELAY)
            print(f"    [{label}] GET {url} -> HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code == 429 and attempt < 3:
                wait = 20 * attempt
                print(f"    [{label}] 429 (límite de ritmo) — espero {wait}s y reintento ({attempt}/3)")
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return ""
            return r.text
        except Exception as e:
            print(f"    [{label}] ERROR al pedir {url}: {e!r}")
            return ""
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


_ES_WEEKDAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def _day_label(d: date) -> str:
    """'Miércoles 14' -- mismo formato de día que ya usan el resto de cines."""
    return f"{_ES_WEEKDAYS[d.weekday()]} {d.day}"


# Cines Renoir en Madrid: SON TRES (13 oct 2026, David: "Cines Renoir son tres
# cines en Madrid y me interesan tanto Princesa como Plaza de España"). La app
# solo seguía Plaza de España -- por eso "Su propio infierno" (que sí echa
# Renoir Princesa) se escapó. Nombre -> slug de cinesrenoir.com. OJO: el slug
# de Princesa es "cines-princesa", no "renoir-princesa".
RENOIR_CINEMAS = [
    ("Cines Renoir (Plaza de España)", "renoir-plaza-de-espana"),
    ("Cines Renoir (Princesa)", "cines-princesa"),
    ("Cines Renoir (Retiro)", "renoir-retiro"),
]
_RENOIR_BASE = "https://www.cinesrenoir.com"
_RENOIR_SLUG_RE = re.compile(r"^/pelicula/([^/]+)/?$")
_RENOIR_DIRECTOR_RE = re.compile(r"^\s*de\s+(.+?)\s*$", re.IGNORECASE)


def parse_renoir_day(html: str):
    """Una página de cartelera de Renoir (un día) -> {slug: {title, director,
    times}}. Cada película viene repetida 3 veces (maquetas lg/md/sm de la
    misma tarjeta, `.my-account-content`): se fusionan por slug. Una misma
    película con varias versiones/salas suma todas sus horas."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for card in soup.select(".my-account-content"):
        link = card.find("a", href=_RENOIR_SLUG_RE)
        if link is None:
            continue
        title = link.get_text(strip=True)
        if not title:
            continue
        slug = _RENOIR_SLUG_RE.match(link["href"]).group(1)
        entry = out.setdefault(slug, {"title": title, "director": None, "times": set()})
        small_b = card.select_one("small b")
        if small_b and not entry["director"]:
            m = _RENOIR_DIRECTOR_RE.match(small_b.get_text(" ", strip=True))
            if m:
                # varios directores: "A y B" / "A, B" -> el primero basta como pista
                entry["director"] = re.split(r"\s+y\s+|,", m.group(1))[0].strip() or None
        for a in card.select("a.btn"):
            hhmm = a.get_text(strip=True)
            if re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
                entry["times"].add(f"{int(hhmm.split(':')[0]):02d}:{hhmm.split(':')[1]}")
    return out


def _renoir_dates(base_html: str, today: date):
    """Fechas que ofrece la propia web (selector ?fecha=YYYY-MM-DD, 3 días);
    si no se encuentra el selector, solo hoy."""
    soup = BeautifulSoup(base_html, "html.parser")
    dates = []
    for el in soup.find_all(["option", "a"]):
        ref = el.get("value") or el.get("href") or ""
        m = re.search(r"fecha=(\d{4})-(\d{2})-(\d{2})", ref)
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d not in dates:
                dates.append(d)
    return sorted(dates) or [today]


def scrape_renoir_cinema(cinema_name: str, slug: str):
    """Un cine Renoir, TODOS los días que ofrece su web (antes solo se leía
    la página de "hoy": una película que no se echara hoy pero sí mañana no
    existía para la app)."""
    base_url = f"{_RENOIR_BASE}/cine/{slug}/cartelera/"
    base_html = _get(base_url, cinema_name)
    if not base_html:
        return []
    today = madrid_today()
    dates = _renoir_dates(base_html, today)
    merged = {}  # slug -> {title, director, showtimes[list], seen set}
    for d in dates:
        html = base_html if d == today else _get(f"{base_url}?fecha={d.isoformat()}", cinema_name)
        if not html:
            continue
        for fslug, info in parse_renoir_day(html).items():
            m = merged.setdefault(
                fslug,
                {"title": info["title"], "director": None, "showtimes": [], "seen": set()},
            )
            m["director"] = m["director"] or info["director"]
            for t in sorted(info["times"]):
                label = f"{_day_label(d)} · {t}"
                if label not in m["seen"]:
                    m["seen"].add(label)
                    m["showtimes"].append(label)
    return [
        {
            "title": _clean_title(m["title"]),
            "showtimes": m["showtimes"],
            "listing_url": f"{_RENOIR_BASE}/pelicula/{fslug}/",
            "director_hint": m["director"],
        }
        for fslug, m in merged.items()
    ]


def scrape_renoir():
    # Compatibilidad: el cine que ya existía.
    return scrape_renoir_cinema(*RENOIR_CINEMAS[0])


# Embajadores (13 oct 2026): la web oficial SÍ se puede leer (HTML estático,
# 7 días con ?dia=0..6). Antes se usaba FilmAffinity, cuya página de este cine
# NO incluía "Su propio infierno" aunque Embajadores la echa los días 14 y 15
# (confirmado por David y en su web) -- FilmAffinity va por detrás de la web
# real, así que para este cine se lee la propia web y FilmAffinity queda de
# respaldo si falla.
_EMBAJADORES_URL = "https://cinesembajadores.es/madrid/cartelera-del-dia/?ciudad=madrid"


def parse_embajadores_day(html: str, today: date):
    """Un día de cartelera de Embajadores -> [{title, director, times:[(date,
    'HH:MM')]}]. Cada sesión lleva su fecha real en data-dia="DD/MM"."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for info in soup.select("div.info"):
        a = info.select_one("h2 a")
        if a is None or not a.get_text(strip=True):
            continue
        director = None
        for h5 in info.select("h5"):
            if "dirección" in h5.get_text(" ", strip=True).lower():
                txt = h5.get_text(" ", strip=True).split(":", 1)[-1].strip()
                director = re.split(r"\s+y\s+|,", txt)[0].strip() or None
                break
        sessions = []
        for span in info.select("span[data-dia][data-hora]"):
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", span["data-dia"].strip())
            hm = re.fullmatch(r"(\d{1,2}):(\d{2})", span["data-hora"].strip())
            if not (m and hm):
                continue
            day, month = int(m.group(1)), int(m.group(2))
            year = today.year + (1 if month < today.month - 6 else 0)
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            sessions.append((d, f"{int(hm.group(1)):02d}:{hm.group(2)}"))
        out.append({"title": a.get_text(strip=True), "director": director, "sessions": sessions})
    return out


def scrape_embajadores():
    today = madrid_today()
    merged = {}  # título limpio (minúsculas) -> entrada
    for dia in range(7):
        url = _EMBAJADORES_URL + (f"&dia={dia}" if dia else "")
        html = _get(url, "Cines Embajadores")
        if not html:
            continue
        for f in parse_embajadores_day(html, today):
            clean = _clean_title(f["title"])
            m = merged.setdefault(
                clean.lower(),
                {"title": clean, "director": None, "showtimes": [], "seen": set()},
            )
            m["director"] = m["director"] or f["director"]
            for d, hhmm in f["sessions"]:
                label = f"{_day_label(d)} · {hhmm}"
                if label not in m["seen"]:
                    m["seen"].add(label)
                    m["showtimes"].append(label)
    if not merged:
        print("    [Cines Embajadores] su web no devolvió nada -- se usa FilmAffinity de respaldo")
        return scrape_via_filmaffinity("Cines Embajadores")
    return [
        {
            "title": m["title"],
            "showtimes": m["showtimes"],
            "listing_url": CINEMA_OFFICIAL_URLS.get("Cines Embajadores"),
            "director_hint": m["director"],
        }
        for m in merged.values()
        if m["showtimes"]
    ]


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


def _this_and_next_week_cutoff(today: date) -> date:
    """
    Domingo de "la semana que viene" (semana = lunes a domingo), tomando
    `today` como referencia. Ej.: si hoy es viernes 25 de septiembre de 2026
    (semana del 21-27), "la semana que viene" es del 28 de sept. al 4 de
    octubre -> devuelve 2026-10-04. Ver `scrape_via_filmaffinity` para por
    qué esta ventana concreta (pedida explícitamente así por David).
    """
    end_of_this_week = today + timedelta(days=(6 - today.weekday()))
    return end_of_this_week + timedelta(days=7)


def scrape_via_filmaffinity(cinema_name: str):
    """
    Cada tarjeta de película en la página de cartelera de FilmAffinity vive
    en un contenedor único e identificable: un <div id="m-<id_de_ficha>">
    (confirmado viendo el HTML real y en vivo de la página, no un resumen de
    WebFetch — ver HANDOFF.md).

    HISTORIAL (25 sept 2026, para no repetir los mismos tres errores):
    1) Un intento descartaba aquí cualquier película SIN ningún horario
       detectado cerca, pensando que así se filtraba un supuesto "carrusel
       de destacados" — esa hipótesis nunca se confirmó con HTML real y
       causó una regresión grave (de 8-10 resultados a solo 4). Revertido.
    2) Un segundo intento descartaba cualquier tarjeta marcada por
       FilmAffinity con el texto "Entradas en preventa" — mecánicamente
       funcionaba, pero ese mismo aviso se usa TANTO para una preventa de
       verdad (película aún no estrenada) COMO para una simple "entrada
       anticipada" de una película YA estrenada con sesión dentro de unos
       días (David: *"puedes comprar el dia 1 de octubre entradas para
       final de mes.... eso no es preventa.... porque son peliculas ya
       estrenadas"*). Como el texto es idéntico en los dos casos, no
       distingue nada por sí solo. Revertido.
    3) Comprobado también que basarse en si la película YA se ha estrenado
       (fecha oficial de TMDB) tampoco sirve: "La odisea" (estrenada en
       julio 2026) y "El drama" (estrenado y ya en HBO Max desde agosto
       2026) están LAS DOS ya estrenadas, con sesión suelta futura en Sala
       Equis, y aun así David quiere una sí y la otra... en realidad
       también sí, una vez comprobado que la fecha era real (ver más
       abajo) — el estado de estreno nunca fue el criterio real.

    El criterio real, dado explícitamente por David tras ver los datos
    reales de "Hoppers"/"Super Mario Galaxy" (solo pase suelto dentro de
    2-3 semanas) frente a "El drama" (pase suelto dentro de unos días,
    aceptado): *"si el pase no es la semana que viene no tiene que
    aparecer"*. Es decir, el criterio nunca fue semántico (preventa vs.
    entrada anticipada vs. estreno) — es solo de PROXIMIDAD: si NINGUNA
    sesión de esa tarjeta cae dentro de esta semana o la que viene, no se
    muestra, tenga o no aviso de preventa.

    Para aplicarlo de forma fiable se usa el atributo `data-sess-date`
    ("YYYY-MM-DD") que FilmAffinity pone en cada fila de sesión dentro de
    la tarjeta — confirmado en vivo con el HTML real (no el texto
    traducido/parseado, que es lo que ya falló antes) que es exacto y sin
    ambigüedad de mes/año, a diferencia de intentar parsear "2 de octubre"
    con una regex.
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
    cutoff = _this_and_next_week_cutoff(madrid_today())

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

        card = tag.find_parent(id=f"m-{film_key}") if m else None
        if card is not None:
            sess_dates = []
            for el in card.find_all(attrs={"data-sess-date": True}):
                try:
                    sess_dates.append(
                        datetime.strptime(el["data-sess-date"], "%Y-%m-%d").date()
                    )
                except ValueError:
                    pass
            if sess_dates and not any(d <= cutoff for d in sess_dates):
                print(
                    f"    [{cinema_name}] descartada '{_clean_title(title)}' — "
                    f"la sesión más próxima que da FilmAffinity es el "
                    f"{min(sess_dates).isoformat()}, más allá de esta semana "
                    f"y la que viene (pedido explícitamente así por David: "
                    f"si el pase no es para esta semana o la siguiente, no "
                    f"se muestra)"
                )
                continue
            # Si no se detectó NINGÚN data-sess-date en la tarjeta (cambio
            # de plantilla, fallo de parseo...), no se descarta por defecto
            # — ver el punto 1 del historial de arriba: descartar por falta
            # de detección, en vez de por una fecha real comprobada, es
            # exactamente el error que ya causó una regresión grave.

        year_hint, director_hint = _find_year_and_director_nearby(tag)
        showtimes = _find_dated_showtimes_nearby(tag)
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
    "Cines Embajadores": scrape_embajadores,
    "Cineteca (Matadero Madrid)": scrape_cineteca,
    "Filmoteca - Cine Doré": scrape_dore,
    "Sala Equis": lambda: scrape_via_filmaffinity("Sala Equis"),
    "Círculo de Bellas Artes (Cine Estudio)": lambda: scrape_via_filmaffinity(
        "Círculo de Bellas Artes (Cine Estudio)"
    ),
    "Cines Renoir (Plaza de España)": lambda: scrape_renoir_cinema(*RENOIR_CINEMAS[0]),
    "Cines Renoir (Princesa)": lambda: scrape_renoir_cinema(*RENOIR_CINEMAS[1]),
    "Cines Renoir (Retiro)": lambda: scrape_renoir_cinema(*RENOIR_CINEMAS[2]),
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


# ---------------------------------------------------------------------------
# RADAR DE ESTRENOS (13 oct 2026)
#
# Por qué existe: "Su propio infierno" (Her Private Hell, Nicolas Winding
# Refn, con Sophie Thatcher; en tu watchlist, director y actriz favoritos) se
# estrenó el 9 oct y NO salió. No fue un fallo de título ni de gustos: nunca
# llegó a escanearse, porque solo se proyecta en cines que la app no sigue
# (Cinesa Manoteras, mk2 Palacio de Hielo, Renoir Princesa, Yelmo Plenilunio,
# Yelmo Rivas H2O, Zoco Majadahonda, Cinesa Las Rozas) -- comprobado en vivo
# en FilmAffinity. La lista fija de 10 cines es ciega a cualquier película que
# no pase por ella, por buena que sea para ti.
#
# Segunda vía, independiente de esa lista: se leen los estrenos en cines de
# España (rdcat.php?id=new_th_es, 20 títulos por orden de estreno), se
# resuelve cada uno a su ficha de IMDb y match_engine.select_radar_picks se
# queda SOLO con los de coincidencia fuerte (pendientes / actor / director
# favorito); para esos se piden los cines reales de Madrid
# (movie-showtimes.php?id=<ficha>&state=ES-M).
# ---------------------------------------------------------------------------
RADAR_URL = "https://www.filmaffinity.com/es/rdcat.php?id=new_th_es"
RADAR_SHOWTIMES_URL = "https://www.filmaffinity.com/es/movie-showtimes.php?id={fa_id}&state=ES-M"

# El enlace visible NUNCA debe ir a FilmAffinity (ver CINEMA_OFFICIAL_URLS):
# se asigna la web de la cadena según el nombre del cine; si no hay una
# fiable, None (el frontend ya pone "horario en la web del cine").
_RADAR_CHAIN_URLS = (
    ("renoir", "https://www.cinesrenoir.com"),
    ("cinesa", "https://www.cinesa.es"),
    ("mk2", "https://www.mk2cines.es"),
    ("yelmo", "https://yelmocines.es"),
)

_WEEKDAY_NUM_RE = re.compile(
    r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s*(\d{1,2})?",
    re.IGNORECASE,
)


def canonical_cinema_name(fa_name: str) -> str:
    """Nombre FilmAffinity de un cine -> el nombre que usa la lista fija de la
    app, para que el radar y la lista fija no cuenten dos veces el mismo cine
    ("Renoir Cines Princesa" en FA == "Cines Renoir (Princesa)" aquí)."""
    low = fa_name.lower()
    if "renoir" in low:
        for key, name in (("princesa", "Cines Renoir (Princesa)"),
                          ("plaza", "Cines Renoir (Plaza de España)"),
                          ("retiro", "Cines Renoir (Retiro)")):
            if key in low:
                return name
    if "embajadores" in low:
        return "Cines Embajadores"
    if "yelmo" in low and "ideal" in low:
        return "Yelmo Cines Ideal"
    if "sala equis" in low:
        return "Sala Equis"
    if "proyecciones" in low:
        return "Cinesa Proyecciones"
    if "golem" in low:
        return "Cines Golem"
    if "mk2" in low and "paz" in low:
        return "Mk2 Cine Paz"
    return fa_name


def _radar_chain_url(cinema_name: str):
    low = cinema_name.lower()
    for key, url in _RADAR_CHAIN_URLS:
        if key in low:
            return url
    return None


def parse_radar_candidates(html: str):
    """Tarjetas de la lista de estrenos -> [{fa_id, title, year, director_hint}].
    Cada tarjeta repite su data-movie-id en varios contenedores anidados;
    se queda con la primera por id. El primer enlace de persona de la tarjeta
    es el director (el reparto va después)."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for card in soup.select("div.movie-card[data-movie-id]"):
        fa_id = card.get("data-movie-id")
        if not fa_id or fa_id in seen:
            continue
        seen.add(fa_id)
        title_a = card.select_one(".mc-title a")
        title = title_a.get_text(strip=True) if title_a else ""
        if not title:
            continue
        year = None
        year_el = card.select_one(".mc-year")
        for m in _YEAR_NEARBY_RE.finditer(year_el.get_text(" ", strip=True) if year_el else ""):
            y = int(m.group(1))
            if 1900 <= y <= date.today().year + 1:
                year = str(y)
                break
        # Solo el bloque de dirección (el reparto va aparte en .mc-cast).
        dir_a = card.select_one(".mc-director a[href*='name-id']")
        out.append(
            {
                "fa_id": fa_id,
                "title": _clean_title(title),
                "year": year,
                "director_hint": dir_a.get_text(strip=True) if dir_a else None,
            }
        )
    return out


def _fa_original_title(fa_id: str):
    """Título original de la ficha de FilmAffinity (el título en español a
    veces no existe como AKA en IMDb: 'Su propio infierno' / 'Her Private
    Hell')."""
    html = _get(f"https://www.filmaffinity.com/es/film{fa_id}.html", f"FA ficha {fa_id}")
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for dt in soup.find_all("dt"):
        if "original" in dt.get_text(strip=True).lower():
            dd = dt.find_next_sibling("dd")
            if dd:
                t = dd.get_text(" ", strip=True)
                return t or None
    return None


def get_radar_candidates():
    """Estrenos en cines de España resueltos a IMDb (sin filtrar por gustos)."""
    html = _get(RADAR_URL, "Radar estrenos")
    if not html:
        return []
    cands = parse_radar_candidates(html)
    print(f"    [Radar estrenos] {len(cands)} estrenos en la lista de FilmAffinity")
    for c in cands:
        guess = _resolve_cinema_title(c["title"], c.get("year"), c.get("director_hint"))
        if not guess:
            original = _fa_original_title(c["fa_id"])
            if original and original.lower() != c["title"].lower():
                guess = _resolve_cinema_title(
                    _clean_title(original), c.get("year"), c.get("director_hint")
                )
                if guess:
                    print(
                        f"      [Radar] {c['title']!r} resuelto por su título "
                        f"original {original!r} -> {guess['imdb_id']}"
                    )
        c["imdb_id"] = guess["imdb_id"] if guess else None
        c["imdb_hint_title"] = guess.get("title") if guess else None
        c["imdb_hint_poster"] = guess.get("poster") if guess else None
        if not c["imdb_id"]:
            print(f"      [Radar] sin imdb_id para: {c['title']!r}")
    return cands


def parse_madrid_showtimes(html: str):
    """Página movie-showtimes ...&state=ES-M -> [{name, showtimes,
    listing_url}] (un elemento por cine; varias versiones, p.ej. VOSE y
    doblada, se fusionan). Los días se etiquetan con el texto VISIBLE
    ('Martes 13'), no con data-sess-date, que va un día por detrás del texto."""
    soup = BeautifulSoup(html, "html.parser")
    cinemas = []
    for block in soup.select("div.theater"):
        name_el = block.select_one(".card-header .th-name")
        if not name_el:
            continue
        name = canonical_cinema_name(name_el.get_text(strip=True))
        times, seen = [], set()
        for row in block.select("[data-sess-date]"):
            sess_date = row.select_one(".sess-date")
            dm = _WEEKDAY_NUM_RE.search(sess_date.get_text(" ", strip=True)) if sess_date else None
            if not dm:
                continue
            day = dm.group(1).capitalize() + (f" {dm.group(2)}" if dm.group(2) else "")
            for a in row.select(".times-wrap a"):
                hhmm = a.get_text(strip=True)
                if not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
                    continue
                label = f"{day} · {int(hhmm.split(':')[0]):02d}:{hhmm.split(':')[1]}"
                if label not in seen:
                    seen.add(label)
                    times.append(label)
        if times:
            cinemas.append(
                {"name": name, "showtimes": times, "listing_url": _radar_chain_url(name)}
            )
    return cinemas


def get_madrid_showtimes_for_film(fa_id: str):
    html = _get(RADAR_SHOWTIMES_URL.format(fa_id=fa_id), f"Radar sesiones Madrid {fa_id}")
    return parse_madrid_showtimes(html) if html else []


if __name__ == "__main__":
    import json

    print(json.dumps(get_madrid_billboard(), ensure_ascii=False, indent=2)[:3000])
