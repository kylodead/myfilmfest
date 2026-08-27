"""
Scraper de cartelera de Madrid vía FilmAffinity (cubre, con ID propio de
cine, todos los cines que pediste: comerciales, Cineteca, Filmoteca/Doré,
Sala Equis y Círculo de Bellas Artes incluidos).

Se eligió FilmAffinity en vez de SensaCine porque cada película tiene una
URL con un ID numérico estable (filmXXXXXX.html) fácil de aislar con un
selector simple, y porque FilmAffinity suele dar títulos más "limpios" para
el matching contra IMDb.

Si FilmAffinity cambia su HTML, esta es la única pieza que probablemente
haya que retocar (los selectores están aislados en parse_cinema_page).
"""
import re
import time

import requests
from bs4 import BeautifulSoup

from utils import HEADERS, REQUEST_DELAY, best_guess_imdb

# Tus cines, con su ID de FilmAffinity (comprobado a fecha de creación de
# este proyecto, vía https://www.filmaffinity.com/es/theaters.php?state=ES-M).
# Si algún cine cambia de ID o desaparece de FilmAffinity, corrige aquí.
CINEMAS = {
    "Yelmo Cines Ideal": "433",
    "Cines Embajadores": "1254",
    "Cineteca (Matadero Madrid)": "515",
    "Filmoteca - Cine Doré": "709",
    "Sala Equis": "1261",
    "Círculo de Bellas Artes (Cine Estudio)": "266",
    "Cines Renoir (Plaza de España)": "432",
    "Cines Golem": "384",
    "Mk2 Cine Paz": "302",
    "Cinesa Proyecciones": "271",
}

# Enlaces a fichas de película en FilmAffinity: /es/film123456.html
FILM_LINK_RE = re.compile(r"film\d+\.html$")
TIME_RE = re.compile(r"^\d{1,2}[:h]\d{2}$")


def _fetch_cinema_page(theater_id: str, cinema_name: str = "") -> str:
    url = f"https://www.filmaffinity.com/es/theater-showtimes.php?id={theater_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        time.sleep(REQUEST_DELAY)
        print(f"    [{cinema_name or theater_id}] GET {url} -> HTTP {r.status_code}, {len(r.text)} bytes")
        if r.status_code != 200:
            return ""
        return r.text
    except Exception as e:
        print(f"    [{cinema_name or theater_id}] ERROR al pedir {url}: {e!r}")
        return ""


def parse_cinema_page(html: str):
    """
    Devuelve una lista de dicts {title, showtimes: [...], listing_url}.
    Nota: la maquetación exacta de FilmAffinity puede variar; este parser
    busca de forma tolerante bloques de película + horas, y si algo falla
    para una película concreta simplemente se omite (no rompe el resto).
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    films = []

    seen_ids = set()
    for link in soup.find_all("a", href=FILM_LINK_RE):
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title:
            continue

        # Usamos el ID numérico de la URL como identificador único de la
        # película en esta página (más fiable que el texto del título, que
        # puede repetirse en enlaces distintos -ej. cartel + título-).
        m = re.search(r"film(\d+)\.html", href)
        film_key = m.group(1) if m else title
        if film_key in seen_ids:
            continue

        url = href if href.startswith("http") else "https://www.filmaffinity.com" + href

        # Los horarios pueden estar varios niveles por encima del enlace del
        # título según la maquetación exacta; subimos ancestros hasta
        # encontrar uno que realmente contenga texto con pinta de hora, en
        # vez de asumir un único nivel de contenedor fijo.
        showtimes = []
        container = link.parent
        for _ in range(6):
            if container is None:
                break
            found = [t.strip().replace("h", ":") for t in container.find_all(string=TIME_RE)]
            if found:
                showtimes = found
                break
            container = container.parent

        seen_ids.add(film_key)
        films.append(
            {
                "title": title,
                "showtimes": sorted(set(showtimes)),
                "listing_url": url,
            }
        )
    return films


def get_madrid_billboard():
    """
    Devuelve: { cinema_name: [ {title, showtimes, listing_url, imdb_id,
    imdb_info}, ... ] }
    Intenta resolver cada película a su ficha de IMDb vía búsqueda por título.
    """
    billboard = {}
    for cinema_name, theater_id in CINEMAS.items():
        html = _fetch_cinema_page(theater_id, cinema_name)
        films = parse_cinema_page(html)
        print(f"    [{cinema_name}] {len(films)} películas encontradas en la página")
        enriched = []
        resolved = 0
        for f in films:
            guess = best_guess_imdb(f["title"])
            f["imdb_id"] = guess["imdb_id"] if guess else None
            if f["imdb_id"]:
                resolved += 1
            else:
                print(f"      sin imdb_id para: {f['title']!r}")
            enriched.append(f)
        print(f"    [{cinema_name}] {resolved}/{len(films)} resueltas a un imdb_id")
        billboard[cinema_name] = enriched
    return billboard


if __name__ == "__main__":
    import json

    print(json.dumps(get_madrid_billboard(), ensure_ascii=False, indent=2)[:3000])
