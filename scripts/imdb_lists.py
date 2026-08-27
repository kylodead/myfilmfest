"""
Lee tus 3 CSV exportados de IMDb (Ratings, Watchlist y tu lista de Actores
favoritos) desde la carpeta data/, y construye:
  - watchlist: set de imdb_id pendientes
  - taste_profile: géneros/directores/actores que más te gustan, calculado a
    partir de tus votadas con nota alta.
  - favorite_actors: set de nombres de actores favoritos.

Formato esperado de los CSV (el estándar que exporta IMDb):
  ratings.csv    -> columnas: Const, Your Rating, Title, Title Type, Genres, ...
  watchlist.csv  -> columnas: Const, Title, Title Type, Genres, ...
  actors.csv     -> si exportas una lista de IMDb de personas, columnas:
                    Const, Name, ... En caso de no tener columna "Name" pero sí
                    "Title", se usa esa (algunas exportaciones de listas de
                    personas usan "Title" para el nombre).
"""
import csv
from collections import Counter
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TASTE_RATING_THRESHOLD = 7  # a partir de qué nota consideramos "te gustó"


def _read_csv(path: Path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_ratings():
    return _read_csv(DATA_DIR / "ratings.csv")


def load_watchlist():
    return _read_csv(DATA_DIR / "watchlist.csv")


def load_favorite_actors_csv():
    return _read_csv(DATA_DIR / "actors.csv")


def get_watchlist_ids():
    rows = load_watchlist()
    return {r.get("Const", "").strip() for r in rows if r.get("Const")}


def get_watchlist_titles_lookup():
    """dict imdb_id -> título tal como aparece en tu watchlist (por si hace falta)."""
    rows = load_watchlist()
    return {r["Const"].strip(): r.get("Title", "") for r in rows if r.get("Const")}


def get_favorite_actor_names():
    rows = load_favorite_actors_csv()
    names = set()
    for r in rows:
        name = r.get("Name") or r.get("Title") or ""
        name = name.strip()
        if name:
            names.add(name)
    return names


MIN_GENRE_SAMPLES = 15  # no fiarse de un género con 3 pelis vistas
GENRE_PREFERENCE_MARGIN = 0.35  # cuánto por encima de tu media general para contar como "género que te gusta de verdad"
DIRECTOR_LIKE_THRESHOLD = 8  # listón más alto que el general para directores "de fiar"
MIN_DIRECTOR_SAMPLES = 2


def build_taste_profile():
    """
    Construye tu perfil de gustos evitando la trampa de "todo es Drama": en
    vez de contar qué géneros ves más (eso solo mide qué se produce más),
    mide en qué géneros tu nota MEDIA supera claramente tu nota media
    general — eso sí indica una preferencia real. Igual con directores, pero
    exigiendo nota alta (>=8) en más de una película suya.
    """
    rows = load_ratings()
    rated_ids = set()
    liked_ids = set()
    liked_titles = []  # títulos (con nota >= TASTE_RATING_THRESHOLD) -> para detectar sagas que te gustan

    all_ratings = []
    genre_ratings = {}  # genre -> list[float]
    director_high = Counter()  # director -> nº pelis suyas con nota >= DIRECTOR_LIKE_THRESHOLD

    for r in rows:
        const = r.get("Const", "").strip()
        if const:
            rated_ids.add(const)
        try:
            rating = float(r.get("Your Rating", "0") or 0)
        except ValueError:
            rating = 0
        if rating <= 0:
            continue

        all_ratings.append(rating)
        for g in (r.get("Genres") or "").split(","):
            g = g.strip()
            if g:
                genre_ratings.setdefault(g, []).append(rating)

        if rating >= DIRECTOR_LIKE_THRESHOLD:
            for d in (r.get("Directors") or "").split(","):
                d = d.strip()
                if d:
                    director_high[d] += 1

        if rating >= TASTE_RATING_THRESHOLD and const:
            liked_ids.add(const)
            title = (r.get("Title") or "").strip()
            if title:
                liked_titles.append(title)

    overall_avg = sum(all_ratings) / len(all_ratings) if all_ratings else 0

    genre_affinity = {}  # genre -> cuánto por encima de tu media
    for g, values in genre_ratings.items():
        if len(values) < MIN_GENRE_SAMPLES:
            continue
        avg = sum(values) / len(values)
        diff = avg - overall_avg
        if diff >= GENRE_PREFERENCE_MARGIN:
            genre_affinity[g] = round(diff, 2)

    # los géneros que de verdad prefieres, ordenados de más a menos
    top_genres = set(
        sorted(genre_affinity, key=lambda g: genre_affinity[g], reverse=True)[:6]
    )

    top_directors = {
        d for d, n in director_high.items() if n >= MIN_DIRECTOR_SAMPLES
    }

    return {
        "rated_ids": rated_ids,  # todo lo que ya has visto/votado -> excluir de recomendaciones
        "liked_ids": liked_ids,
        "liked_titles": liked_titles,  # títulos que puntuaste bien -> detectar sagas (ver match_engine)
        "overall_avg": round(overall_avg, 2),
        "top_genres": top_genres,
        "genre_affinity": genre_affinity,
        "top_directors": top_directors,
        "director_counts": dict(director_high),
    }
