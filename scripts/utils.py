"""
Utilidades comunes: metadatos de películas (reparto, director, géneros, nota,
póster), caché en disco para no repetir peticiones, y helpers varios.

FUENTE DE METADATOS — IMPORTANTE:
IMDb bloquea sistemáticamente (HTTP 202, no 200) las peticiones a sus fichas
completas (/title/ttXXXXXXX/) cuando vienen desde IPs de datacenter como las
de GitHub Actions. Ni con reintentos ni con esperas crecientes se consigue
pasar ese bloqueo de forma fiable — no es un problema puntual, es un bloqueo
activo para ese patrón de tráfico. El único endpoint de IMDb que SÍ funciona
siempre es el de autocompletado/sugerencias (IMDb Suggestion), que seguimos
usando para resolver el imdb_id a partir de un título.

Por eso, para el reparto/director/géneros/nota (lo que hace falta para saber
si una peli encaja con tus gustos) usamos TMDB (themoviedb.org) como fuente
principal: es una API pensada para consumo automático, no bloquea tráfico de
GitHub Actions, y es gratuita. Solo hace falta una clave de API gratuita
(variable de entorno TMDB_API_KEY). Si esa clave no está configurada, o TMDB
no encuentra la ficha, caemos al scraping de IMDb como último recurso
best-effort (puede fallar, igual que antes).
"""
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}

REQUEST_DELAY = 0.6  # segundos entre peticiones, para ser buenos ciudadanos

TMDB_API_KEY = (os.environ.get("TMDB_API_KEY") or "").strip()
TMDB_BASE = "https://api.themoviedb.org/3"


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", key)[:150]
    return CACHE_DIR / f"{safe}.json"


def cached_get_json(key: str, fetch_fn, max_age_days: int = 25, cache_empty: bool = True):
    """
    Devuelve datos cacheados si existen y son recientes; si no, llama a
    fetch_fn(). La caché SE COMMITEA al repo (ver .gitignore) para no
    repetir peticiones semana tras semana con las mismas películas.

    cache_empty=False es para las llamadas de "ficha completa" (TMDB/IMDb):
    un resultado vacío ahí normalmente significa que la petición falló (red,
    bloqueo, límite de la API), no que la película no tenga datos — así que
    NO lo guardamos, para poder reintentarlo la semana que viene en vez de
    quedarnos 25 días con una ficha en blanco por un fallo puntual.
    """
    path = _cache_path(key)
    if path.exists():
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days < max_age_days:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    data = fetch_fn()
    if data or cache_empty:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return data


def normalize_title(title: str) -> str:
    t = title.lower().strip()
    t = "".join(
        c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)
    )
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def imdb_suggestion_search(title: str):
    """
    Usa el endpoint público de autocompletado de IMDb (el mismo que usa la caja
    de búsqueda de imdb.com). No cuelga de /user/ ni de /title/*/reviews, así
    que no está afectado por el bloqueo de robots.txt de las páginas de listas
    personales. Devuelve una lista de candidatos {id, title, year, poster}.
    """
    q = re.sub(r"[^a-z0-9]", "", normalize_title(title))
    if not q:
        return []
    first_char = q[0]
    url = f"https://v2.sg.media-imdb.com/suggestion/{first_char}/{q}.json"

    def _fetch():
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return []
            data = r.json()
            out = []
            for item in data.get("d", []):
                if not item.get("id", "").startswith("tt"):
                    continue
                out.append(
                    {
                        "imdb_id": item["id"],
                        "title": item.get("l"),
                        "year": item.get("y"),
                        "poster": (item.get("i") or {}).get("imageUrl"),
                        # "qid" distingue película/corto/TV/especial... y "rank"
                        # es la popularidad interna de IMDb (más bajo = más
                        # popular) — las guardamos porque best_guess_imdb las
                        # necesita para no perder pelis cuyo título en IMDb no
                        # se parece en nada al de la cartelera española (ver
                        # comentario ahí, caso real: "El ser querido" / "The
                        # Beloved").
                        "qid": item.get("qid"),
                        "rank": item.get("rank"),
                    }
                )
            return out
        except Exception:
            return []

    return cached_get_json(f"suggest_{q}", _fetch, max_age_days=60)


def imdb_title_info(imdb_id: str):
    """
    Lee la ficha pública /title/ttXXXXXXX/ (permitida por robots.txt) y extrae
    del JSON-LD: título, año, poster, rating, géneros, actores, director.
    """
    url = f"https://www.imdb.com/title/{imdb_id}/"

    def _fetch():
        r = None
        # IMDb devuelve a veces HTTP 202 (no 200) desde IPs de datacenter
        # como las de GitHub Actions — parece un reto/limitación puntual, no
        # un bloqueo total, así que reintentamos un par de veces con espera
        # antes de rendirnos, en vez de descartar la película a la primera.
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
            except Exception as e:
                print(f"      [imdb_title_info] {imdb_id}: ERROR intento {attempt + 1}/3: {e!r}")
                time.sleep(REQUEST_DELAY)
                continue
            if r.status_code == 200:
                break
            print(
                f"      [imdb_title_info] {imdb_id}: intento {attempt + 1}/3 -> "
                f"HTTP {r.status_code} al pedir {url}"
            )
            time.sleep(REQUEST_DELAY * (attempt + 2))  # espera creciente: 1.2s, 1.8s
        else:
            return {}
        time.sleep(REQUEST_DELAY)
        try:
            if r.status_code != 200:
                return {}
            m = re.search(
                r'<script type="application/ld\+json">(.*?)</script>',
                r.text,
                re.DOTALL,
            )
            if not m:
                print(
                    f"      [imdb_title_info] {imdb_id}: HTTP 200 pero no se encontró "
                    f"el bloque JSON-LD ({len(r.text)} bytes recibidos — ¿página de bloqueo/captcha?)"
                )
                return {}
            ld = json.loads(m.group(1))
            actors = ld.get("actor") or []
            if isinstance(actors, dict):
                actors = [actors]
            directors = ld.get("director") or []
            if isinstance(directors, dict):
                directors = [directors]
            genres = ld.get("genre") or []
            if isinstance(genres, str):
                genres = [genres]
            rating = (ld.get("aggregateRating") or {}).get("ratingValue")
            return {
                "imdb_id": imdb_id,
                "title": ld.get("name"),
                "poster": ld.get("image"),
                "rating": rating,
                "genres": genres,
                "actors": [a.get("name") for a in actors if a.get("name")],
                "directors": [d.get("name") for d in directors if d.get("name")],
                "description": ld.get("description"),
                "url": url,
            }
        except Exception as e:
            print(f"      [imdb_title_info] {imdb_id}: ERROR {e!r}")
            return {}

    return cached_get_json(f"title_{imdb_id}", _fetch, max_age_days=25, cache_empty=False)


# Pedimos la ficha de TMDB en español (language=es-ES) para que título y
# sinopsis salgan en tu idioma — pero eso significa que el NOMBRE del género
# también viene en español ("Comedia", "Ciencia ficción"...), y tu perfil de
# gustos está construido con los nombres en inglés tal cual los exporta IMDb
# ("Comedy", "Sci-Fi"...). Si comparásemos el texto tal cual, NUNCA
# coincidirían — un bug real que se coló al añadir el idioma español.
# La solución fiable: los IDs de género de TMDB son fijos y no dependen del
# idioma (28 siempre es "Action", pase lo que pase en `name`), así que
# traducimos por ID a los mismos nombres que usa IMDb, no por el texto.
TMDB_GENRE_ID_TO_NAME = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Sci-Fi",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
}

GENRE_ALIASES = {
    # Respaldo por si algún día TMDB añade un id nuevo que no está en el
    # mapa de arriba: al menos cubrimos el caso en inglés más habitual.
    "science fiction": "Sci-Fi",
}


def _normalize_genre_name(genre_obj):
    """Recibe el dict {id, name} de TMDB y devuelve el nombre en el mismo
    idioma/formato que usa tu CSV de IMDb (ver comentario de TMDB_GENRE_ID_TO_NAME)."""
    if not genre_obj:
        return None
    gid = genre_obj.get("id")
    if gid in TMDB_GENRE_ID_TO_NAME:
        return TMDB_GENRE_ID_TO_NAME[gid]
    name = (genre_obj.get("name") or "").strip()
    if not name:
        return None
    return GENRE_ALIASES.get(name.lower(), name)


def _tmdb_movie_full_fetch(tmdb_id, fallback_imdb_id=None):
    try:
        r = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}",
            params={
                "api_key": TMDB_API_KEY,
                "language": "es-ES",
                "append_to_response": "credits",
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"      [tmdb] {tmdb_id}: HTTP {r.status_code}")
            return {}
        d = r.json()
    except Exception as e:
        print(f"      [tmdb] {tmdb_id}: ERROR {e!r}")
        return {}

    genres = [n for n in (_normalize_genre_name(g) for g in (d.get("genres") or [])) if n]
    credits = d.get("credits") or {}
    cast = credits.get("cast") or []
    # 25 en vez de 10: un actor favorito con un papel secundario (no de
    # cabecera) se quedaba fuera del corte anterior y el match nunca se
    # detectaba — sale gratis en la misma petición, así que no cuesta nada
    # ampliarlo.
    actors = [c.get("name") for c in cast[:25] if c.get("name")]
    crew = credits.get("crew") or []
    directors = [c.get("name") for c in crew if c.get("job") == "Director" and c.get("name")]
    poster_path = d.get("poster_path")
    poster = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
    imdb_id = d.get("imdb_id") or fallback_imdb_id
    if not d.get("title") and not d.get("id"):
        return {}
    # Si la peli pertenece a una saga (p.ej. "Insidious Collection"), TMDB nos
    # lo da GRATIS en esta misma petición — lo guardamos para poder detectar
    # "ya has visto y puntuado bien otra peli de esta saga", sin tener que
    # pedir la lista completa de la colección (eso sí costaría una petición
    # aparte por cada peli con saga).
    collection = d.get("belongs_to_collection") or {}
    return {
        "imdb_id": imdb_id,
        "title": d.get("title") or d.get("original_title"),
        "poster": poster,
        "rating": d.get("vote_average"),
        "genres": genres,
        "actors": actors,
        "directors": directors,
        "collection_name": collection.get("name"),
        "url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else d.get("homepage"),
    }


def tmdb_title_info_by_tmdb_id(tmdb_id: str, imdb_id: str = None):
    """Ficha TMDB directa por su propio id (nos la da gratis JustWatch en
    cada resultado de búsqueda, así nos ahorramos una petición extra)."""
    if not TMDB_API_KEY or not tmdb_id:
        return {}
    return cached_get_json(
        f"tmdb_{tmdb_id}",
        lambda: _tmdb_movie_full_fetch(tmdb_id, imdb_id),
        max_age_days=25,
        cache_empty=False,
    )


def tmdb_title_info_by_imdb(imdb_id: str):
    """
    Resuelve el tmdb_id a partir de un imdb_id (caso de la cartelera de
    cines, donde solo tenemos imdb_id) usando el endpoint /find de TMDB, y
    luego pide la ficha completa.
    """
    if not TMDB_API_KEY or not imdb_id:
        return {}

    def _fetch():
        try:
            r = requests.get(
                f"{TMDB_BASE}/find/{imdb_id}",
                params={
                    "api_key": TMDB_API_KEY,
                    "external_source": "imdb_id",
                    "language": "es-ES",
                },
                timeout=10,
            )
            if r.status_code != 200:
                print(f"      [tmdb find] {imdb_id}: HTTP {r.status_code}")
                return {}
            results = (r.json() or {}).get("movie_results") or []
            if not results:
                return {}
            tmdb_id = results[0].get("id")
            if not tmdb_id:
                return {}
        except Exception as e:
            print(f"      [tmdb find] {imdb_id}: ERROR {e!r}")
            return {}
        return _tmdb_movie_full_fetch(tmdb_id, imdb_id)

    return cached_get_json(f"tmdb_by_imdb_{imdb_id}", _fetch, max_age_days=25, cache_empty=False)


# Memoria SOLO de esta ejecución (no se guarda en disco, se pierde al
# terminar el proceso) — evita el problema real que se vio en el log: una
# peli que sale en 8-9 cines a la vez (p.ej. un estreno grande) provocaba
# 8-9 peticiones IDÉNTICAS a TMDB/IMDb para el mismo imdb_id, una por cada
# cine donde aparecía, porque cada aparición se puntuaba por separado. Como
# cache_empty=False no guarda los fallos en disco (a propósito, para poder
# reintentarlos la semana que viene), sin esto un fallo se repetía una y
# otra vez EN LA MISMA ejecución en vez de solo una — eso multiplicaba por
# 8-9 el tiempo perdido contra el bloqueo de IMDb.
_RUN_METADATA_CACHE = {}


def get_title_metadata(imdb_id: str = None, tmdb_id: str = None):
    """
    Punto único para pedir reparto/director/géneros/nota/póster de una
    película. Primero intenta TMDB (fiable, no bloqueado); si no hay
    TMDB_API_KEY configurada, o TMDB no encuentra la ficha, cae al scraping
    best-effort de IMDb (el mismo de siempre, que puede fallar por bloqueo).
    """
    cache_key = (imdb_id, tmdb_id)
    if cache_key in _RUN_METADATA_CACHE:
        return _RUN_METADATA_CACHE[cache_key]

    info = {}
    if TMDB_API_KEY:
        if tmdb_id:
            info = tmdb_title_info_by_tmdb_id(tmdb_id, imdb_id)
        if not info and imdb_id:
            info = tmdb_title_info_by_imdb(imdb_id)
    if not info and imdb_id:
        info = imdb_title_info(imdb_id)
    info = info or {}
    _RUN_METADATA_CACHE[cache_key] = info
    return info


def _title_similarity(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def _year_key(c):
    try:
        return int(c.get("year") or 0)
    except (TypeError, ValueError):
        return 0


def _rank_key(c):
    # Popularidad interna de IMDb: más bajo = más popular/más probable que
    # sea "el" resultado correcto entre varios homónimos.
    try:
        return int(c.get("rank") or 10**9)
    except (TypeError, ValueError):
        return 10**9


def _normalize_person_name(name: str) -> str:
    return normalize_title(name or "")


def _director_hint_matches(directors, hint: str) -> bool:
    hint_norm = _normalize_person_name(hint)
    if not hint_norm:
        return False
    for d in directors or []:
        d_norm = _normalize_person_name(d)
        if not d_norm:
            continue
        if hint_norm == d_norm or hint_norm in d_norm or d_norm in hint_norm:
            return True
    return False


def _pick_by_director(candidates, director_hint: str, max_checked: int = 5):
    """
    Entre varios candidatos homónimos, pide la ficha completa (TMDB/IMDb) de
    los más populares y se queda con el primero cuyo director de verdad
    coincide con `director_hint` — la pista que sacamos de la propia web del
    cine/FilmAffinity junto al título. Es la señal más fiable de todas para
    homónimos (dos películas con el mismo título casi nunca comparten
    director), por eso se prueba antes que año o similitud de texto.
    Limitado a `max_checked` para no disparar una ficha completa por cada
    candidato si el título es muy genérico.
    """
    ordered = sorted(candidates, key=_rank_key)[:max_checked]
    for c in ordered:
        info = get_title_metadata(imdb_id=c.get("imdb_id"))
        if info and _director_hint_matches(info.get("directors"), director_hint):
            return c
    return None


def best_guess_imdb(title: str, year: str = None, director_hint: str = None, min_similarity: float = 0.82):
    """
    Busca en IMDb Suggestion y devuelve el candidato más plausible.

    Con UN ÚNICO candidato nos fiamos de él aunque el texto no se parezca en
    absoluto al nuestro: el motor de sugerencias de IMDb ya indexa por
    título original/AKA (no solo por el que ves en pantalla), así que si de
    verdad no hubiera relación no habría devuelto nada.

    Si nos pasan `director_hint` (sacado de la propia web del cine/
    FilmAffinity junto al título, cuando está disponible) y hay más de un
    candidato, lo comprobamos ANTES que nada más: es la confirmación más
    fiable posible contra el caso real que reportaste — "La conspiración"
    en el Doré es "The Conspirator" (2010) de Robert Redford, y sin esta
    comprobación el sistema podía quedarse con OTRA película distinta que
    también se llama "La conspiración".

    Si no hay pista de director, o no coincide con ninguno, el caso real que
    esto arregla es "El ser querido" (cartelera en español) / "The Beloved"
    en IMDb: título completamente distinto, así que la similitud de texto
    SIEMPRE lo iba a descartar, aunque IMDb Suggestion sí lo tenía entre los
    candidatos y con el año correcto. Por eso miramos si hay un candidato
    del año exacto esperado (año real de la sesión si lo sacamos de la
    propia web, o el actual/anterior como respaldo) antes de exigir que el
    texto se parezca — el año + ser la única/la más popular coincidencia de
    ese año es una señal más fiable que el texto cuando el título está
    traducido. Si hay varios del mismo año, nos quedamos con el más popular
    en IMDb (rank más bajo), no con el primero que devuelva la API.

    Si nada de eso resuelve la ambigüedad, caemos al criterio más antiguo:
    exigir similitud de texto >= min_similarity y, entre los que la cumplen,
    preferir el más reciente (evita el caso "Malabestia" 2026 resolviendo a
    una peli italiana de los 80 con el mismo título).
    """
    candidates = imdb_suggestion_search(title)
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Descarta personas homónimas (directores, actores...) que a veces se
    # cuelan en las sugerencias junto a los títulos — se reconocen porque no
    # tienen año de estreno.
    films = [c for c in candidates if c.get("year")]
    if not films:
        films = candidates

    if director_hint and len(films) > 1:
        by_director = _pick_by_director(films, director_hint)
        if by_director:
            return by_director

    if year:
        same_year = [c for c in films if str(c.get("year")) == str(year)]
        if len(same_year) == 1:
            return same_year[0]
        if len(same_year) > 1:
            return min(same_year, key=_rank_key)

    filtered = [c for c in films if _title_similarity(title, c.get("title") or "") >= min_similarity]
    if not filtered:
        return None

    if year:
        for c in filtered:
            if str(c.get("year")) == str(year):
                return c

    return max(filtered, key=_year_key)
