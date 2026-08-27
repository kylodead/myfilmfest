"""
Cruza cartelera de Madrid + novedades de streaming con tus listas (pendientes,
actores favoritos) y tu perfil de gustos real (géneros donde tu nota media
supera claramente tu media general, directores que puntúas alto más de una
vez) — no con "géneros que ves mucho", que no significa que te gusten más
que la media.

Jerarquía de motivos (de más a menos fiable):
  1. Está en tu lista de pendientes           -> score 100
  2. Sale un actor/actriz de tu lista de favoritos -> score 85
  3. La dirige alguien a quien sueles puntuar alto (>=8, más de una vez) -> score 65
  4. Coincide con AL MENOS 2 de tus géneros de verdadera preferencia,
     Y ninguno de esos géneros es genérico-ómnibus por sí solo           -> score 45
Un match "solo por género" nunca se ofrece en solitario si solo coincide 1
género — eso es ruido, no una recomendación.
"""
from utils import imdb_title_info

MIN_GENRE_MATCHES = 2


def _score_and_reason(imdb_id, taste_profile, favorite_actors, watchlist_ids):
    """Devuelve (incluir: bool, motivo: str, score: int) para un imdb_id."""
    if not imdb_id:
        return False, None, 0

    if imdb_id in taste_profile.get("rated_ids", set()):
        return False, None, 0  # ya la has visto/votado

    if imdb_id in watchlist_ids:
        return True, "Está en tu lista de pendientes", 100

    info = imdb_title_info(imdb_id) or {}
    actors = set(info.get("actors") or [])
    directors = set(info.get("directors") or [])
    genres = set(info.get("genres") or [])

    matched_actors = actors & favorite_actors
    if matched_actors:
        who = " y ".join(sorted(matched_actors)[:2])
        return True, f"Sale {who}, de tus actores favoritos", 85

    matched_directors = directors & taste_profile.get("top_directors", set())
    if matched_directors:
        who = ", ".join(sorted(matched_directors)[:1])
        n = taste_profile.get("director_counts", {}).get(list(matched_directors)[0], 0)
        return True, f"Dirigida por {who}, a quien sueles puntuar alto ({n} pelis con nota ≥8)", 65

    genre_affinity = taste_profile.get("genre_affinity", {})
    matched_genres = genres & taste_profile.get("top_genres", set())
    if len(matched_genres) >= MIN_GENRE_MATCHES:
        # ordena por cuánto te gusta ese género realmente (no por frecuencia)
        ordered = sorted(matched_genres, key=lambda g: genre_affinity.get(g, 0), reverse=True)
        return (
            True,
            f"Combina {' y '.join(ordered[:3])}, géneros en los que sueles puntuar por encima de tu media",
            45,
        )

    return False, None, 0


def select_cinema_picks(billboard, taste_profile, favorite_actors, watchlist_ids):
    """
    billboard: { cinema_name: [ {title, showtimes, sensacine_url, imdb_id}, ... ] }
    Devuelve lista de recomendaciones para lunes-jueves, agrupadas por
    película (una peli puede estar en varios cines -> se agrupan showtimes).
    """
    by_imdb = {}
    for cinema_name, films in billboard.items():
        for f in films:
            imdb_id = f.get("imdb_id")
            if not imdb_id:
                continue
            include, reason, score = _score_and_reason(
                imdb_id, taste_profile, favorite_actors, watchlist_ids
            )
            if not include:
                continue
            entry = by_imdb.setdefault(
                imdb_id,
                {"imdb_id": imdb_id, "reason": reason, "score": score, "cinemas": []},
            )
            entry["cinemas"].append(
                {
                    "name": cinema_name,
                    "showtimes": f.get("showtimes", []),
                    "sensacine_url": f.get("sensacine_url"),
                }
            )

    results = []
    for imdb_id, entry in by_imdb.items():
        info = imdb_title_info(imdb_id) or {}
        entry["cinemas"].sort(key=lambda c: c["name"])
        results.append(
            {
                "imdb_id": imdb_id,
                "title": info.get("title"),
                "poster": info.get("poster"),
                "rating": info.get("rating"),
                "imdb_url": info.get("url", f"https://www.imdb.com/title/{imdb_id}/"),
                "reason": entry["reason"],
                "score": entry["score"],
                "cinemas": entry["cinemas"],
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def select_streaming_picks(streaming_items, taste_profile, favorite_actors, watchlist_ids, max_results=6):
    """
    streaming_items: lista de {title, release_year, release_date, platform,
    imdb_id, poster} — ya vienen ordenados por recencia real desde
    justwatch_streaming.py (lo más reciente primero).
    Devuelve hasta `max_results` recomendaciones (para dar opciones de
    viernes/sábado/domingo, no solo una), con poster + link a IMDb +
    plataforma.
    """
    results = []
    seen_ids = set()
    for item in streaming_items:
        imdb_id = item.get("imdb_id")
        if not imdb_id or imdb_id in seen_ids:
            continue
        include, reason, score = _score_and_reason(
            imdb_id, taste_profile, favorite_actors, watchlist_ids
        )
        if not include:
            continue
        seen_ids.add(imdb_id)
        info = imdb_title_info(imdb_id) or {}
        results.append(
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
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max_results]
