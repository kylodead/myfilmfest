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

HISTORIAL DE ESTE FICHERO — cambio importante (septiembre 2026): antes cada
entrada se guardaba con la fecha EXACTA de la ejecución que la generó, y
"ya se ofreció recientemente" se calculaba respecto a esa fecha. Bug real
reportado: al pasar a lanzamientos manuales (sin cron automático), David
probó a relanzar la app varias veces en el mismo día para comprobar un pick
de cine — cada relanzamiento grababa sus propios picks de streaming en el
historial, así que el SIGUIENTE relanzamiento (mismo día, mismo viernes que
se está preparando) se encontraba sus propios resultados de hace un rato ya
"vistos recientemente" y los excluía, degradando el resultado a mitad del
mismo día hasta acabar pareciéndose sospechosamente a la semana anterior.

Ahora cada entrada se guarda bajo la SEMANA para la que se generó (el
viernes de esa semana, `week_of` — lo calcula build_site.py con
_next_weekend_and_week() y lo pasa explícitamente, no se adivina aquí con
"hoy"), y si ya había una entrada de ese mismo imdb_id para esa MISMA
semana, se sobrescribe en vez de sumarse. "Ya se ofreció recientemente" solo
mira semanas ANTERIORES a la que se está generando ahora — así relanzar la
app 10 veces el mismo viernes (o cualquier día de esa misma semana, por si
se prueba fuera de viernes) da siempre el mismo resultado, sin ir
degradándose, y el "no repetir" de verdad sigue funcionando de una semana
real a la siguiente.

HISTORY_MAX_AGE_DAYS más abajo hace que, pasado ese tiempo, el título vuelva
a estar disponible: con actores/directores concretos el catálogo real de
tus 5 plataformas para ESE actor/director no es infinito, así que bloquear
un título para siempre podría dejar la sección de streaming sin
alternativas de verdad antes de tiempo. Medio año de "descanso" es tiempo de
sobra para que no se sienta repetido, sin llegar a un bloqueo permanente.
"""
import json
from datetime import date, timedelta
from pathlib import Path

HISTORY_PATH = Path(__file__).resolve().parent.parent / "cache" / "streaming_pick_history.json"
HISTORY_MAX_AGE_DAYS = 180


def _default_today() -> date:
    # build_site.py siempre pasa `week_of` explícito (ver _next_weekend_and_week),
    # así que esto es solo un respaldo defensivo si alguna vez se llama sin
    # él — usa hora de Madrid, no UTC (ver utils.madrid_today).
    from utils import madrid_today

    return madrid_today()


def _read_valid_entries(week_of: date):
    """Lee el fichero de historial y devuelve solo las entradas todavía
    "vigentes" (dentro de HISTORY_MAX_AGE_DAYS contando desde `week_of`) —
    las caducadas se descartan aquí mismo, tanto al leer para excluir como
    al reescribir, así el fichero no crece sin límite semana tras semana."""
    if not HISTORY_PATH.exists():
        return []
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("shown", [])
    except Exception:
        return []
    cutoff = week_of - timedelta(days=HISTORY_MAX_AGE_DAYS)
    valid = []
    for entry in raw:
        # Compatibilidad con el formato antiguo (clave "date") además del
        # nuevo ("week_of") — así el historial ya guardado antes de este
        # cambio no se pierde ni revienta al leerlo.
        raw_week = entry.get("week_of") or entry.get("date")
        try:
            entry_week = date.fromisoformat(raw_week)
        except Exception:
            continue
        if entry_week >= cutoff and entry.get("imdb_id"):
            valid.append({"imdb_id": entry["imdb_id"], "week_of": entry_week.isoformat()})
    return valid


def load_recent_non_watchlist_ids(week_of: date = None) -> set:
    """imdb_id que se ofrecieron por un motivo que NO es "está en tu lista de
    pendientes" en semanas ANTERIORES a `week_of` (nunca la propia semana que
    se está generando ahora — relanzar la misma semana varias veces no debe
    autoexcluirse sus propios resultados) dentro de los últimos
    HISTORY_MAX_AGE_DAYS días — para excluirlos de volver a salir por ese
    mismo tipo de motivo esta semana."""
    week_of = week_of or _default_today()
    return {
        e["imdb_id"]
        for e in _read_valid_entries(week_of)
        if date.fromisoformat(e["week_of"]) < week_of
    }


def record_shown(imdb_ids, week_of: date = None):
    """Añade estos imdb_id al historial bajo la semana `week_of` — llamar
    SOLO con los picks finales de streaming cuyo motivo no sea "está en tu
    lista de pendientes" (esos se pueden repetir sin límite, no hace falta
    guardarlos aquí). Si ya había una entrada de ese imdb_id para la MISMA
    semana (relanzamiento de prueba), se sobrescribe en vez de duplicarse.
    Fusiona con lo que ya hubiera vigente y poda lo caducado."""
    week_of = week_of or _default_today()
    kept = _read_valid_entries(week_of)
    by_id = {e["imdb_id"]: e for e in kept}
    for imdb_id in imdb_ids:
        if not imdb_id:
            continue
        by_id[imdb_id] = {"imdb_id": imdb_id, "week_of": week_of.isoformat()}
    HISTORY_PATH.parent.mkdir(exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps({"shown": list(by_id.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
