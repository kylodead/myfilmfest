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
    today = date.today()
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

    cinema_picks = select_cinema_picks(
        billboard, taste_profile, favorite_actors, watchlist_ids
    )

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
        allow_fallback_fill=False,
    )
    while len(streaming_picks) < 3 and window < MAX_RECENCY_WINDOW_DAYS:
        window += 7
        print(f"    solo {len(streaming_picks)} match(es) con tus gustos en {window - 7} días — ampliando a {window} días...")
        streaming_window_items = filter_by_window(streaming_items_all, window)
        streaming_picks = select_streaming_picks(
            streaming_window_items, taste_profile, favorite_actors, watchlist_ids,
            allow_fallback_fill=False,
        )
    if len(streaming_picks) < 3:
        print(f"    sigue sin haber 3 matches tras ampliar hasta {window} días — se rellena con lo más reciente disponible")
        streaming_picks = select_streaming_picks(
            streaming_window_items, taste_profile, favorite_actors, watchlist_ids,
            allow_fallback_fill=True,
        )

    friday, saturday, sunday, monday, thursday = _next_weekend_and_week()

    output = {
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "week_label": f"{friday.strftime('%d/%m')} – {thursday.strftime('%d/%m')}",
        "weekend": {
            "range": f"{saturday.strftime('%d/%m')} – {sunday.strftime('%d/%m')}",
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
