#!/usr/bin/env python3
"""
scraper_agenda.py — Boletines de Reuniones de Comisiones → data/agenda.json
============================================================================
Lista los boletines en /parlamentario/boletines/, descarga los nuevos (IDs más
altos que el último procesado), parsea cada reunión (fecha, hora, comisión, salón,
tipo y temario) y acumula en data/agenda.json (las reuniones pasadas se mantienen).

Tipo de reunión:
  - Por la sección del PDF: REUNIONES DE SENADORES → 'senadores',
    REUNIONES DE ASESORES → 'asesores', REUNIONES BICAMERALES → 'bicameral'.
  - Ajuste: cualquier reunión cuya comisión contenga la palabra BICAMERAL se
    tipifica como 'bicameral', sin importar la sección.

Dos formatos de boletín conviven (el sitio cambió de formato en julio de 2026,
y volvió a cambiarlo levemente en agosto):
  - "Tabular" (histórico): tablas FECHA|HORA|COMISIÓN|SALÓN. El encabezado de
    sección ("REUNIONES DE ASESORES", etc.) NO forma parte de ninguna tabla —
    pdfplumber lo descarta si sólo se leen extract_tables(). Por eso se resuelve
    la sección de cada tabla por posición vertical (ver extraer_filas_con_seccion).
  - "Texto libre" (actual): sin tablas, con marcadores 🗓/🕑/📍/📋. Hasta julio
    de 2026 traía secciones "AGENDA DE SENADORES" / "AGENDA DE ASESORES";
    desde agosto de 2026 ese encabezado desapareció (un único listado, sin
    distinguir senadores de asesores) — por eso la detección de formato se
    basa en el marcador de fecha (RE_FECHA_LINEA) y no en ese encabezado, que
    ahora es opcional (ver _es_texto_formato_libre / parsear_reuniones_texto_libre).

Reuniones "suspendidas": cada boletín nuevo es la versión vigente de la agenda
para las fechas que trae. Si una reunión ya acumulada de un boletín anterior
tenía una de esas fechas y no reaparece en el boletín nuevo, se marca
`suspendida: true` (ver marcar_suspendidas()) — no distingue de una
reprogramación a otra fecha/hora, que se ve igual (desaparece de su fecha
original). Si una reunión suspendida vuelve a aparecer más adelante con la
misma clave, el merge normal la reemplaza por la versión fresca y deja de
estar suspendida.

Tolerante a fallos: si un boletín falla, se registra el error y se continúa.
"""

import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import datetime
import shutil
import subprocess
import tempfile

try:
    import requests
    from bs4 import BeautifulSoup
    import pdfplumber
except ImportError:
    print("ERROR: faltan dependencias. Ejecutá: pip install -r requirements.txt")
    sys.exit(1)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
AGENDA_JSON = os.path.join(DATA_DIR, "agenda.json")
BOLETINES_PROCESADOS_JSON = os.path.join(DATA_DIR, "boletines_procesados.json")
COMISIONES_JSON = os.path.join(DATA_DIR, "comisiones.json")

BASE_URL = "https://www.senado.gob.ar"
URL_BOLETINES = f"{BASE_URL}/parlamentario/boletines/"

# El sitio tiene un filtro anti-bots (F5/Imperva, desde ago/2026) que bloquea
# a quien DICE ser un navegador (User-Agent Mozilla/Chrome/...) sin ejecutar
# su JavaScript -- un User-Agent propio y honesto pasa sin problema. Verificado
# en las 6 fuentes que usa la app y desde 3 redes distintas. NO volver a poner
# un UA de navegador "para disimular": es exactamente lo que se bloquea.
HEADERS = {"User-Agent": "HSN-SistemaLegislativo/1.0 (+https://github.com/Prosecretaria-Parlamentaria-HSN/hsn-sistema-legislativo)"}

DIAS_SEMANA = re.compile(
    r"\b(LUNES|MARTES|MI[EÉ]RCOLES|JUEVES|VIERNES|S[AÁ]BADO|DOMINGO)\b", re.IGNORECASE)

# Encabezados de sección (formato tabular)
SEC_SENADORES = re.compile(r"REUNIONES\s+DE\s+SENADORES", re.IGNORECASE)
SEC_ASESORES = re.compile(r"REUNIONES\s+DE\s+ASESORES", re.IGNORECASE)
SEC_BICAMERAL = re.compile(r"REUNIONES\s+BICAMERAL", re.IGNORECASE)

# Encabezados de sección (formato texto libre)
RE_AGENDA_SEC = re.compile(r"^AGENDA\s+DE\s+(SENADORES|ASESORES)\s*$", re.IGNORECASE)

# Número de expediente: S-565/25, P.E-121/26, CD-12/26, O.V-3/26, etc.
RE_EXPTE_NUM = re.compile(r"EXPTE\.?\s*([A-ZÑ]{1,3}(?:\.[A-ZÑ]{1,2})?-?\s*\d+/\d+)", re.IGNORECASE)
# Igual, pero sin exigir el prefijo "EXPTE." (el formato nuevo no lo trae)
RE_EXP_TOKEN = re.compile(r"\b([A-ZÑ]{1,3}(?:\.[A-ZÑ]{1,2})?-\d+/\d+)\b")

MESES_NOMBRE = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6,
    "JULIO": 7, "AGOSTO": 8, "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}
RE_FECHA_LINEA = re.compile(
    r"🗓\s*([A-ZÁÉÍÓÚÑ]+)\s+(\d{1,2})\s+DE\s+([A-ZÁÉÍÓÚÑ]+)\s*🕑\s*(.+?)\s*📍\s*(.+)$")
# Marca el inicio de una lista de expositores de audiencia pública (no son
# expedientes; hay que dejar de acumular temario a partir de acá).
RE_EXPOSITORES = re.compile(r"\bEXPOSITORES\s*:", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─────────────────────────────── Helpers ─────────────────────────────────────

def _limpiar(texto):
    if not texto:
        return ""
    return re.sub(r"\s+", " ", texto).strip()


def _norm_com(s):
    """Normaliza nombre de comisión: mayúsculas, sin tildes, sin prefijo 'DE '."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s.upper().strip())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"^DE\s+", "", s)
    s = re.sub(r"[^A-Z0-9, ]", "", s)
    return s


def cargar_comisiones_norm():
    """Nombres normalizados de las comisiones permanentes (data/comisiones.json),
    usados para reconocer líneas de nombre de comisión en el formato de texto libre."""
    try:
        data = json.load(open(COMISIONES_JSON, encoding="utf-8"))
        return {_norm_com(c["nombre"]) for c in data}
    except Exception:
        return set()


def _mapear_columnas(row):
    """Mapea las columnas FECHA/HORA/COMISIÓN/SALÓN a su índice real dentro de
    la fila. pdfplumber a veces intercala columnas vacías (tablas de 5-6 celdas
    en vez de 4), así que no se puede asumir posición fija 0/1/2/3."""
    cmap = {}
    for i, cell in enumerate(row):
        if not isinstance(cell, str):
            continue
        u = cell.upper()
        if "FECHA" in u and "fecha" not in cmap:
            cmap["fecha"] = i
        elif "HORA" in u and "hora" not in cmap:
            cmap["hora"] = i
        elif "COMISI" in u and "comision" not in cmap:
            cmap["comision"] = i
        elif ("SALÓN" in u or "SALON" in u) and "salon" not in cmap:
            cmap["salon"] = i
    return cmap


def _es_fila_encabezado(row):
    cmap = _mapear_columnas(row)
    return "fecha" in cmap and "hora" in cmap


def parsear_celda_fecha(celda):
    dia, fecha = "", ""
    for l in [l.strip() for l in (celda or "").split("\n") if l.strip()]:
        if DIAS_SEMANA.match(l):
            dia = l.upper()
        elif re.match(r"^\d{1,2}/\d{2}$", l):
            fecha = l
    return dia, fecha


def parsear_celda_comisiones(celda):
    comisiones, modalidad = [], ""
    for l in [l.strip() for l in (celda or "").split("\n") if l.strip()]:
        m = re.match(r"^\((.+)\)$", l)
        if m:
            modalidad = m.group(1).strip()
        else:
            comisiones.append(l)
    return comisiones, modalidad


def parsear_celda_salon(celda):
    lineas = [l.strip() for l in (celda or "").split("\n") if l.strip()]
    salon = lineas[0] if lineas else ""
    resto = " ".join(lineas[1:]) if len(lineas) > 1 else ""
    return salon, _limpiar(f"{salon} {resto}")


def parsear_temario(texto):
    """Divide el texto de temario (formato tabular) en expedientes
    [{numero, extracto}]. Si no hay EXPTE., devuelve un único item de tipo texto."""
    texto = (texto or "").strip()
    if not texto:
        return []
    # Quitar encabezados de grupo (PROYECTOS DE ...:) — quedan implícitos en el extracto
    if "EXPTE" not in texto.upper():
        return [{"numero": "", "extracto": _limpiar(texto)}]
    partes = re.split(r"(?=EXPTE\.)", texto, flags=re.IGNORECASE)
    items = []
    for parte in partes:
        parte = _limpiar(parte)
        if not parte or "EXPTE" not in parte.upper():
            continue
        m = RE_EXPTE_NUM.search(parte)
        numero = re.sub(r"\s+", "", m.group(1)).upper() if m else ""
        # Extracto: texto tras el número
        if m:
            extracto = parte[m.end():].lstrip(" ,:.-").strip()
        else:
            extracto = parte
        items.append({"numero": numero, "extracto": _limpiar(extracto)})
    return items


def parsear_temario_texto_libre(texto):
    """Divide el texto de temario (formato texto libre, sin 'EXPTE.') en
    expedientes [{numero, extracto}], separando por el patrón S-123/25 /
    P.E-123/26 / etc. donde aparezca. Sin expedientes -> un único item de texto."""
    texto = _limpiar(texto)
    if not texto:
        return []
    matches = list(RE_EXP_TOKEN.finditer(texto))
    if not matches:
        return [{"numero": "", "extracto": texto}]
    items = []
    if matches[0].start() > 0:
        pre = texto[:matches[0].start()].strip(" ,:.-")
        # descartar preámbulos que son sólo una etiqueta de grupo (ej. "PROYECTO DE LEY:")
        if pre and not re.match(r"^[A-ZÁÉÍÓÚÑ ,]+$", pre):
            items.append({"numero": "", "extracto": pre})
    for i, m in enumerate(matches):
        numero = m.group(1).upper()
        inicio = m.end()
        fin = matches[i + 1].start() if i + 1 < len(matches) else len(texto)
        extracto = texto[inicio:fin]
        # limpiar una etiqueta de grupo que haya quedado pegada al final (ej. "... PROYECTOS DE COMUNICACIÓN:")
        # antes de recortar los separadores, porque el ':' de la etiqueta es la pista.
        extracto = re.sub(r"\s*[A-ZÁÉÍÓÚÑ ,]{6,}:\s*$", "", extracto)
        extracto = extracto.strip(" ,:.-")
        items.append({"numero": numero, "extracto": extracto})
    return items


def tipo_reunion(seccion, comisiones):
    """Tipifica según sección, salvo que alguna comisión contenga BICAMERAL."""
    if any("BICAMERAL" in c.upper() for c in comisiones):
        return "bicameral"
    return seccion


# ─────────────────────────────── Parseo del boletín (formato tabular) ────────

def _lineas_de_pagina(page):
    """Agrupa las palabras de una página por línea (mismo 'top' aproximado),
    devolviendo [(top, texto_línea), ...] de arriba hacia abajo. Sirve para
    encontrar encabezados de sección que no forman parte de ninguna tabla."""
    palabras = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
    por_top = {}
    for w in palabras:
        top = round(w["top"])
        por_top.setdefault(top, []).append(w)
    fusionados = []
    for top in sorted(por_top.keys()):
        if fusionados and top - fusionados[-1][0] <= 2:
            fusionados[-1] = (fusionados[-1][0], fusionados[-1][1] + por_top[top])
        else:
            fusionados.append((top, list(por_top[top])))
    resultado = []
    for top, ws in fusionados:
        ws.sort(key=lambda w: w["x0"])
        resultado.append((top, " ".join(w["text"] for w in ws)))
    return resultado


def extraer_filas_con_seccion(pdf):
    """Recorre página por página y asigna a cada fila de cada tabla la sección
    vigente (senadores/asesores/bicameral), determinada por el encabezado de
    sección más cercano por encima en la misma página (o el de la página
    anterior si no hay ninguno en ésta) — el encabezado vive como texto suelto,
    no como fila de tabla, así que no puede detectarse fila por fila."""
    filas = []
    seccion_actual = "senadores"
    for page in pdf.pages:
        eventos = []
        for top, texto_linea in _lineas_de_pagina(page):
            up = texto_linea.upper()
            if SEC_ASESORES.search(up):
                eventos.append((top, "header", "asesores"))
            elif SEC_BICAMERAL.search(up):
                eventos.append((top, "header", "bicameral"))
            elif SEC_SENADORES.search(up):
                eventos.append((top, "header", "senadores"))
        for tabla in page.find_tables():
            eventos.append((tabla.bbox[1], "table", tabla.extract()))
        eventos.sort(key=lambda e: e[0])
        for _, kind, payload in eventos:
            if kind == "header":
                seccion_actual = payload
            else:
                for fila in payload:
                    filas.append((seccion_actual, fila))
    return filas


def parsear_reuniones_tabular(filas_con_seccion):
    """Igual que la versión anterior, pero la sección de cada reunión viene
    resuelta de antemano por posición (ver extraer_filas_con_seccion), en vez
    de inferirse leyendo el texto de la propia fila."""
    reuniones = []
    reunion = None
    esperando_datos = False
    seccion_reunion_actual = "senadores"
    col_map = {"fecha": 0, "hora": 1, "comision": 2, "salon": 3}

    def cerrar():
        nonlocal reunion
        if reunion is not None:
            raw = reunion.pop("_temario_raw", "")
            reunion["temario"] = parsear_temario(raw)
            reunion["tipo"] = tipo_reunion(reunion["seccion"], reunion["comisiones"])
            reuniones.append(reunion)
            reunion = None

    def celda(row, key, default=""):
        idx = col_map.get(key)
        if idx is None or idx >= len(row):
            return default
        return row[idx] if row[idx] is not None else default

    for seccion, row in filas_con_seccion:
        if not row:
            continue
        col0 = _limpiar(row[0] or "")
        up = col0.upper()

        cmap_row = _mapear_columnas(row)
        if "fecha" in cmap_row and "hora" in cmap_row:
            cerrar()
            esperando_datos = True
            seccion_reunion_actual = seccion
            col_map = cmap_row
            continue

        hora_cell = celda(row, "hora", None)
        if esperando_datos and hora_cell and re.match(r"^\d{1,2}:\d{2}$", _limpiar(hora_cell)):
            dia, fecha = parsear_celda_fecha(celda(row, "fecha"))
            comisiones, modalidad = parsear_celda_comisiones(celda(row, "comision"))
            salon, salon_completo = parsear_celda_salon(celda(row, "salon"))
            reunion = {
                "seccion": seccion_reunion_actual,
                "dia": dia,
                "fecha": fecha,
                "hora": _limpiar(hora_cell),
                "modalidad": modalidad,
                "comisiones": comisiones,
                "salon": salon,
                "salon_completo": salon_completo,
                "_temario_raw": "",
                "temario": [],
            }
            esperando_datos = False
            continue

        if up == "TEMARIO" or not col0:
            continue

        # Fila de contenido del temario: no es encabezado ni fila de datos, y ya
        # hay una reunión abierta a la que pertenece.
        if reunion is not None:
            reunion["_temario_raw"] += "\n" + (row[0] or "")

    cerrar()
    return reuniones


# ─────────────────────────────── Parseo del boletín (formato texto libre) ────
# Desde ~agosto de 2026 este formato dejó de traer el encabezado "AGENDA DE
# SENADORES" / "AGENDA DE ASESORES" (ya no separa reuniones de senadores de
# las de asesores; todo va en un único listado), pero sigue usando los
# marcadores 🗓🕑📍📋 — por eso la detección de formato se basa en el marcador
# de fecha, no en ese encabezado (que ahora es opcional).

RE_BOLETIN_PREAMBULO = re.compile(
    r"^(N[°ºo]\s*\d+\s*/\s*\d+|INFORMACI[ÓO]N\s+AL\b.*)$", re.IGNORECASE)


def _es_texto_formato_libre(texto):
    # RE_FECHA_LINEA usa '$' sin re.MULTILINE (se aplica línea por línea en el
    # parser); acá sólo interesa detectar la presencia de los marcadores.
    texto = texto or ""
    return "🗓" in texto and "📍" in texto


def _parece_nombre_comision(linea, comisiones_norm):
    """¿Esta línea es el nombre de una comisión (y no texto de temario)? Las
    reuniones conjuntas listan varias comisiones separadas por "|" en la misma
    línea, así que alcanza con que UNA de las partes lo sea. Las comisiones
    bicamerales ad-hoc y las audiencias públicas de acuerdos no están en
    comisiones.json (no son comisiones permanentes), pero sus nombres siempre
    empiezan con "BICAMERAL" o "ACUERDOS" respectivamente."""
    partes = [p.strip() for p in linea.upper().split("|")] if "|" in linea else [linea.strip().upper()]
    for up in partes:
        if not up:
            continue
        if up.startswith("BICAMERAL") or up.startswith("ACUERDOS"):
            return True
        if _norm_com(up) in comisiones_norm:
            return True
    return False


def _le_sigue_fecha(lineas, i, comisiones_norm):
    """¿La línea en `i` es (el inicio de) un nombre de comisión que termina
    justo antes de una línea de fecha, con a lo sumo 1 línea de continuación
    en el medio y sin que aparezca contenido de temario (token de expediente)
    en el camino? Señal estructural —no depende de reconocer el nombre
    contra comisiones.json— para comisiones ad-hoc que no son ni BICAMERAL
    ni ACUERDOS (p.ej. "ADMINISTRADORA DE LA BIBLIOTECA DEL CONGRESO DE LA
    NACIÓN", una comisión especial que ni siquiera está en comisiones.json).

    Si la línea intermedia YA matchea una comisión oficial reconocida
    (_parece_nombre_comision), esa es la que abre el nombre real -> la línea
    `i` no puede ser también el inicio (era la cola de un ítem de temario sin
    puntuación final que, por casualidad, tiene la reunión siguiente 1-2
    líneas después). Sin este corte, ambas líneas se funden en un solo
    nombre de comisión corrupto (visto en la práctica: boletín 103/26, un
    ítem de temario terminaba en "...OFICINA DE PRESUPUESTO DEL CONGRESO DE
    LA NACIÓN" justo antes de la próxima reunión)."""
    for j in (i + 1, i + 2):
        if j >= len(lineas):
            break
        candidata = lineas[j]
        if RE_FECHA_LINEA.search(candidata):
            return True
        cu = candidata.upper()
        if RE_EXP_TOKEN.search(candidata) or cu.startswith("TEMARIO") or candidata.startswith("📋"):
            return False
        if _parece_nombre_comision(candidata, comisiones_norm):
            return False
    return False


def _comisiones_desde_buffer(buffer):
    """Arma la lista final de comisiones a partir de las líneas acumuladas
    antes de la fecha. Si en algún punto aparece "|" (reunión conjunta en el
    formato narrativo/agosto-2026), se separa por ese carácter; si no, cada
    línea acumulada es una comisión (formato histórico). Descarta artefactos
    de extracción: palabras sueltas de 3 letras o menos y líneas sin ninguna
    letra (p.ej. un "-" suelto de un glifo/bullet del PDF que, al unirse con
    join(), quedaba pegado como prefijo de la primera comisión real -- visto
    en la práctica en el boletín 109/26: "- MINERÍA, ENERGÍA Y COMBUSTIBLES"
    en vez de "MINERÍA, ENERGÍA Y COMBUSTIBLES", lo que rompía el matching
    contra la misma reunión en boletines anteriores/posteriores)."""
    buffer = [c for c in buffer if re.search(r"[A-ZÁÉÍÓÚÑa-záéíóúñ]", c)]
    texto = " ".join(buffer)
    if "|" in texto:
        partes = [p.strip() for p in texto.split("|") if p.strip()]
    else:
        partes = [c for c in buffer if c.strip()]
    limpio = [c for c in partes
              if len(re.sub(r"[^A-ZÁÉÍÓÚÑ]", "", c.upper())) > 3]
    return limpio or partes or buffer


def parsear_reuniones_texto_libre(texto, comisiones_norm):
    """Parsea el formato de texto libre (marcadores 🗓🕑📍📋). Como no hay
    separador explícito entre el temario de una reunión y el nombre de la
    siguiente comisión cuando dos reuniones van seguidas, se reconoce el
    nombre de la próxima comisión contra la lista oficial (o el prefijo
    BICAMERAL). El encabezado "AGENDA DE SENADORES/ASESORES" es opcional: si
    no aparece (boletines desde agosto de 2026), todo se tipifica como
    'senadores' salvo lo que sea bicameral."""
    lineas = [l.strip() for l in (texto or "").split("\n") if l.strip()]
    reuniones = []
    seccion = "senadores"
    comision_buffer = []
    reunion = None
    en_expositores = False

    def cerrar():
        nonlocal reunion
        if reunion is not None:
            raw = reunion.pop("_temario_raw", "")
            reunion["temario"] = parsear_temario_texto_libre(raw)
            expositores_raw = re.sub(r"\s+", " ", reunion.pop("_expositores_raw", "")).strip()
            reunion["expositores"] = expositores_raw or None
            reunion["tipo"] = tipo_reunion(reunion["seccion"], reunion["comisiones"])
            reuniones.append(reunion)
            reunion = None

    for i, linea in enumerate(lineas):
        up = linea.upper()

        m_sec = RE_AGENDA_SEC.match(up)
        if m_sec:
            cerrar()
            comision_buffer = []
            en_expositores = False
            seccion = "senadores" if m_sec.group(1).upper() == "SENADORES" else "asesores"
            continue
        if RE_BOLETIN_PREAMBULO.match(up):
            continue  # encabezado del boletín (N°, INFORMACIÓN AL...)
        if up.startswith("NO HAY REUNIONES"):
            cerrar()
            comision_buffer = []
            en_expositores = False
            continue
        if up.startswith("*SE ACTUALIZAR"):
            cerrar()
            continue
        if re.match(r"^\d+$", up):
            continue  # número de página suelto

        m_fecha = RE_FECHA_LINEA.search(linea)
        if m_fecha:
            cerrar()
            en_expositores = False
            dia_txt, dd, mes_txt, hora_txt, salon_txt = m_fecha.groups()
            mes_num = MESES_NOMBRE.get(mes_txt.upper())
            fecha = f"{int(dd):02d}/{mes_num:02d}" if mes_num else ""
            hora_txt = re.sub(r"\s*H$", "", hora_txt.strip(), flags=re.IGNORECASE).strip().upper()
            if "CONTINUACI" in hora_txt:
                hora = "A continuación"
            else:
                hm = re.match(r"(\d{1,2}):(\d{2})", hora_txt)
                hora = f"{hm.group(1)}:{hm.group(2)}" if hm else hora_txt
            salon_txt = salon_txt.strip()
            salon = re.sub(r"^SAL[OÓ]N\s+", "", salon_txt, flags=re.IGNORECASE)
            reunion = {
                "seccion": seccion,
                "dia": dia_txt.upper(),
                "fecha": fecha,
                "hora": hora,
                "modalidad": "",
                "comisiones": _comisiones_desde_buffer(comision_buffer),
                "salon": salon,
                "salon_completo": salon_txt,
                "_temario_raw": "",
                "_expositores_raw": "",
                "temario": [],
            }
            comision_buffer = []
            continue

        if linea.startswith("📋"):
            continue  # marcador "TEMARIO"

        if RE_EXPOSITORES.match(up):
            en_expositores = True  # lo que sigue son nombres de expositores, no expedientes
            continue

        parece_nombre_ad_hoc = (
            not RE_EXP_TOKEN.search(linea)
            and not linea.rstrip().endswith((".", ":"))
            and _le_sigue_fecha(lineas, i, comisiones_norm)
        )
        if reunion is not None and (_parece_nombre_comision(linea, comisiones_norm) or parece_nombre_ad_hoc):
            cerrar()
            en_expositores = False
            comision_buffer = [linea]
            continue

        if reunion is None:
            comision_buffer.append(linea)
        elif en_expositores:
            reunion["_expositores_raw"] += " " + linea
        else:
            reunion["_temario_raw"] += " " + linea

    cerrar()
    return reuniones



# ─────────────────────────────── Boletín con diseño gráfico (desde 150/26) ──
# Desde el boletín 150/26 el PDF es un diseño gráfico: los títulos (sección,
# comisión, "TEMARIO") y los expedientes son texto con tamaños de letra fijos,
# pero la "pastilla" con DÍA FECHA | HORA | SALÓN está dibujada como forma
# vectorial (no tiene texto extraíble). Esa línea se lee con OCR (tesseract).
TAM_SECCION = 28      # "REUNIONES DE SENADORES / ASESORES" (33 pt)
TAM_COMISION = 17     # nombre de comisión (20 pt)
TAM_SUBTITULO = 14.5  # gris, ej. "AUDIENCIA PÚBLICA" (15 pt)
TAM_TEMARIO = 13      # "TEMARIO" (14 pt)
TAM_GRUPO = 11        # "PROYECTO DE LEY" (12 pt, negro)
RE_PASTILLA = re.compile(
    r"(LUNES|MARTES|MI[EÉ]RCOLES|JUEVES|VIERNES|S[AÁ]BADO|DOMINGO)\s+(\d{1,2})\s+DE\s+([A-ZÁÉÍÓÚÑ]+)\s*[|!Il]?\s*(.+?)\s*[|!Il]\s*(SAL[OÓ]N\s+.+)$",
    re.IGNORECASE)
PALABRAS_COLGANTES = {"DE", "DEL", "LA", "LOS", "LAS", "Y", "E", "EN", "PARA", "CON", "A", "AL", "O"}


def _es_formato_grafico(pdf):
    """¿Boletín de diseño gráfico? Tiene líneas "TEMARIO" en tipografía grande
    (en el formato tabular 'TEMARIO' es sólo el encabezado de una columna)."""
    for page in pdf.pages[:3]:
        for ch in page.chars:
            if ch["size"] >= TAM_TEMARIO and "Roboto" in ch.get("fontname", ""):
                return True
    return False


def _ocr_pastilla(page, top):
    """Lee la pastilla de fecha/hora/salón que está justo arriba de la línea
    'TEMARIO' (ubicada en `top`). Devuelve (dia, fecha 'dd/mm', hora, salon)."""
    if shutil.which("tesseract") is None:
        raise RuntimeError("tesseract no está instalado (apt-get install tesseract-ocr tesseract-ocr-spa)")
    y0 = max(0, top - 66)
    y1 = max(y0 + 10, top - 2)
    recorte = page.crop((0, y0, page.width, y1))
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "p.png")
        recorte.to_image(resolution=300).save(img, format="PNG")
        for lang in ("spa", "eng"):
            for psm in ("6", "7", "11"):
                r = subprocess.run(["tesseract", img, "-", "-l", lang, "--psm", psm],
                                   capture_output=True, text=True)
                for linea in r.stdout.splitlines():
                    m = RE_PASTILLA.search(linea.strip())
                    if not m:
                        continue
                    dia, dd, mes_txt, hora_txt, salon_txt = m.groups()
                    mes_num = MESES_NOMBRE.get(unicodedata.normalize("NFC", mes_txt.upper()))
                    if not mes_num:
                        continue
                    hora_txt = hora_txt.strip().upper()
                    if "CONTINUACI" in hora_txt:
                        hora = "A continuación"
                    else:
                        hm = re.search(r"(\d{1,2})[:.](\d{2})", hora_txt)
                        if not hm:
                            continue
                        hora = f"{int(hm.group(1))}:{hm.group(2)}"
                    salon_txt = re.sub(r"\s+", " ", salon_txt).strip(" |.")
                    salon = re.sub(r"^SAL[OÓ]N\s+", "", salon_txt, flags=re.IGNORECASE)
                    return dia.upper(), f"{int(dd):02d}/{mes_num:02d}", hora, salon.upper(), salon_txt.upper()
    return None


def _unir_titulos(lineas, comisiones_norm):
    """Junta títulos de comisión partidos en 2 renglones (p.ej. "... DEL" +
    "SEGUIMIENTO DE ..."); si no se parte, cada renglón es una comisión."""
    res = []
    i = 0
    while i < len(lineas):
        actual = lineas[i].strip()
        while i + 1 < len(lineas):
            sig = lineas[i + 1].strip()
            ultima = actual.upper().split()[-1] if actual.split() else ""
            if (ultima in PALABRAS_COLGANTES
                    or _norm_com(actual + " " + sig) in comisiones_norm):
                actual = actual + " " + sig
                i += 1
            else:
                break
        res.append(actual)
        i += 1
    return res


def parsear_boletin_grafico(pdf, comisiones_norm):
    """Parsea el diseño gráfico. Devuelve (reuniones, texto)."""
    # 1) Líneas con tamaño/negrita, en orden de lectura
    lineas = []
    for pn, page in enumerate(pdf.pages):
        for l in page.extract_text_lines(return_chars=True, x_tolerance=3, y_tolerance=3):
            chars = [c for c in l["chars"] if c["text"].strip() and c["text"] != "\u200b"]
            if not chars or l["top"] > page.height - 45:   # pie con nº de página
                continue
            texto = _limpiar(l["text"].replace("\u200b", ""))
            if not texto or texto.upper().startswith("*SE ACTUALIZAR"):
                continue
            c0 = chars[0]
            lineas.append({"p": pn, "top": l["top"], "texto": texto, "size": c0["size"],
                           "bold": "Bold" in c0.get("fontname", "") or "Black" in c0.get("fontname", "")})
    texto_total = "\n".join(l["texto"] for l in lineas)

    reuniones = []
    seccion = "senadores"
    titulos, modalidad = [], ""
    reunion = None
    item = None

    def cerrar():
        nonlocal reunion, item
        if reunion is not None:
            reunion["tipo"] = tipo_reunion(reunion["seccion"], reunion["comisiones"])
            reuniones.append(reunion)
        reunion, item = None, None

    for l in lineas:
        t, up, sz = l["texto"], l["texto"].upper(), l["size"]
        if sz >= TAM_SECCION:
            cerrar(); titulos, modalidad = [], ""
            if SEC_ASESORES.search(up):
                seccion = "asesores"
            elif SEC_SENADORES.search(up):
                seccion = "senadores"
            continue
        if sz >= TAM_COMISION:                       # título de comisión
            if reunion is not None:
                cerrar(); titulos, modalidad = [], ""
            titulos.append(t)
            continue
        if sz >= TAM_TEMARIO and up.startswith("TEMARIO"):
            if reunion is not None:
                cerrar()
            pastilla = _ocr_pastilla(pdf.pages[l["p"]], l["top"])
            if pastilla is None:
                raise RuntimeError(f"no se pudo leer la fecha/hora/salón (OCR) en la página {l['p'] + 1}")
            dia, fecha, hora, salon, salon_txt = pastilla
            try:  # control de coherencia del OCR: el día de la semana debe coincidir con la fecha
                dd_, mm_ = int(fecha[:2]), int(fecha[3:])
                anio_ = datetime.date.today().year
                esperado = ["LUNES", "MARTES", "MIERCOLES", "JUEVES", "VIERNES", "SABADO", "DOMINGO"][
                    datetime.date(anio_, mm_, dd_).weekday()]
                if unicodedata.normalize("NFD", dia).encode("ascii", "ignore").decode().upper() != esperado:
                    log.warning(f"    ! revisar OCR: '{dia} {fecha}' no coincide con el día de la semana")
            except Exception:
                pass
            comisiones = _comisiones_desde_buffer(_unir_titulos(titulos, comisiones_norm))
            reunion = {"seccion": seccion, "dia": dia, "fecha": fecha, "hora": hora,
                       "modalidad": modalidad.upper(), "comisiones": comisiones,
                       "salon": salon, "salon_completo": salon_txt, "temario": [], "expositores": None}
            titulos, modalidad, item = [], "", None
            continue
        if sz >= TAM_SUBTITULO:                      # subtítulo gris
            if reunion is None:
                modalidad = t
            continue
        if reunion is None:
            continue
        if sz >= TAM_GRUPO:                          # etiqueta de grupo (PROYECTO DE LEY...)
            reunion["temario"].append({"numero": "", "extracto": t.rstrip(":")})
            item = None
            continue
        # texto de expedientes (10 pt)
        m = RE_EXP_TOKEN.match(t)
        if m and l["bold"]:
            item = {"numero": m.group(1).upper(), "extracto": t[m.end():].lstrip(" ,:.-").strip()}
            reunion["temario"].append(item)
        elif item is not None:
            item["extracto"] = _limpiar(item["extracto"] + " " + t)
        else:
            item = {"numero": "", "extracto": t}
            reunion["temario"].append(item)
    cerrar()
    return reuniones, texto_total


# ─────────────────────────────── Orquestación del parseo ─────────────────────

def extraer_tablas_y_texto(contenido_pdf):
    """Se mantiene por compatibilidad (usado en el histórico de este script);
    no se usa en el flujo principal, que ahora pasa por parsear_boletin()."""
    all_rows, textos = [], []
    with pdfplumber.open(io.BytesIO(contenido_pdf)) as pdf:
        for page in pdf.pages:
            for tabla in page.extract_tables():
                all_rows.extend(tabla)
            textos.append(page.extract_text(x_tolerance=3, y_tolerance=3) or "")
    return all_rows, "\n".join(textos)


def extraer_numero_boletin(texto):
    m = re.search(r"N[°ºo]\s*(\d+/\d+)", texto[:200])
    return m.group(1) if m else ""


def parsear_boletin(contenido_pdf, comisiones_norm):
    """Detecta el formato del boletín (tabular vs. texto libre) y devuelve
    (reuniones, texto_completo)."""
    with pdfplumber.open(io.BytesIO(contenido_pdf)) as pdf:
        texto_completo = "\n".join(
            (p.extract_text(x_tolerance=3, y_tolerance=3) or "") for p in pdf.pages)
        if _es_formato_grafico(pdf):
            return parsear_boletin_grafico(pdf, comisiones_norm)
        if _es_texto_formato_libre(texto_completo):
            reuniones = parsear_reuniones_texto_libre(texto_completo, comisiones_norm)
        else:
            filas = extraer_filas_con_seccion(pdf)
            reuniones = parsear_reuniones_tabular(filas)
    return reuniones, texto_completo


# ─────────────────────────────── Listado de boletines ────────────────────────

def listar_boletines(session):
    """Devuelve [{id, numero, url, nuevo}] ordenados por id descendente.

    Hasta oct/2026 el link terminaba en un id interno (descargarBoletinReunion/2410).
    Desde el boletín 150/26 termina en NÚMERO_AÑO (descargarBoletinReunion/154_2026).
    Para el segundo caso se arma un id sintético creciente y mayor que cualquier id
    viejo: (año-2000)*100000 + número (154_2026 -> 2600154)."""
    resp = session.get(URL_BOLETINES, timeout=45)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    boletines = []
    vistos = set()
    for a in soup.find_all("a", href=re.compile(r"descargarBoletinReunion/(\d+)", re.I)):
        m = re.search(r"descargarBoletinReunion/(\d+)(?:_(\d{4}))?", a["href"], re.I)
        if not m:
            continue
        href = a["href"]
        url = href if href.startswith("http") else BASE_URL + href
        # Número de boletín desde la fila (columna 1)
        numero = ""
        tr = a.find_parent("tr")
        if tr:
            tds = tr.find_all("td")
            if tds:
                numero = tds[0].get_text(strip=True)
        if m.group(2):
            num, anio = int(m.group(1)), int(m.group(2))
            bid = (anio - 2000) * 100000 + num
            numero = f"{num}/{anio % 100:02d}"
            nuevo = True
        else:
            bid = int(m.group(1))
            nuevo = False
        if bid in vistos:
            continue
        vistos.add(bid)
        boletines.append({"id": bid, "numero": numero, "url": url, "nuevo": nuevo})
    boletines.sort(key=lambda b: b["id"], reverse=True)
    return boletines


def descargar_pdf(session, url):
    resp = session.get(url, timeout=90)
    resp.raise_for_status()
    return resp.content


# ─────────────────────────────── Persistencia ────────────────────────────────

def cargar_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception as exc:
            log.warning(f"No se pudo leer {path}: {exc}")
    return default


def guardar_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def clave_reunion(r):
    # No incluye 'tipo': si una corrección de clasificación cambia el tipo de
    # una reunión ya guardada, debe reemplazarla en vez de duplicarla.
    return (r.get("fecha", ""), r.get("hora", ""), tuple(r.get("comisiones", [])))


def marcar_suspendidas(reuniones_acum, reuniones_del_boletin, bid):
    """Si este boletín trae reuniones para una fecha X, es la versión vigente
    de la agenda para esa fecha: cualquier reunión ya acumulada (de un
    boletín anterior) para esa misma fecha que NO reaparezca acá se considera
    dada de baja de la agenda ('suspendida'). Si una reunión suspendida
    reaparece más adelante con la misma clave, el merge normal la reemplaza
    por la versión fresca (sin el flag) — vuelve a quedar vigente sola.
    No distingue "suspendida" de "reprogramada a otra fecha/hora": ambas se
    ven igual desde este único boletín (desaparece de su fecha original)."""
    fechas_bol = {r["fecha"] for r in reuniones_del_boletin if r.get("fecha")}
    if not fechas_bol:
        return 0
    claves_bol = {clave_reunion(r) for r in reuniones_del_boletin}
    marcadas = 0
    for r in reuniones_acum:
        if r.get("fecha") not in fechas_bol:
            continue
        if r.get("boletin_id", 0) >= bid:
            continue
        if clave_reunion(r) in claves_bol:
            continue
        if not r.get("suspendida"):
            r["suspendida"] = True
            marcadas += 1
    return marcadas


# ─────────────────────────────── Main ─────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("scraper_agenda iniciado")

    agenda = cargar_json(AGENDA_JSON, {"reuniones": []})
    if isinstance(agenda, list):
        agenda = {"reuniones": agenda}
    reuniones_acum = agenda.get("reuniones", [])
    indice = {clave_reunion(r): i for i, r in enumerate(reuniones_acum)}

    estado = cargar_json(BOLETINES_PROCESADOS_JSON, {"ultimo_id": 0, "procesados": []})
    ultimo_id = estado.get("ultimo_id", 0)
    procesados = set(estado.get("procesados", []))

    session = requests.Session()
    session.headers.update(HEADERS)
    comisiones_norm = cargar_comisiones_norm()

    # El listado nunca está realmente vacío (siempre hay boletines
    # históricos) — 0 filas es señal de una respuesta degradada
    # (rate-limit/anti-bot intermitente del sitio); vale la pena reintentar.
    boletines = []
    for intento in range(1, 4):
        try:
            boletines = listar_boletines(session)
        except Exception as exc:
            log.error(f"No se pudo listar boletines (intento {intento}/3): {exc}")
            boletines = []
        if boletines:
            break
        if intento < 3:
            log.warning(f"  0 boletines en el intento {intento}/3, probablemente respuesta "
                        f"degradada del sitio — reintentando en 20s...")
            time.sleep(20)

    # Boletines con id NÚMERO_AÑO (desde 150/26): se salteán los que ya estaban
    # cargados con el id viejo (número <= el último boletín ya procesado del año).
    ultimo_num = {}
    for r in reuniones_acum:
        mm = re.match(r"^(\d+)/(\d{2})$", str(r.get("boletin_numero", "")))
        if mm:
            a_ = int(mm.group(2))
            ultimo_num[a_] = max(ultimo_num.get(a_, 0), int(mm.group(1)))

    def _ya_cargado(b):
        if not b.get("nuevo"):
            return False
        n_, a_ = b["id"] % 100000, b["id"] // 100000
        return n_ <= ultimo_num.get(a_, 0)

    nuevos = [b for b in boletines
              if b["id"] > ultimo_id and b["id"] not in procesados and not _ya_cargado(b)]
    log.info(f"  → {len(boletines)} boletines listados, {len(nuevos)} nuevos (último_id={ultimo_id})")

    nuevas_reuniones = 0
    max_id = ultimo_id
    # Procesar de menor a mayor para que el más nuevo prevalezca en duplicados
    for bol in sorted(nuevos, key=lambda b: b["id"]):
        bid = bol["id"]
        log.info(f"  Boletín id={bid} (N° {bol['numero']})")
        try:
            contenido = descargar_pdf(session, bol["url"])
            reuniones, texto = parsear_boletin(contenido, comisiones_norm)
            numero_pdf = bol["numero"] if bol.get("nuevo") else (extraer_numero_boletin(texto) or bol["numero"])
            if bol.get("nuevo") and not reuniones:
                log.warning("    ! el boletín no trajo reuniones; se reintentará en la próxima corrida")
                continue
            log.info(f"    → {len(reuniones)} reunión(es)")
            marcadas = marcar_suspendidas(reuniones_acum, reuniones, bid)
            if marcadas:
                log.info(f"    → {marcadas} reunión(es) marcadas como suspendidas (desaparecieron de la agenda)")
            for r in reuniones:
                r["boletin_id"] = bid
                r["boletin_numero"] = numero_pdf
                k = clave_reunion(r)
                if k in indice:
                    reuniones_acum[indice[k]] = r  # reemplaza por la versión más nueva
                else:
                    indice[k] = len(reuniones_acum)
                    reuniones_acum.append(r)
                    nuevas_reuniones += 1
            procesados.add(bid)
            max_id = max(max_id, bid)
        except Exception as exc:
            log.error(f"    ✗ Error en boletín {bid}: {exc}")
            continue

    agenda["reuniones"] = reuniones_acum
    guardar_json(AGENDA_JSON, agenda)
    guardar_json(BOLETINES_PROCESADOS_JSON,
                 {"ultimo_id": max_id, "procesados": sorted(procesados)})
    log.info(f"  → {nuevas_reuniones} reuniones nuevas; {len(reuniones_acum)} en agenda.json; "
             f"último_id={max_id}")
    log.info("scraper_agenda finalizado.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
