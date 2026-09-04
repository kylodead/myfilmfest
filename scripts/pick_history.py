"""
Historial de qué títulos de streaming ya se te han OFRECIDO como recomendación
(no "visto" — eso ya lo llevan tus CSVs de IMDb), para no repetir la misma
peli semana tras semana cuando el motivo es "sale un actor/director/género
que te gusta" — pedido explícitamente así: si hay más de una opción que
encaja, mejor variar; solo se permite repetir cuando el motivo es "está en
tu lista de pendientes", porque ahí sí tiene sentido seguir recordándotela
mientras no la veas.

Se guarda en cache/streaming_pick_history.json — la misma carpeta "cache/"
que el workflow YA commitea junto a docs/data.json en cada ejecución
(ver .github/workflows/weekly.yml), así que no hace falta tocar el workflow
para que este historial persista de una semana a la siguiente.

Cada entrada guarda también la fecha en que se ofreció, y HISTORY_MAX_AGE_DAYS
más abajo hace que, pasado ese tiempo, el título vuelva a estar disponible:
con actores/directores concretos el catálogo real de tus 5 plataformas para
ESE actor/director no es infinito, así que bloquear un título para siempre
podría dejar la sección de streaming sin alternativas de verdad antes de
tiempo. Medio año de "descanso" es tiempo de sobra para que no se sienta
repetido, sin llegar a un bloqueo permanente.
"""
import json
from datetime import date, timedelta
from pathlib import Path

HISTORY_PATH = Path(__file__).resolve().parent.parent / "cache" / "streaming_pick_history.json"
HISTORY_MAX_AGE_DAYS = 180


def _read_valid_entries(today: date):
    """Lee el fichero de historial y devuelve solo las entradas todavía
    "vigentes" (dentro de HISTORY_MAX_AGE_DAYS) — las caducadas se descartan
    aquí mismo, tanto al leer para excluir como al reescribir, así el fichero
    no crece sin límite semana tras semana."""
    if not HISTORY_PATH.exists():
        return []
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("shown", [])
    except Exception:
        return []
    cutoff = today - timedelta(days=HISTORY_MAX_AGE_DAYS)
    valid = []
    for entry in raw:
        try:
            shown_date = date.fromisoformat(entry["date"])
        except Exception:
            continue
        if shown_date >= cutoff and entry.get("imdb_id"):
            valid.append(entry)
    return valid


def load_recent_non_watchlist_ids(today: date = None) -> set:
    """imdb_id que se ofrecieron por un motivo que NO es "está en tu lista de
    pendientes" en los últimos HISTORY_MAX_AGE_DAYS días — para excluirlos de
    volver a salir por ese mismo tipo de motivo esta semana."""
    today = today or date.today()
    return {entry["imdb_id"] for entry in _read_valid_entries(today)}


def record_shown(imdb_ids, today: date = None):
    """Añade estos imdb_id al historial con la fecha de hoy — llamar SOLO con
    los picks finales de streaming cuyo motivo no sea "está en tu lista de
    pendientes" (esos se pueden repetir sin límite, no hace falta guardarlos
    aquí). Fusiona con lo que ya hubiera vigente y poda lo caducado."""
    today = today or date.today()
    kept = _read_valid_entries(today)
    seen_ids = {e["imdb_id"] for e in kept}
    for imdb_id in imdb_ids:
        if not imdb_id or imdb_id in seen_ids:
            continue
        kept.append({"imdb_id": imdb_id, "date": today.isoformat()})
        seen_ids.add(imdb_id)
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps({"shown": kept}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
