"""
Scraper de cartelera de Madrid vía SensaCine (cubre, con código propio de
cine, todos los cines que pediste: comerciales, Cineteca, Filmoteca/Doré y
Sala Equis incluidos). Un único formato de página por cine, en vez de un
scraper distinto para cada web -> mucho más mantenible.

Si SensaCine cambia su HTML, esta es la única pieza que probablemente haya
que retocar (los selectores están aislados en parse_cinema_page).
"""
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

from utils import HEADERS, REQUEST_DELAY, best_guess_imdb

# Tus cines, con su código de SensaCine (comprobado a fecha de creación de
# este proyecto). Si algún cine cambia de código o SensaCine dejara de
# tenerlo, corrige aquí.
CINEMAS = {
    "Yelmo Cines Ideal": "E0621",
    "Cines Embajadores": "E1032",
    "Cineteca (Matadero Madrid)": "E0781",
    "Filmoteca - Cine Doré": "G02GQ",
    "Sala Equis": "G0FUY",
    "Círculo de Bellas Artes (Cine Estudio)": "E0687",
    "Cines Renoir (Plaza de España)": "E0577",
    "Cines Golem": "E0347",
    "Mk2 Cine Paz": "E0564",
    "Cinesa Proyecciones": "E0402",
}


def _fetch_cinema_page(code: str) -> str:
    url = f"https://www.sensacine.com/cines/cine/{code}/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        time.sleep(REQUEST_DELAY)
        if r.status_code != 200:
            return ""
        return r.text
    except Exception:
        return ""


def parse_cinema_page(html: str):
    """
    Devuelve una lista de dicts {title, showtimes: [...], sensacine_url}.
    Nota: la maquetación exacta de SensaCine puede variar; este parser busca
    de forma tolerante bloques de película + horas, y si algo falla para una
    película concreta simplemente se omite (no rompe el resto).
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    films = []

    # Cada película suele estar en un contenedor con enlace a /pelicula/
    seen_titles = set()
    for link in soup.select('a[href*="/pelicula/"]'):
        title = link.get_text(strip=True)
        if not title or title in seen_titles:
            continue
        container = link.find_parent(["div", "li", "article"])
        showtimes = []
        if container:
            for t in container.find_all(string=re.compile(r"^\d{1,2}[:h]\d{2}$")):
                showtimes.append(t.strip().replace("h", ":"))
        seen_titles.add(title)
        films.append(
            {
                "title": title,
                "showtimes": sorted(set(showtimes)),
                "sensacine_url": "https://www.sensacine.com" + link.get("href", ""),
            }
        )
    return films


def get_madrid_billboard():
    """
    Devuelve: { cinema_name: [ {title, showtimes, sensacine_url, imdb_id,
    imdb_info}, ... ] }
    Intenta resolver cada película a su ficha de IMDb vía búsqueda por título.
    """
    billboard = {}
    for cinema_name, code in CINEMAS.items():
        html = _fetch_cinema_page(code)
        films = parse_cinema_page(html)
        enriched = []
        for f in films:
            guess = best_guess_imdb(f["title"])
            f["imdb_id"] = guess["imdb_id"] if guess else None
            enriched.append(f)
        billboard[cinema_name] = enriched
    return billboard


if __name__ == "__main__":
    import json

    print(json.dumps(get_madrid_billboard(), ensure_ascii=False, indent=2)[:3000])
