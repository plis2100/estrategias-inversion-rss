from __future__ import annotations

import email.utils
import html
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup


BASE = "https://www.estrategiasdeinversion.com"

SITEMAP_NOTICIAS = (
    f"{BASE}/news-sitemap.xml"
)

SITEMAPS_SECCIONES = [
    f"{BASE}/sitemap-actualidad-noticias-empresas-ultimo-mes.xml",
    f"{BASE}/sitemap-actualidad-noticias-espanya-ultimo-mes.xml",
    f"{BASE}/sitemap-actualidad-otras-noticias-ultimo-mes.xml",
    f"{BASE}/sitemap-analisis-noticias-ultimo-mes.xml",
    f"{BASE}/sitemap-analisis-informes-ultimo-mes.xml",
]

SALIDA = Path("rss.xml")
ESTADO = Path("estado.json")

MAXIMO_ARTICULOS_RSS = 1000
MAXIMO_URL_ESTADO = 20000
MAXIMO_NUEVOS_POR_EJECUCION = 150
TRABAJADORES = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
)

CABECERAS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml,"
        "application/rss+xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Cache-Control": "no-cache",
}

NS_SITEMAP = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


def descargar(url: str, timeout: int = 60) -> bytes:
    peticion = urllib.request.Request(
        url,
        headers=CABECERAS,
    )

    with urllib.request.urlopen(
        peticion,
        timeout=timeout,
    ) as respuesta:
        contenido = respuesta.read()

    if not contenido:
        raise RuntimeError(f"Respuesta vacía: {url}")

    return contenido


def limpiar_url(url: str) -> str:
    return url.strip().split("#", 1)[0].split("?", 1)[0]


def es_articulo(url: str) -> bool:
    ruta = urlparse(url).path.lower()

    if not url.startswith(BASE):
        return False

    patrones_validos = (
        "/actualidad/noticias/",
        "/analisis/",
    )

    if not any(patron in ruta for patron in patrones_validos):
        return False

    # Los artículos terminan normalmente en -n-123456
    if not re.search(r"-n-\d+/?$", ruta):
        return False

    exclusiones = (
        "/cotizaciones/",
        "/cursos/",
        "/premium/",
        "/herramientas/",
        "/diccionario/",
        "/corporativo/",
        "/foros/",
        "/tags/",
    )

    return not any(exclusion in ruta for exclusion in exclusiones)


def leer_news_sitemap() -> dict[str, dict]:
    resultado: dict[str, dict] = {}

    try:
        raiz = ET.fromstring(descargar(SITEMAP_NOTICIAS))
    except Exception as error:
        print(f"No se pudo leer news-sitemap: {error}")
        return resultado

    for nodo in raiz.findall("sm:url", NS_SITEMAP):
        url = nodo.findtext("sm:loc", default="", namespaces=NS_SITEMAP)
        url = limpiar_url(url)

        if not es_articulo(url):
            continue

        noticia = nodo.find("news:news", NS_SITEMAP)

        titulo = ""
        fecha = ""

        if noticia is not None:
            titulo = noticia.findtext(
                "news:title",
                default="",
                namespaces=NS_SITEMAP,
            ).strip()

            fecha = noticia.findtext(
                "news:publication_date",
                default="",
                namespaces=NS_SITEMAP,
            ).strip()

        resultado[url] = {
            "url": url,
            "titulo": titulo,
            "fecha": fecha,
        }

    return resultado


def leer_sitemap_seccion(url_sitemap: str) -> set[str]:
    urls: set[str] = set()

    try:
        raiz = ET.fromstring(descargar(url_sitemap))
    except Exception as error:
        print(f"No se pudo leer {url_sitemap}: {error}")
        return urls

    for nodo in raiz.findall("sm:url", NS_SITEMAP):
        url = nodo.findtext(
            "sm:loc",
            default="",
            namespaces=NS_SITEMAP,
        )

        url = limpiar_url(url)

        if es_articulo(url):
            urls.add(url)

    return urls


def leer_todas_las_secciones() -> set[str]:
    urls: set[str] = set()

    with ThreadPoolExecutor(
        max_workers=min(5, len(SITEMAPS_SECCIONES))
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(leer_sitemap_seccion, sitemap): sitemap
            for sitemap in SITEMAPS_SECCIONES
        }

        for trabajo in as_completed(trabajos):
            urls.update(trabajo.result())

    return urls


def cargar_estado() -> tuple[set[str], bool]:
    if not ESTADO.exists():
        return set(), True

    try:
        datos = json.loads(
            ESTADO.read_text(encoding="utf-8")
        )

        return set(datos.get("urls_vistas", [])), False

    except (json.JSONDecodeError, OSError):
        return set(), True


def guardar_estado(urls: set[str]) -> None:
    lista = sorted(urls)[-MAXIMO_URL_ESTADO:]

    datos = {
        "ultima_actualizacion": datetime.now(
            timezone.utc
        ).isoformat(),
        "urls_vistas": lista,
    }

    contenido = json.dumps(
        datos,
        ensure_ascii=False,
        indent=2,
    )

    guardar_texto_atomico(
        ESTADO,
        contenido + "\n",
    )


def obtener_meta(
    sopa: BeautifulSoup,
    nombre: str,
    atributo: str = "property",
) -> str:
    etiqueta = sopa.find(
        "meta",
        attrs={atributo: nombre},
    )

    if not etiqueta:
        return ""

    return etiqueta.get("content", "").strip()


def categoria_desde_url(url: str) -> str:
    ruta = urlparse(url).path.lower()

    correspondencias = [
        ("/actualidad/noticias/empresas/", "Empresas"),
        ("/actualidad/noticias/bolsa-espana/", "Bolsa española"),
        ("/actualidad/noticias/bolsa-eeuu/", "Bolsa estadounidense"),
        ("/actualidad/noticias/economia/", "Economía"),
        ("/actualidad/noticias/divisas/", "Divisas"),
        ("/actualidad/noticias/criptomonedas/", "Criptomonedas"),
        ("/actualidad/noticias/", "Actualidad"),
        ("/analisis/trading/", "Trading"),
        ("/analisis/bolsa-y-mercados/informes/", "Informes"),
        ("/analisis/bolsa-y-mercados/", "Análisis de mercados"),
        ("/analisis/", "Análisis"),
    ]

    for patron, categoria in correspondencias:
        if patron in ruta:
            return categoria

    return "Estrategias de Inversión"


def convertir_fecha(fecha: str) -> datetime:
    if not fecha:
        return datetime.now(timezone.utc)

    fecha = fecha.strip().replace("Z", "+00:00")

    try:
        resultado = datetime.fromisoformat(fecha)

        if resultado.tzinfo is None:
            resultado = resultado.replace(
                tzinfo=timezone.utc
            )

        return resultado

    except ValueError:
        try:
            resultado = email.utils.parsedate_to_datetime(fecha)

            if resultado.tzinfo is None:
                resultado = resultado.replace(
                    tzinfo=timezone.utc
                )

            return resultado

        except (TypeError, ValueError):
            return datetime.now(timezone.utc)


def extraer_articulo(
    url: str,
    datos_sitemap: dict | None = None,
) -> dict | None:
    datos_sitemap = datos_sitemap or {}

    try:
        contenido = descargar(url, timeout=45)
        sopa = BeautifulSoup(contentido := contenido, "html.parser")

        titulo = (
            obtener_meta(sopa, "og:title")
            or datos_sitemap.get("titulo", "")
        )

        if not titulo and sopa.title:
            titulo = sopa.title.get_text(
                " ",
                strip=True,
            )

        descripcion = (
            obtener_meta(sopa, "description", "name")
            or obtener_meta(sopa, "og:description")
        )

        fecha = (
            obtener_meta(sopa, "article:published_time")
            or obtener_meta(sopa, "datePublished")
            or datos_sitemap.get("fecha", "")
        )

        autor = obtener_meta(sopa, "author", "name")
        imagen = obtener_meta(sopa, "og:image")

        titulo = html.unescape(
            " ".join(titulo.split())
        )

        descripcion = html.unescape(
            " ".join(descripcion.split())
        )

        if not titulo:
            return None

        return {
            "url": url,
            "titulo": titulo,
            "descripcion": descripcion,
            "fecha": convertir_fecha(fecha),
            "categoria": categoria_desde_url(url),
            "autor": autor,
            "imagen": imagen,
        }

    except Exception as error:
        print(f"No se pudo procesar {url}: {error}")
        return None


def texto_elemento(
    elemento: ET.Element,
    nombre: str,
) -> str:
    nodo = elemento.find(nombre)

    if nodo is None or nodo.text is None:
        return ""

    return nodo.text.strip()


def cargar_rss_anterior() -> dict[str, ET.Element]:
    articulos: dict[str, ET.Element] = {}

    if not SALIDA.exists():
        return articulos

    try:
        raiz = ET.parse(SALIDA).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return articulos

        for item in canal.findall("item"):
            url = (
                texto_elemento(item, "guid")
                or texto_elemento(item, "link")
            )

            url = limpiar_url(url)

            if url:
                articulos[url] = item

    except ET.ParseError:
        print("El rss.xml anterior no era válido")

    return articulos


def fecha_item(item: ET.Element) -> datetime:
    fecha = texto_elemento(item, "pubDate")
    return convertir_fecha(fecha)


def crear_item(datos: dict) -> ET.Element:
    item = ET.Element("item")

    ET.SubElement(item, "title").text = datos["titulo"]
    ET.SubElement(item, "link").text = datos["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "true"},
    )
    guid.text = datos["url"]

    ET.SubElement(item, "pubDate").text = (
        email.utils.format_datetime(datos["fecha"])
    )

    ET.SubElement(item, "category").text = datos["categoria"]

    descripcion = datos.get("descripcion", "")

    if descripcion:
        ET.SubElement(item, "description").text = descripcion

    autor = datos.get("autor", "")

    if autor:
        ET.SubElement(item, "author").text = autor

    imagen = datos.get("imagen", "")

    if imagen:
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": imagen,
                "type": "image/jpeg",
            },
        )

    return item


def crear_rss(
    articulos: dict[str, ET.Element],
) -> ET.ElementTree:
    ordenados = sorted(
        articulos.values(),
        key=fecha_item,
        reverse=True,
    )[:MAXIMO_ARTICULOS_RSS]

    rss = ET.Element("rss", {"version": "2.0"})
    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "Estrategias de Inversión — Todas las publicaciones"
    )

    ET.SubElement(canal, "link").text = BASE

    ET.SubElement(canal, "description").text = (
        "Todas las noticias y análisis públicos de "
        "Estrategias de Inversión."
    )

    ET.SubElement(canal, "language").text = "es-ES"

    ET.SubElement(canal, "lastBuildDate").text = (
        email.utils.format_datetime(
            datetime.now(timezone.utc)
        )
    )

    for item in ordenados:
        canal.append(item)

    return ET.ElementTree(rss)


def guardar_texto_atomico(
    ruta: Path,
    contenido: str,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=".",
        prefix=f"{ruta.stem}_",
        suffix=".tmp",
    ) as temporal:
        temporal.write(contenido)
        temporal_path = Path(temporal.name)

    os.replace(temporal_path, ruta)


def guardar_xml_atomico(arbol: ET.ElementTree) -> None:
    ET.indent(arbol, space="  ")

    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=".",
        prefix="rss_",
        suffix=".xml",
    ) as temporal:
        temporal_path = Path(temporal.name)

        arbol.write(
            temporal,
            encoding="utf-8",
            xml_declaration=True,
        )

    os.replace(temporal_path, SALIDA)


def main() -> None:
    noticias_recientes = leer_news_sitemap()
    urls_secciones = leer_todas_las_secciones()

    urls_vistas, primera_ejecucion = cargar_estado()
    articulos = cargar_rss_anterior()

    if primera_ejecucion:
        # En el primer arranque no mete cientos de noticias antiguas.
        # Registra el mes actual y publica las noticias más recientes.
        candidatos = set(noticias_recientes)
        print(
            "Primera ejecución: se incorporarán "
            f"{len(candidatos)} publicaciones recientes"
        )
    else:
        nuevas_secciones = urls_secciones - urls_vistas

        candidatos = (
            set(noticias_recientes)
            | nuevas_secciones
        )

        print(
            f"Artículos nuevos localizados: "
            f"{len(nuevas_secciones)}"
        )

    pendientes = [
        url
        for url in candidatos
        if url not in articulos
    ]

    pendientes = pendientes[:MAXIMO_NUEVOS_POR_EJECUCION]

    resultados: list[dict] = []

    with ThreadPoolExecutor(
        max_workers=TRABAJADORES
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                extraer_articulo,
                url,
                noticias_recientes.get(url),
            ): url
            for url in pendientes
        }

        for trabajo in as_completed(trabajos):
            resultado = trabajo.result()

            if resultado:
                resultados.append(resultado)

    for resultado in resultados:
        articulos[resultado["url"]] = crear_item(resultado)

    arbol = crear_rss(articulos)
    guardar_xml_atomico(arbol)

    urls_vistas.update(urls_secciones)
    urls_vistas.update(noticias_recientes)
    guardar_estado(urls_vistas)

    print(
        f"RSS actualizado: {len(articulos)} publicaciones. "
        f"Nuevas añadidas: {len(resultados)}"
    )


if __name__ == "__main__":
    main()
