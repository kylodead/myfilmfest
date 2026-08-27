# MyFilmFest 🎬

Tu recomendador semanal de cine: cada viernes cruza tus listas de IMDb (votadas, pendientes, actores favoritos) con la cartelera de tus cines de Madrid y las novedades de streaming en tus plataformas (Disney+, Filmin, Movistar Plus+, Netflix, Prime Video), y te propone:

- **Fin de semana** → estrenos recientes en tus plataformas, que están en tu lista de pendientes o encajan con tu cast/gustos.
- **Lunes a jueves** → películas en tus cines de Madrid (Yelmo Ideal, Cines Embajadores, Cineteca, Filmoteca Cine Doré, Sala Equis, Cines Renoir, Cines Golem, Mk2 Cine Paz, Cinesa Proyecciones) que están en tu lista de pendientes o encajan con tu cast/gustos.

Se publica solo, cada viernes, en una web (GitHub Pages) que puedes añadir a la pantalla de inicio del iPhone como una app.

---

## 1. Cómo funciona por dentro (para que sepas qué esperar)

- Tus listas de **Ratings** y **Watchlist** de IMDb bloquean el acceso automático de robots (`robots.txt`), así que **no se pueden leer "en vivo"**. En su lugar, exportas tú mismo los CSV desde IMDb (2 clics) y los subes a la carpeta `data/` cuando quieras refrescarlos. El viernes, el sistema usa siempre el CSV más reciente que haya en el repositorio.
- La cartelera de Madrid se obtiene de SensaCine (que agrega todos tus cines, incluidos los pequeños/filmoteca).
- Las novedades de streaming se obtienen de JustWatch.
- Estas dos fuentes cambian de vez en cuando su web, así que es **normal** que alguna semana el scraper falle para una fuente concreta — el sistema está hecho para no romperse por eso (sigue publicando lo que sí ha podido obtener, y anota el aviso al final de la página). Si un viernes ves el aviso de error, dímelo y te lo arreglo.

## 2. Preparar tus 3 archivos de IMDb

Desde tu perfil de IMDb (`imdb.com` → tu usuario → "Your Ratings" / "Your Watchlist" / tu lista de actores):

1. Entra en la lista.
2. Botón **"Export"** (arriba a la derecha) → se descarga un `.csv`.
3. Repite para Ratings, Watchlist, y tu lista de actores favoritos.

Guarda los 3 archivos como `ratings.csv`, `watchlist.csv` y `actors.csv`.

## 3. Subir el proyecto a GitHub (una vez)

1. Entra en [github.com](https://github.com) con tu cuenta y pulsa **"New repository"**.
2. Nombre sugerido: `myfilmfest`. Puede ser público o privado (con privado, GitHub Pages también funciona). Crea el repo vacío (sin README).
3. Descomprime el ZIP que te he pasado. Entra en el repo recién creado → botón **"Add file" → "Upload files"** → arrastra **todo el contenido** de la carpeta (no la carpeta en sí, su contenido) → "Commit changes".
4. Sustituye los `data/ratings.csv`, `data/watchlist.csv`, `data/actors.csv` de ejemplo por los tuyos: en GitHub, entra en la carpeta `data/`, abre cada archivo, botón del lápiz (editar) o simplemente vuelve a "Add file → Upload files" con tus CSV reales (mismo nombre, para que se sobrescriban).
5. Ve a **Settings → Actions → General**, baja hasta "Workflow permissions" y marca **"Read and write permissions"**. Guarda.
6. Ve a **Settings → Pages**. En "Source" elige **"GitHub Actions"**.
7. Ve a la pestaña **"Actions"** del repo → selecciona el workflow "MyFilmFest - actualización semanal" → botón **"Run workflow"** para lanzarlo a mano la primera vez y comprobar que todo va bien (tarda 1-2 minutos).
8. Cuando termine (icono verde ✓), ve otra vez a **Settings → Pages**: ahí aparecerá la URL pública de tu web (algo como `https://tu-usuario.github.io/myfilmfest/`).

A partir de aquí, **cada viernes se ejecuta solo** (workflow programado) y la web se actualiza sin que tengas que hacer nada, usando siempre el CSV más reciente que haya en `data/`.

### Refrescar tus listas

Cuando quieras que la próxima ejecución use tus listas actualizadas: vuelve a exportar el/los CSV de IMDb y súbelo(s) de nuevo a `data/` (Upload files, mismo nombre). No hace falta hacer nada más.

### Cambiar el día/hora de ejecución

Está en `.github/workflows/weekly.yml`, línea `cron: "0 6 * * 5"` (viernes 08:00 hora de Madrid en horario de verano). Si quieres otra hora, dímelo y te doy la línea exacta.

## 4. Añadir la web al iPhone

1. Abre la URL de tu GitHub Pages en **Safari** (tiene que ser Safari, no Chrome, para que funcione "Añadir a inicio").
2. Botón compartir (el cuadrado con flecha hacia arriba) → **"Añadir a pantalla de inicio"**.
3. Ya tienes el icono de MyFilmFest en tu iPhone, se abre a pantalla completa como una app.

## 5. Estructura del proyecto

```
data/                CSVs que subes tú (ratings, watchlist, actores)
scripts/
  imdb_lists.py       lee los CSV y construye tu perfil de gustos
  cines_madrid.py     scraper de cartelera (SensaCine) para tus cines
  justwatch_streaming.py  novedades de streaming en tus plataformas
  match_engine.py     cruza todo con tus gustos y decide qué recomendar
  build_site.py       orquesta todo y genera docs/data.json
  utils.py            llamadas a IMDb (ficha pública, autocompletado) + caché
docs/                 la webapp (GitHub Pages sirve esta carpeta)
  index.html, styles.css, app.js
  manifest.json, sw.js, icons/    → configuración PWA para iPhone
  data.json           se regenera cada viernes automáticamente
.github/workflows/weekly.yml   el cron que lo ejecuta todo cada viernes
```

## 6. Probarlo en tu Mac (opcional)

Si tienes Python 3.11+ instalado:

```bash
pip install -r requirements.txt
python3 scripts/build_site.py
```

Esto genera `docs/data.json`; abre `docs/index.html` con "Open with Live Server" (o cualquier servidor local — no vale abrirlo con doble clic porque el navegador bloquea el `fetch` a `data.json` desde `file://`) para verlo tal cual se vería publicado.

## 7. Limitaciones honestas

- El matching por "cast o gustos" es una heurística (pendientes > sale un actor favorito > director habitual > coincidencia de géneros), no magia — algunas semanas dará con menos aciertos que otras.
- SensaCine y JustWatch pueden cambiar su web en cualquier momento y romper un scraper puntualmente; el sistema no se cae por eso, simplemente esa fuente faltará esa semana y lo verás avisado en la propia web.
- Los códigos de cine de SensaCine (en `scripts/cines_madrid.py`) están comprobados a fecha de creación de este proyecto (agosto 2026); si algún cine cambia de código, se corrige en una línea.
