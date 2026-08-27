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
import time
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

# Cuánto de "reciente" cuenta como estreno reciente para el finde. Ajustable.
RECENCY_WINDOW_DAYS = 45


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
        all_providers = providers(country=COUNTRY, language=LANGUAGE)
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


def get_weekly_streaming_releases():
    """
    Devuelve lista de dicts: {title, platform, imdb_id, poster, release_date,
    release_year}, ordenada de más reciente a menos reciente, ya filtrada a
    estrenos de los últimos RECENCY_WINDOW_DAYS días.
    Best-effort: si la librería o la API fallan (JustWatch cambia su
    esquema), devuelve lista vacía en vez de romper el pipeline entero.
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

    all_items = []
    seen = set()

    for technical_name, label in provider_map.items():
        try:
            entries = search(
                title="",
                country=COUNTRY,
                language=LANGUAGE,
                count=40,
                best_only=True,
                providers=[technical_name],
                min_release_year=this_year - 1,
                object_types=["MOVIE"],
            )
        except Exception as e:
            print(f"    ERROR en search() para {label} ({technical_name}): {e!r}")
            entries = []
        time.sleep(0.3)
        print(f"    [{label}] search() devolvió {len(entries)} resultados")

        kept = 0
        for e in entries:
            rd = _parse_date(getattr(e, "release_date", None))
            if not rd or rd < cutoff:
                continue  # no es un estreno reciente, lo descartamos
            imdb_id = getattr(e, "imdb_id", None)
            if not imdb_id:
                continue
            kept += 1
            key = (imdb_id, technical_name)
            if key in seen:
                continue
            seen.add(key)
            all_items.append(
                {
                    "title": getattr(e, "title", None),
                    "platform": label,
                    "imdb_id": imdb_id,
                    "poster": _full_poster_url(getattr(e, "poster", None)),
                    "release_date": rd.isoformat(),
                    "release_year": getattr(e, "release_year", None),
                }
            )
        print(f"    [{label}] {kept} dentro de los últimos {RECENCY_WINDOW_DAYS} días")

    all_items.sort(key=lambda i: i["release_date"], reverse=True)
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
