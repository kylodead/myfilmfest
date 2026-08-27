"""
Novedades de streaming de la semana en tus plataformas (Disney+, Filmin,
Movistar Plus+, Netflix, Prime Video) para España, usando
"simple-justwatch-python-api" (wrapper no oficial pero mantenido de la API
GraphQL que usa justwatch.com internamente: https://github.com/Electronic-Mango/simple-justwatch-python-api).

A diferencia de la cartelera (donde tenemos que ADIVINAR el imdb_id por
título), JustWatch nos da el imdb_id, el póster y la fecha de estreno
directamente en cada resultado — así que aquí no hay adivinanzas.

Los nombres de proveedor (`technical_name`) se resuelven en tiempo real
contra `providers()` en vez de hardcodear códigos, porque JustWatch los
cambia de vez en cuando entre países.
"""
import re
import unicodedata
from datetime import date, datetime, timedelta

MY_PROVIDER_NAMES = [
    "Netflix",
    "Disney Plus",
    "Amazon Prime Video",
    "Filmin",
    "Movistar Plus+",
]

COUNTRY = "ES"
LANGUAGE = "es"

# Cuánto de "reciente" cuenta como estreno reciente para el finde: los
# últimos 7 días, incluido el propio día de la consulta (viernes) — así lo
# pediste. Antes eran 45 días, una ventana demasiado ancha que dejaba entrar
# estrenos de mes y medio atrás como si fueran "de esta semana".
RECENCY_WINDOW_DAYS = 7


def _norm(s):
    s = (s or "").lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s)


def _resolve_provider_technical_names():
    """
    Pregunta a JustWatch qué proveedores hay en España y hace match por
    nombre (tolerante a mayúsculas/tildes) para no depender de códigos fijos.
    """
    try:
        from simplejustwatchapi.justwatch import providers  # type: ignore
    except ImportError as e:
        print(f"    ERROR: no se pudo importar simplejustwatchapi ({e!r}) — ¿está en requirements.txt?")
        return {}

    try:
        # providers() SOLO acepta `country` (comprobado contra el código fuente
        # real de la librería en GitHub) — pasarle `language` provocaba un
        # TypeError y abortaba toda la resolución de streaming.
        all_providers = providers(country=COUNTRY)
    except Exception as e:
        print(f"    ERROR al llamar providers(): {e!r}")
        return {}

    print(f"    providers() devolvió {len(all_providers)} proveedores para {COUNTRY}")
    if all_providers:
        sample = all_providers[0]
        print(f"    ejemplo de proveedor (para depurar campos): {sample!r}")

    wanted_norm = {_norm(name): name for name in MY_PROVIDER_NAMES}
    resolved = {}
    for p in all_providers:
        # el objeto puede ser dict o namedtuple según versión de la librería
        name = getattr(p, "name", None) or getattr(p, "clear_name", None) or (p.get("name") if isinstance(p, dict) else None)
        technical_name = getattr(p, "technical_name", None) or (p.get("technical_name") if isinstance(p, dict) else None)
        if not name or not technical_name:
            continue
        key = _norm(name)
        for wanted_key, label in wanted_norm.items():
            if wanted_key in key or key in wanted_key:
                resolved[technical_name] = label
    print(f"    proveedores tuyos resueltos: {resolved}")
    return resolved


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# Solo estas cuentan como "está en tu plataforma": incluida en la
# suscripción (o gratis con anuncios). RENT/BUY/CINEMA no cuentan, aunque
# JustWatch las liste como "oferta" — comprar o ver en cine no es lo que
# pediste para el finde en streaming.
STREAMING_MONETIZATION_TYPES = {"FLATRATE", "ADS", "FREE"}


def get_weekly_streaming_releases():
    """
    Devuelve lista de dicts: {title, platform, imdb_id, poster, release_date,
    release_year}, ordenada de más reciente a menos reciente, ya filtrada a
    estrenos de los últimos RECENCY_WINDOW_DAYS días.
    Best-effort: si la librería o la API fallan (JustWatch cambia su
    esquema), devuelve lista vacía en vez de romper el pipeline entero.

    IMPORTANTE: el parámetro `providers=[...]` de search() resultó NO
    filtrar de verdad (comprobado en real: los 5 proveedores devolvían
    exactamente los mismos 40 resultados) — así que una peli podía salir
    etiquetada "Netflix" sin estar realmente disponible ahí (p.ej. estrenos
    que solo están en cines). Ahora se pide UNA lista amplia sin fiarse del
    filtro, y se valida caso por caso mirando las ofertas reales de cada
    película (`e.offers`), aceptando solo las que de verdad tienen una
    oferta de tipo suscripción/gratis en alguna de tus plataformas.
    """
    try:
        from simplejustwatchapi.justwatch import search  # type: ignore
    except ImportError as e:
        print(f"    ERROR: no se pudo importar simplejustwatchapi.search ({e!r})")
        return []

    provider_map = _resolve_provider_technical_names()  # technical_name -> label bonito
    if not provider_map:
        print("    no se resolvió ningún proveedor tuyo, no hay nada que buscar")
        return []

    cutoff = date.today() - timedelta(days=RECENCY_WINDOW_DAYS)
    this_year = date.today().year

    try:
        entries = search(
            title="",
            country=COUNTRY,
            language=LANGUAGE,
            count=100,
            best_only=False,  # necesitamos TODAS las ofertas de cada peli, no solo "la mejor", para poder validar de verdad
            min_release_year=this_year - 1,
            object_types=["MOVIE"],
        )
    except Exception as e:
        print(f"    ERROR en search(): {e!r}")
        entries = []
    print(f"    search() (estrenos recientes en ES, sin filtrar por proveedor) devolvió {len(entries)} resultados")

    all_items = []
    seen = set()
    for e in entries:
        rd = _parse_date(getattr(e, "release_date", None))
        if not rd or rd < cutoff:
            continue  # no es un estreno reciente, lo descartamos
        imdb_id = getattr(e, "imdb_id", None)
        if not imdb_id:
            continue

        matched_label = None
        for offer in getattr(e, "offers", None) or []:
            mtype = getattr(offer, "monetization_type", None)
            if mtype not in STREAMING_MONETIZATION_TYPES:
                continue
            package = getattr(offer, "package", None)
            technical_name = getattr(package, "technical_name", None) if package else None
            if technical_name in provider_map:
                matched_label = provider_map[technical_name]
                break

        if not matched_label:
            continue  # sin oferta real de suscripción en tus plataformas (p.ej. solo está en cines)

        key = (imdb_id, matched_label)
        if key in seen:
            continue
        seen.add(key)
        title = getattr(e, "title", None)
        all_items.append(
            {
                "title": title,
                "platform": matched_label,
                "imdb_id": imdb_id,
                # JustWatch ya nos da el tmdb_id gratis en la misma respuesta —
                # con esto match_engine puede pedir el reparto/director a TMDB
                # sin tener que resolver primero el imdb_id, un paso menos.
                "tmdb_id": getattr(e, "tmdb_id", None),
                "poster": _full_poster_url(getattr(e, "poster", None)),
                "release_date": rd.isoformat(),
                "release_year": getattr(e, "release_year", None),
            }
        )
        print(f"    ✓ {title!r} -> oferta real confirmada en {matched_label}")

    all_items.sort(key=lambda i: i["release_date"], reverse=True)
    print(f"    {len(all_items)} estrenos recientes con oferta de streaming real confirmada en tus plataformas")
    return all_items


def _full_poster_url(poster_path):
    """El campo `poster` de JustWatch suele venir como ruta relativa tipo
    '/poster/123456/{profile}'; hay que completarla con su CDN."""
    if not poster_path:
        return None
    if poster_path.startswith("http"):
        return poster_path
    path = poster_path.replace("{profile}", "s332")
    return f"https://images.justwatch.com{path}"


if __name__ == "__main__":
    import json

    print(json.dumps(get_weekly_streaming_releases(), ensure_ascii=False, indent=2))
