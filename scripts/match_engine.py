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
from datetime import date

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


def _release_date_sort_key(release_date_str):
    """
    Para ordenar por fecha de estreno MÁS RECIENTE primero dentro de un
    mismo nivel de acierto — pedido explícitamente así: en vez de fiarnos de
    una insignia de texto de una página de terceros que no hemos podido
    verificar (ver el historial de intentos en cines_madrid.py), usamos la
    fecha de estreno real en España que ya trae TMDB (utils._spain_release_date,
    con reestrenos/restauraciones de clásicos correctamente detectados —
    caso real que motivó esto: "Cronos", reestreno esta semana pese a ser de
    1993).

    Sin fecha conocida se trata como "lo más antiguo posible", para que se
    vaya al final del grupo en vez de intercalarse al azar entre las que sí
    tienen fecha real.
    """
    if not release_date_str:
        return float("inf")
    try:
        return -date.fromisoformat(release_date_str).toordinal()
    except ValueError:
        return float("inf")


def select_cinema_picks(billboard, taste_profile, favorite_actors, watchlist_ids):
    """
    billboard: { cinema_name: [ {title, showtimes, listing_url, imdb_id},
    ... ] }
    Devuelve lista de recomendaciones para lunes-jueves, agrupadas por
    película (una peli puede estar en varios cines -> se agrupan showtimes).

    Dentro de un mismo nivel de acierto (mismo `score` — pendientes, actor,
    director...) se ordena por fecha de estreno real en España, la más
    reciente primero (ver _release_date_sort_key) — así que también se
    incluye esa fecha en la ficha de salida (`release_date`), por si algún
    día quieres mostrarla en la propia tarjeta.
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
        # Caso real que motivó esto: "La bola negra" aparecía en la cartelera
        # de Cinesa Proyecciones cuando en realidad solo tiene preventa para
        # el 25-27 de septiembre (fecha futura), no una proyección real esta
        # semana. No era un fallo de orden ni de detección de una insignia de
        # texto de la web de terceros (dos intentos fallidos con eso, ver
        # historial en cines_madrid.py) sino que la fecha de estreno en
        # España que trae TMDB confirmaba que aún no se ha estrenado —
        # `upcoming_release_date` (utils._spain_release_info) solo tiene
        # valor en ese caso exacto. Se omite la película entera (no solo se
        # reordena) hasta la semana de su estreno real, pedido explícitamente
        # así: "si la fecha es el 25 pues pelicula omitida hasta la semana de
        # su estreno". No distinguimos aquí el caso de "pase de preview real
        # antes del estreno oficial" porque los horarios que scrapeamos son
        # franjas por día de la semana, no fechas concretas verificables —
        # se prefiere el pequeño riesgo de ocultar un preview genuino antes
        # que seguir mostrando una preventa como si fuera cartelera real.
        if info.get("upcoming_release_date"):
            continue
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
                "release_date": info.get("release_date"),
                "cinemas": entry["cinemas"],
            }
        )

    results.sort(
        key=lambda r: (-r["score"], _release_date_sort_key(r["release_date"]), r["title"] or "")
    )
    return results


WEEKEND_DAYS = ["viernes", "sábado", "domingo"]


WATCHLIST_SCORE = 100  # ver _score_and_reason: es el único motivo que SÍ puede repetirse semana tras semana


def select_streaming_picks(
    streaming_items,
    taste_profile,
    favorite_actors,
    watchlist_ids,
    allow_fallback_fill=True,
    excluded_repeat_ids=None,
):
    """
    streaming_items: lista de {title, release_year, release_date, platform,
    imdb_id, poster} — ya vienen ordenados por recencia real desde
    justwatch_streaming.py (lo más reciente primero).

    Primero se llenan los picks con matches DE VERDAD (pendientes / actor /
    director / género, misma jerarquía que en cines). Si con `streaming_items`
    no hay 3, build_site.py es quien decide qué hacer: normalmente reintenta
    esta misma función con una `streaming_items` más amplia (ventana de más
    días hacia atrás, ver justwatch_streaming.filter_by_window) ANTES de
    aceptar un relleno sin criterio — así, si esta semana el catálogo nuevo no
    tiene nada para ti pero hace 2-3 semanas sí, se prefiere eso a "lo último
    aunque no encaje", como pediste.

    `allow_fallback_fill` controla si, cuando ni ampliando se llega a 3, esta
    llamada debe completar igualmente con los estrenos más recientes
    disponibles aunque no encajen con tus gustos (dejándolo dicho de forma
    explícita en el motivo, nunca disfrazado de acierto) — build_site.py lo
    deja en False mientras todavía puede ampliar la ventana, y solo lo pone a
    True en la última vuelta, cuando ya se ha llegado al tope de ampliación y
    hay que rellenar sí o sí para no dejar el finde a medias.

    `excluded_repeat_ids` son imdb_id que ya se te ofrecieron en semanas
    recientes (ver pick_history.py) por un motivo que NO era "está en tu
    lista de pendientes" — se descartan aquí para no repetir "sale un actor/
    director que te gusta" con el mismo título semana tras semana mientras
    haya otro que también encaje, pedido explícitamente así. La ÚNICA
    excepción es, precisamente, el motivo "está en tu lista de pendientes"
    (WATCHLIST_SCORE): ese sí puede repetirse, porque mientras no la veas
    tiene sentido seguir recordándotela.
    """
    excluded_repeat_ids = excluded_repeat_ids or set()
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
        if score != WATCHLIST_SCORE and imdb_id in excluded_repeat_ids:
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

    if len(picks) < 3 and allow_fallback_fill:
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
            # Mismo criterio de no-repetición que en los matches de verdad:
            # un título ya ofrecido como relleno recientemente no vuelve a
            # colarse aquí mientras haya otro estreno reciente sin ofrecer.
            if imdb_id in excluded_repeat_ids:
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
