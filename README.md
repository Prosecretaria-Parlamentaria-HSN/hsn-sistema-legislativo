# Sistema Legislativo HSN

Página web de la **Prosecretaría Parlamentaria del Honorable Senado de la Nación Argentina** que reúne en un solo lugar los proyectos, comisiones, agenda, órdenes del día, sanciones y pliegos del Senado. Se actualiza sola dos veces por día.

**Web:** https://prosecretaria-parlamentaria-hsn.github.io/hsn-sistema-legislativo/

---

## Qué incluye la web

| Sección | Qué muestra |
|---|---|
| **Inicio** | Resumen y accesos a cada sección. |
| **Proyectos** | Buscador con filtros (año, tipo, bloque, provincia, origen, comisión, autor, fechas, "Con OD"), tabla dinámica y estadísticas. Cada proyecto lleva sus marcas: Orden del Día, preferencia solicitada, sancionado, enviado a Diputados, dado cuenta / pendiente de dar cuenta. **Exporta a Excel y PDF** según los filtros activos. |
| **Senadores** | Fichas de los senadores con bloque, provincia y datos de contacto. |
| **Comisiones** | Comisiones permanentes con integrantes, cupos y vacantes, historial de cambios, proyectos en trámite y próxima reunión. Se puede descargar en PDF. |
| **Agenda** | Reuniones de comisiones, con temario enlazado a cada expediente y marca de reuniones suspendidas. |
| **Ayuda Memoria** | Órdenes del Día con dictamen: tipo de proyecto, autor/es, contenido, firmantes, disidencias y minoría. Filtros por comisión, autor, bloque y tipo. **Exporta a Excel y PDF.** Incluye la solapa Preferencias. |
| **Sanciones HSN** | Leyes sancionadas, acuerdos aprobados y otros, tomados de los Boletines de Novedades. |
| **Pliegos judiciales** | Seguimiento de pliegos del Poder Judicial. |
| **Herramientas para sesión** | Utilidades para usar durante la sesión (por ejemplo, el tablero de votación). |

### Exportar a Excel y PDF
- Disponible en **Proyectos** y en **Ayuda Memoria**.
- Respeta los filtros que estén aplicados; el encabezado del archivo indica cuáles.
- El Excel sale con formato institucional (encabezado azul, filas alternadas, links a los expedientes y filtros en las columnas).
- El PDF sale en A4 horizontal, con encabezado, paginado y links.
- Si se exportan más de 1500 proyectos a PDF, la página pide confirmación (conviene filtrar antes).
- Cuando un expediente no tiene autor individual, se muestra el origen: *Poder Ejecutivo*, *Cámara de Diputados*, *Nota de un particular* o *Senado (sin autor individual)*.

### Tipografía y diseño
- Tipografía **Montserrat** en toda la página y en los PDF. Si falta `data/fonts_montserrat.json`, usa Poppins como respaldo.
- Montserrat no tiene las flechas (→) ni los tildes (✓); el navegador dibuja esos símbolos con otra fuente.
- Azul institucional `#1B5EA2`. Colores propios por tipo de proyecto y por bloque.

---

## Cómo funciona

```
Scrapers (senado.gob.ar)  →  data/*.json  →  generar_web.py  →  index.html  →  generar_diseno_nuevo.py  →  web publicada
```

1. **Scrapers** (`scripts/scraper_*.py`) descargan datos del sitio del Senado y los guardan en `data/`.
2. **`scripts/generar_web.py`** lee todos los datos y arma un `index.html` autosuficiente: los datos van embebidos, no hace falta servidor y funciona abriendo el archivo.
3. **`scripts/generar_diseno_nuevo.py`** aplica el diseño actual (barra lateral, botones "Borrar filtros", "Volver", etc.) sobre ese `index.html`. El resultado es lo que se publica.
4. **GitHub Pages** publica `index.html` desde la rama `main`.

Todo el front es JavaScript puro. Las únicas librerías externas se cargan por CDN: SheetJS, jsPDF y ExcelJS (esta última recién al exportar a Excel).

---

## Actualización automática

El workflow **"Actualizar datos legislativos"** (`.github/workflows/actualizar.yml`):
- Corre todos los días a las **09:00** y a las **22:00** (hora de Argentina) y se puede lanzar a mano desde *Actions → Run workflow*.
- Ejecuta los scrapers (los incrementales siguen aunque alguno falle), regenera el `index.html` y guarda los cambios en el repo.
- Las corridas programadas de GitHub a veces se demoran unos minutos u horas.

El workflow **"Publicar correcciones de agenda y comisiones"** (`agenda_overrides.yml`) regenera la web cuando se modifica `data/agenda_overrides.json` o `data/dpp_cambios.json` (correcciones manuales).

---

## Estructura del repositorio

```
.github/workflows/    Automatizaciones (actualizar.yml, agenda_overrides.yml)
scripts/              Scrapers y generadores
  generar_web.py          Arma el index.html
  generar_diseno_nuevo.py Aplica el diseño actual
  scraper_proyectos.py    Proyectos y su estado (archivo, caducidad, sanción, dar cuenta)
  scraper_od.py           Órdenes del Día
  scraper_sanciones.py    Boletines de Novedades
  scraper_agenda.py       Reuniones de comisiones
  scraper_comisiones.py   Integrantes de comisiones
  scraper_dpp.py          Cambios de integrantes
  scraper_senadores.py    Senadores
  scraper_pliegos.py      Pliegos judiciales
  scraper_noticias.py     Noticias del Senado
  generar_ayuda_memoria.py / parse_ayuda_memoria.py / actualizar_ayuda_memoria.py
data/                 Bases en JSON, fotos y fuentes
  proyectos.json, od.json, sanciones.json, acuerdos.json, agenda.json,
  comisiones.json, senadores.json, pliegos_*.json, ayuda_memoria*.json
  fonts_montserrat.json, fonts_poppins.json   Fuentes para los PDF
docs/                 Documentación técnica (ESTADO_PROYECTO.md y otros)
editor.html           Editor para correcciones manuales de agenda y comisiones
index.html            Página generada (no se edita a mano)
requirements.txt      Dependencias de Python
```

---

## Datos principales

- **`proyectos.json`**: un registro por expediente (número, año, tipo, extracto, autores, bloques, comisiones, fecha, origen, link, y su estado: sancionado, archivado, caducado). El detalle de campos está en `docs/ESTADO_PROYECTO.md`.
- **`od.json`**: Órdenes del Día con sus expedientes y el PDF.
- **`sanciones.json`**: ítems de los Boletines de Novedades (ley, acuerdo, preferencia, etc.).
- **`ayuda_memoria*.json`**: dictámenes con autor, firmantes, disidencias y minoría.

## Uso local

```
pip install -r requirements.txt
python scripts/generar_web.py
python scripts/generar_diseno_nuevo.py
```
Después se abre `index.html` en el navegador. En Windows, usar `py` en lugar de `python`.

## Cómo subir cambios a mano (desde la web de GitHub)
1. Entrar a la carpeta correspondiente (`scripts/` o `data/`).
2. *Add file → Upload files* y arrastrar el archivo (reemplaza al anterior).
3. *Commit changes*.
4. Si se subió un `.yml`, subirlo **al final**.
5. Ir a *Actions → Actualizar datos legislativos → Run workflow* para regenerar la web.

## Limitaciones conocidas
- Un expediente puede tardar varios días en actualizar su estado: cada corrida revisa un lote acotado de expedientes abiertos.
- La Ayuda Memoria necesita depurarse después de cada Boletín de Novedades (ver `docs/ESTADO_PROYECTO.md`, sección de pendientes).
- Las citas combinadas del boletín tipo "S-252 y 308/26" no se reconocen automáticamente.
- El sitio del Senado puede bloquear o demorar pedidos; si un scraper falla, la web sigue con el último dato guardado.

## Documentación adicional
- `docs/ESTADO_PROYECTO.md`: arquitectura, esquema de datos, convenciones y pendientes.
- `docs/PROMPT_MIGRACION_PROYECTOS.md` e informes de trazabilidad: historial de la migración de proyectos.

---
*Prosecretaría Parlamentaria · Honorable Senado de la Nación Argentina*
