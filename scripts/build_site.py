"""
Orquesta todo el pipeline semanal de MyFilmFest:
  1. Lee tus CSVs de IMDb (data/)
  2. Scrapea cartelera de Madrid (FilmAffinity) para tus cines
  3. Consulta novedades de streaming (JustWatch) en tus plataformas
  4. Cruza todo con tu watchlist / actores favoritos / gustos
  5. Escribe docs/data.json, que es lo que lee la webapp

Pensado para ejecutarse cada viernes desde GitHub Actions, pero también
puedes correrlo en local con:  python scripts/build_site.py
"""
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


def _next_weekend_and_week():
    from utils import madrid_today

    # Hora de Madrid, no UTC (GitHub Actions corre en UTC) — ver el
    # comentario de madrid_today() en utils.py para el bug real que esto
    # evita (una fecha de estreno "de hoy" tratada como futura por el
    # desfase horario entre Madrid y UTC).
    today = madrid_today()
    # próximo viernes (si hoy es viernes, es hoy)
    days_to_friday = (4 - today.weekday()) % 7
    friday = today + timedelta(days=days_to_friday)
    saturday, sunday = friday + timedelta(days=1), friday + timedelta(days=2)
    monday = friday + timedelta(days=3)
    thursday = friday + timedelta(days=6)
    return friday, saturday, sunday, monday, thursday


def main():
    from imdb_lists import (
        build_taste_profile,
        get_favorite_actor_names,
        get_watchlist_ids,
    )

    errors = []

    # Limpieza de cache/ ANTES de generar nada — pedido explícitamente por
    # David al ver que la carpeta crece sin parar en GitHub: borra ficheros
    # de versiones de clave ya obsoletas (ver _OBSOLETE_CACHE_PREFIXES en
    # utils.py) y cualquier ficha que lleve más de un año sin usarse. Ver
    # utils.prune_stale_cache para el detalle de qué borra y por qué.
    from utils import prune_stale_cache

    obsolete_deleted, stale_deleted = prune_stale_cache()
    if obsolete_deleted or stale_deleted:
        print(
            f"→ Limpieza de caché: {obsolete_deleted} fichero(s) de versión "
            f"obsoleta + {stale_deleted} fichero(s) sin usar hace más de un "
            f"año, borrados"
        )

    print("→ Leyendo tus listas de IMDb (CSV en data/)...")
    taste_profile = build_taste_profile()
    favorite_actors = get_favorite_actor_names()
    watchlist_ids = get_watchlist_ids()
    print(
        f"  votadas: {len(taste_profile['rated_ids'])} | "
        f"pendientes: {len(watchlist_ids)} | actores favoritos: {len(favorite_actors)}"
    )

    print("→ Scrapeando cartelera de Madrid (FilmAffinity)...")
    try:
        from cines_madrid import get_madrid_billboard

        billboard = get_madrid_billboard()
    except Exception as e:
        errors.append(f"cartelera: {e}")
        traceback.print_exc()
        billboard = {}

    print("→ Consultando novedades de streaming (JustWatch)...")
    try:
        from justwatch_streaming import (
            MAX_RECENCY_WINDOW_DAYS,
            RECENCY_WINDOW_DAYS,
            filter_by_window,
            get_weekly_streaming_releases,
        )

        # Trae de golpe todo lo publicado hasta MAX_RECENCY_WINDOW_DAYS atrás
        # (una sola pasada de scraping); qué parte de eso se usa lo decide el
        # bucle de más abajo, ampliando la ventana sin volver a pedir nada.
        streaming_items_all = get_weekly_streaming_releases()
    except Exception as e:
        errors.append(f"justwatch: {e}")
        traceback.print_exc()
        streaming_items_all = []
        RECENCY_WINDOW_DAYS = 7
        MAX_RECENCY_WINDOW_DAYS = 7

        def filter_by_window(items, window_days):
            return items

    print("→ Cruzando datos con tus gustos...")
    from match_engine import select_cinema_picks, select_streaming_picks
    from pick_history import load_recent_shown_ids, record_shown

    # Se calcula aquí (antes de lo que lo necesitaba antes) porque el
    # historial de streaming ahora lo usa como clave de "semana" — ver más
    # abajo y el comentario en pick_history.py.
    friday, saturday, sunday, monday, thursday = _next_weekend_and_week()

    cinema_picks = select_cinema_picks(
        billboard, taste_profile, favorite_actors, watchlist_ids
    )

    # Títulos que ya se te ofrecieron en streaming en SEMANAS ANTERIORES (no
    # en esta misma) — para no repetir el mismo título semana tras semana
    # mientras haya otro que también encaje. Se pasa `friday` (la semana para
    # la que se está generando esto) para que relanzar la app varias veces en
    # el mismo día/semana de prueba no se autoexcluya sus propios resultados
    # de hace un rato — bug real reportado: probar dos veces en un día hacía
    # que la segunda pasada excluyera lo que acababa de ofrecer la primera,
    # degradando el resultado a mitad del mismo día.
    #
    # CAMBIO (27 sept 2026, pedido explícitamente por David): antes esta
    # exclusión NO se aplicaba a los picks con motivo "está en tu lista de
    # pendientes" (WATCHLIST_SCORE) — podían repetirse indefinidamente
    # mientras no se vieran. David reportó "Resurrection" y "Buena suerte,
    # pásalo bien, no mueras" repitiéndose varias semanas seguidas en
    # streaming y pidió explícitamente: "si sale una semana en streaming no
    # se repite" — sin excepción para pendientes. Ahora se guardan y excluyen
    # TODOS los picks de streaming por igual (ver
    # pick_history.load_recent_shown_ids, antes "load_recent_non_watchlist_ids").
    excluded_repeat_ids = load_recent_shown_ids(week_of=friday)
    if excluded_repeat_ids:
        print(f"    {len(excluded_repeat_ids)} título(s) ya ofrecidos recientemente, se evitan como repetición")

    # Si en los últimos 7 días no hay (suficientes) estrenos de streaming que
    # encajen con tus gustos, en vez de rellenar directamente con "lo último
    # aunque no encaje" (lo que se hacía antes), se prueba mirando más atrás
    # en el tiempo — 14, 21, 28 días... — hasta encontrar 3 que sí encajen o
    # hasta agotar MAX_RECENCY_WINDOW_DAYS. Con el catálogo tan grande de tus
    # plataformas, casi siempre aparece algo mirando unas semanas atrás; el
    # relleno sin criterio queda como último recurso de verdad, no como lo
    # primero que se prueba.
    window = RECENCY_WINDOW_DAYS
    streaming_window_items = filter_by_window(streaming_items_all, window)
    streaming_picks = select_streaming_picks(
        streaming_window_items, taste_profile, favorite_actors, watchlist_ids,
        allow_fallback_fill=False, excluded_repeat_ids=excluded_repeat_ids,
    )
    while len(streaming_picks) < 3 and window < MAX_RECENCY_WINDOW_DAYS:
        window += 7
        print(f"    solo {len(streaming_picks)} match(es) con tus gustos en {window - 7} días — ampliando a {window} días...")
        streaming_window_items = filter_by_window(streaming_items_all, window)
        streaming_picks = select_streaming_picks(
            streaming_window_items, taste_profile, favorite_actors, watchlist_ids,
            allow_fallback_fill=False, excluded_repeat_ids=excluded_repeat_ids,
        )
    if len(streaming_picks) < 3:
        print(f"    sigue sin haber 3 matches tras ampliar hasta {window} días — se rellena con lo más reciente disponible")
        streaming_picks = select_streaming_picks(
            streaming_window_items, taste_profile, favorite_actors, watchlist_ids,
            allow_fallback_fill=True, excluded_repeat_ids=excluded_repeat_ids,
        )

    # Se guardan en el historial TODOS los picks de streaming de esta semana,
    # sin excepción (incluidos los de "está en tu lista de pendientes" —
    # cambio del 27 sept 2026, ver el comentario largo más arriba, junto a
    # `excluded_repeat_ids`) — así la semana que viene no se te vuelve a
    # ofrecer el mismo título. Se anota bajo la semana `friday` (no "hoy"):
    # relanzar el mismo viernes varias veces sobrescribe la misma entrada de
    # semana en vez de acumular varias, ver pick_history.py.
    record_shown(
        (p["imdb_id"] for p in streaming_picks),
        week_of=friday,
    )

    output = {
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "week_label": f"{friday.strftime('%d/%m')} – {thursday.strftime('%d/%m')}",
        "weekend": {
            # Empieza en viernes, no en sábado: el propio "finde" de la app
            # YA incluye el pick del viernes (ver WEEKEND_DAYS en
            # match_engine.py) — el rango mostrado tiene que reflejar eso o
            # el viernes queda fuera del texto aunque sí aparezca la tarjeta.
            "range": f"{friday.strftime('%d/%m')} – {sunday.strftime('%d/%m')}",
            "picks": streaming_picks,
        },
        "cinema_week": {
            "range": f"{monday.strftime('%d/%m')} – {thursday.strftime('%d/%m')}",
            "picks": cinema_picks,
        },
        "errors": errors,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    out_path = DOCS_DIR / "data.json"
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✓ Escrito {out_path} — {len(cinema_picks)} picks cine, {len(streaming_picks)} picks streaming")


if __name__ == "__main__":
    main()
