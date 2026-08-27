"""
Utilidades comunes: llamadas a IMDb (páginas públicas /title/ e IMDbSuggestion,
que SÍ están permitidas por robots.txt, a diferencia de /user/*), caché en disco
para no repetir peticiones, y helpers varios.
"""
import json
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


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", key)[:150]
    return CACHE_DIR / f"{safe}.json"


def cached_get_json(key: str, fetch_fn, max_age_days: int = 25):
    """Devuelve datos cacheados si existen y son recientes; si no, llama a fetch_fn()."""
    path = _cache_path(key)
    if path.exists():
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days < max_age_days:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    data = fetch_fn()
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
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            time.sleep(REQUEST_DELAY)
            if r.status_code != 200:
                print(f"      [imdb_title_info] {imdb_id}: HTTP {r.status_code} al pedir {url}")
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

    return cached_get_json(f"title_{imdb_id}", _fetch, max_age_days=25)


def _title_similarity(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def best_guess_imdb(title: str, year: str = None, min_similarity: float = 0.82):
    """
    Busca en IMDb Suggestion y devuelve el candidato más plausible, pero solo
    si el título coincide de verdad (similitud >= min_similarity). Esto evita
    falsos positivos con títulos genéricos cortos (p.ej. "Obsession" podría
    devolver una peli de 1954 en vez del estreno actual) — preferimos no
    recomendar nada antes que recomendar la película equivocada.
    """
    candidates = imdb_suggestion_search(title)
    if not candidates:
        return None

    candidates = [c for c in candidates if _title_similarity(title, c.get("title") or "") >= min_similarity]
    if not candidates:
        return None

    if year:
        for c in candidates:
            if str(c.get("year")) == str(year):
                return c
    return candidates[0]
