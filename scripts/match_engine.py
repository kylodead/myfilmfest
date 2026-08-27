"""
Cruza cartelera de Madrid + novedades de streaming con tus listas (pendientes,
actores favoritos) y tu perfil de gustos real (géneros donde tu nota media
supera claramente tu media general, directores que puntúas alto más de una
vez, sagas de las que ya has visto y puntuado bien alguna entrega) — no con
"géneros que ves mucho", que no significa que te gusten más que la media.

Jerarquía de motivos (de más a menos fiable):
  1. Está en tu lista de pendientes           -> score 100
  2. Sale un actor/actriz de tu lista de favoritos -> score 85
  3. La dirige alguien a quien sueles puntuar alto (>=8, más de una vez) -> score 65
  4. Es de una saga de la que ya viste y puntuaste bien otra entrega     -> score 55
  5. Coincide con AL MENOS 2 de tus géneros de verdadera preferencia,
     Y ninguno de esos géneros es genérico-ómnibus por sí solo           -> score 45
Un match "solo por género" nunca se ofrece en solitario si solo coincide 1
género — eso es ruido, no una recomendación.
"""
import re
import unicodedata

from utils import get_title_metadata

MIN_GENRE_MATCHES = 2

# Palabras que TMDB añade al nombre de una colección y que no forman parte
# del nombre real de la saga (viene en español por el language=es-ES de la
# petición, pero cubrimos también el inglés por si acaso) — se quitan antes
# de comparar, para quedarnos solo con "insidious", no "insidious colección".
_COLLECTION_SUFFIX_RE = re.compile(
    r"\b(colecci[oó]n|collection|saga)\b", re.IGNORECASE
)


def _normalize_for_saga(text: str) -> str:
    t = (text or "").lower().strip()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = _COLLECTION_SUFFIX_RE.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _liked_saga_match(collection_name, liked_titles_normalized):
    """
    ¿Alguno de tus títulos puntuados >=7 empieza por el nombre base de esta
    saga? (p.ej. colección "Insidious Collection" -> base "insidious" ->
    ¿tienes puntuada alguna "Insidious..."?). Solo miramos el PREFIJO del
    título, no una coincidencia en cualquier parte, para no confundir sagas
    con palabras sueltas comunes.
    """
    base = _normalize_for_saga(collection_name)
    if not base or len(base) < 3:
        return False
    return any(t.startswith(base) for t in liked_titles_normalized)


def _get_liked_titles_normalized(taste_profile):
    """Normaliza tus títulos puntuados >=7 UNA sola vez por ejecución (no una
    vez por candidato) y lo guarda en el propio taste_profile."""
    cached = taste_profile.get("_liked_titles_normalized")
    if cached is not None:
        return cached
    normalized = {_normalize_for_saga(t) for t in taste_profile.get("liked_titles", [])}
    normalized.discard("")
    taste_profile["_liked_titles_normalized"] = normalized
    return normalized


def _score_and_reason(imdb_id, taste_profile, favorite_actors, watchlist_ids, tmdb_id=None):
    """Devuelve (incluir: bool, motivo: str, score: int, info: dict) para un imdb_id.
    Se devuelve también `info` (la ficha ya pedida) para no tener que volver a
    pedirla justo después solo para sacar título/póster/nota."""
    if not imdb_id:
        return False, None, 0, {}

    if imdb_id in taste_profile.get("rated_ids", set()):
        return False, None, 0, {}  # ya la has visto/votado

    if imdb_id in watchlist_ids:
        return True, "Está en tu lista de pendientes", 100, {}

    info = get_title_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id)
    actors = set(info.get("actors") or [])
    directors = set(info.get("directors") or [])
    genres = set(info.get("genres") or [])

    matched_actors = actors & favorite_actors
    if matched_actors:
        who = " y ".join(sorted(matched_actors)[:2])
        return True, f"Sale {who}, de tus actores favoritos", 85, info

    matched_directors = directors & taste_profile.get("top_directors", set())
    if matched_directors:
        who = ", ".join(sorted(matched_directors)[:1])
        n = taste_profile.get("director_counts", {}).get(list(matched_directors)[0], 0)
        return True, f"Dirigida por {who}, a quien sueles puntuar alto ({n} pelis con nota ≥8)", 65, info

    collection_name = info.get("collection_name")
    if collection_name and _liked_saga_match(collection_name, _get_liked_titles_normalized(taste_profile)):
        return True, f"Es de la saga de {collection_name}, de la que ya viste y puntuaste bien otra entrega", 55, info

    genre_affinity = taste_profile.get("genre_affinity", {})
    matched_genres = genres & taste_profile.get("top_genres", set())
    if len(matched_genres) >= MIN_GENRE_MATCHES:
        # ordena por cuánto te gusta ese género realmente (no por frecuencia)
        ordered = sorted(matched_genres, key=lambda g: genre_affinity.get(g, 0), reverse=True)
        return (
            True,
            f"Combina {' y '.join(ordered[:3])}, géneros en los que sueles puntuar por encima de tu media",
            45,
            info,
        )

    return False, None, 0, info


def select_cinema_picks(billboard, taste_profile, favorite_actors, watchlist_ids):
    """
    billboard: { cinema_name: [ {title, showtimes, listing_url, imdb_id}, ... ] }
    Devuelve lista de recomendaciones para lunes-jueves, agrupadas por
    película (una peli puede estar en varios cines -> se agrupan showtimes).
    """
    by_imdb = {}
    for cinema_name, films in billboard.items():
        for f in films:
            imdb_id = f.get("imdb_id")
            if not imdb_id:
                continue
            include, reason, score, info = _score_and_reason(
                imdb_id, taste_profile, favorite_actors, watchlist_ids
            )
            if not include:
                continue
            entry = by_imdb.setdefault(
                imdb_id,
                {
                    "imdb_id": imdb_id,
                    "reason": reason,
                    "score": score,
                    "info": info,
                    "cinemas": [],
                    # Respaldo de título/póster por si la ficha completa
                    # (TMDB o, en su defecto, IMDb) falla al cargar: usamos
                    # lo que ya nos dio la búsqueda rápida de IMDb Suggestion
                    # al resolver el imdb_id, en vez de dejar la tarjeta en
                    # blanco.
                    "fallback_title": f.get("imdb_hint_title") or f.get("title"),
                    "fallback_poster": f.get("imdb_hint_poster"),
                },
            )
            entry["cinemas"].append(
                {
                    "name": cinema_name,
                    "showtimes": f.get("showtimes", []),
                    "listing_url": f.get("listing_url"),
                }
            )

    results = []
    for imdb_id, entry in by_imdb.items():
        # "Está en tu lista de pendientes" no pide ficha completa en
        # _score_and_reason (no hace falta para decidir), así que aquí la
        # pedimos solo si todavía no la tenemos — para watchlist principalmente.
        info = entry["info"] or get_title_metadata(imdb_id=imdb_id)
        entry["cinemas"].sort(key=lambda c: c["name"])
        results.append(
            {
                "imdb_id": imdb_id,
                "title": info.get("title") or entry["fallback_title"],
                "poster": info.get("poster") or entry["fallback_poster"],
                "rating": info.get("rating"),
                "imdb_url": info.get("url", f"https://www.imdb.com/title/{imdb_id}/"),
                "reason": entry["reason"],
                "score": entry["score"],
                "cinemas": entry["cinemas"],
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


WEEKEND_DAYS = ["viernes", "sábado", "domingo"]


def select_streaming_picks(streaming_items, taste_profile, favorite_actors, watchlist_ids):
    """
    streaming_items: lista de {title, release_year, release_date, platform,
    imdb_id, poster} — ya vienen ordenados por recencia real desde
    justwatch_streaming.py (lo más reciente primero).

    Devuelve SIEMPRE hasta 3 recomendaciones, una por día del finde (viernes,
    sábado, domingo) — nunca "lo que haya salido", como pediste. Primero se
    llenan con matches de verdad (pendientes / actor / director / género,
    misma jerarquía que en cines); si no hay suficientes, se completa con los
    estrenos más recientes disponibles aunque no encajen con tus gustos —
    pero eso se dice explícitamente en el motivo, no se disfraza de acierto.
    """
    seen_ids = set()
    strict = []
    for item in streaming_items:
        imdb_id = item.get("imdb_id")
        if not imdb_id or imdb_id in seen_ids:
            continue
        include, reason, score, info = _score_and_reason(
            imdb_id, taste_profile, favorite_actors, watchlist_ids, tmdb_id=item.get("tmdb_id")
        )
        if not include:
            continue
        seen_ids.add(imdb_id)
        if not info:
            info = get_title_metadata(imdb_id=imdb_id, tmdb_id=item.get("tmdb_id"))
        strict.append(
            {
                "imdb_id": imdb_id,
                "title": info.get("title") or item.get("title"),
                "poster": info.get("poster") or item.get("poster"),
                "rating": info.get("rating"),
                "imdb_url": info.get("url", f"https://www.imdb.com/title/{imdb_id}/"),
                "platform": item.get("platform"),
                "release_date": item.get("release_date"),
                "reason": reason,
                "score": score,
            }
        )
    strict.sort(key=lambda r: r["score"], reverse=True)
    picks = strict[:3]

    if len(picks) < 3:
        for item in streaming_items:
            if len(picks) >= 3:
                break
            imdb_id = item.get("imdb_id")
            if not imdb_id or imdb_id in seen_ids:
                continue
            # Bug real que se coló en la primera versión de este relleno:
            # no comprobaba tu lista de votadas, así que una peli que ya
            # tenías puntuada (p.ej. Spider-Man) podía colarse como "mejor
            # disponible". Se excluye igual que en el resto del matching.
            if imdb_id in taste_profile.get("rated_ids", set()):
                continue
            seen_ids.add(imdb_id)
            info = get_title_metadata(imdb_id=imdb_id, tmdb_id=item.get("tmdb_id"))
            rating = info.get("rating")
            platform = item.get("platform") or "tu plataforma"
            reason = f"Estreno reciente en {platform}"
            if rating:
                reason += f" (IMDb {rating})"
            reason += " — no coincide con tus gustos habituales, es la mejor opción disponible para completar el finde"
            picks.append(
                {
                    "imdb_id": imdb_id,
                    "title": info.get("title") or item.get("title"),
                    "poster": info.get("poster") or item.get("poster"),
                    "rating": rating,
                    "imdb_url": info.get("url", f"https://www.imdb.com/title/{imdb_id}/"),
                    "platform": item.get("platform"),
                    "release_date": item.get("release_date"),
                    "reason": reason,
                    "score": 10,
                }
            )

    for i, p in enumerate(picks):
        p["day"] = WEEKEND_DAYS[i] if i < len(WEEKEND_DAYS) else None

    return picks
