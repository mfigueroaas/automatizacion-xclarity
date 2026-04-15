import asyncio
import ast
import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from zoneinfo import ZoneInfo

try:
    import gspread
    from gspread.exceptions import WorksheetNotFound
except Exception:
    gspread = None
    WorksheetNotFound = Exception

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

CSV_PATH = BASE_DIR / "auditoria_vcenter.csv"

def _normalize_url(url: str) -> str:
    """Agrega protocolo https:// si la URL no lo tiene."""
    if url and not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url

VCENTER_URL = _normalize_url(os.getenv("VCENTER_URL", ""))
VCENTER_USER = os.getenv("VCENTER_USER", "")
VCENTER_PASS = os.getenv("VCENTER_PASS", "")
VCENTER_HEADLESS = os.getenv("VCENTER_HEADLESS", "true").lower() in ("1", "true", "yes", "si")
VCENTER_OBJECTIVES_RAW = os.getenv("VCENTER_OBJECTIVES_JSON", "[]")

ESXI_URL = _normalize_url(os.getenv("ESXI_URL", ""))
ESXI_USER = os.getenv("ESXI_USER", "")
ESXI_PASS = os.getenv("ESXI_PASS", "")
ESXI_NAME = os.getenv("ESXI_NAME", "ESXI")

GOOGLE_SHEETS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() in ("1", "true", "yes", "si")
GOOGLE_SHEETS_SPREADSHEET = os.getenv("GOOGLE_SHEETS_SPREADSHEET", "")
GOOGLE_SHEETS_WORKSHEET = os.getenv("GOOGLE_SHEETS_WORKSHEET_VCENTER", os.getenv("GOOGLE_SHEETS_WORKSHEET", "auditoria_vcenter"))
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credenciales.json")
SAVE_LOCAL_CSV = os.getenv("SAVE_LOCAL_CSV", "false").lower() in ("1", "true", "yes", "si")
TIMEZONE_NAME = os.getenv("TZ", "America/Santiago")

FIELD_KEYS = [
    "anio",
    "mes",
    "dia",
    "hora",
    "objetivo",
    "tipo_objetivo",
    "cpu_porcentaje",
    "cpu_usado",
    "cpu_capacidad",
    "memory_porcentaje",
    "memory_usado",
    "memory_capacidad",
    "storage_porcentaje",
    "storage_usado",
    "storage_capacidad",
    "error",
]

SHEET_HEADERS = [
    "Año",
    "Mes",
    "Día",
    "Hora",
    "Objetivo",
    "Tipo de objetivo",
    "CPU %",
    "CPU usado",
    "CPU capacidad",
    "Memoria %",
    "Memoria usada",
    "Memoria capacidad",
    "Storage %",
    "Storage usado",
    "Storage capacidad",
    "Error",
]

SEL_VCENTER_CLIENT_BUTTON = 'a[href="/ui/"]'  # Botón "Iniciar vSphere Client (HTML5)"
SEL_LOGIN_USER = "#username"
SEL_LOGIN_PASS = "#password"
SEL_LOGIN_BUTTON = "#submit"
SEL_SEARCH_BAR = "#search-term-ref"  # Barra de búsqueda principal
SEL_INVENTORY_ITEM = ".scrollableInventoryTree span.k-in"
SEL_INVENTORY_TREE = ".scrollableInventoryTree"
SEL_INVENTORY_EXPAND_ICON = ".k-icon.k-plus"
SEL_SUMMARY_LIST = "ul.summary-items-list"
SEL_RESOURCE_METER = "li.resource-meter"
SEL_RESOURCE_PROGRESS = "progress"
SEL_RESOURCE_BOTTOM_LEFT = ".resource-meter-bottom-left-info"
SEL_RESOURCE_BOTTOM_RIGHT = ".resource-meter-bottom-right-info"

SEL_ESXI_METRICS = ".metrics > div"
SEL_ESXI_CPU = ".metrics .cpu[title='CPU']"
SEL_ESXI_MEMORY = ".metrics .memory[title='MEMORIA']"
SEL_ESXI_STORAGE = ".metrics .storage[title='ALMACENAMIENTO']"
SEL_ESXI_LOGIN_BUTTON = "[data-test-id='login-action-button']"

_GSHEET_WS = None
_GSHEET_INICIALIZADO = False


def _resolver_path_proyecto(valor_ruta):
    ruta = Path(valor_ruta)
    if ruta.is_absolute():
        return ruta
    return BASE_DIR / ruta


def _parsear_lista_desde_env(valor):
    texto = str(valor or "").strip()
    if not texto:
        return []
    if (texto.startswith("'") and texto.endswith("'")) or (texto.startswith('"') and texto.endswith('"')):
        texto = texto[1:-1].strip()
    try:
        data = json.loads(texto)
    except json.JSONDecodeError:
        data = ast.literal_eval(texto)
    if not isinstance(data, list):
        raise ValueError("VCENTER_OBJECTIVES_JSON debe ser una lista")
    return [str(item).strip() for item in data if str(item).strip()]


def _obtener_zona_horaria_local():
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        if TIMEZONE_NAME != "America/Santiago":
            print(f"[Warn] TZ invalida: {TIMEZONE_NAME}. Usando America/Santiago.")
        return ZoneInfo("America/Santiago")


def _build_time_parts(dt_obj):
    return {
        "anio": dt_obj.strftime("%Y"),
        "mes": dt_obj.strftime("%m"),
        "dia": dt_obj.strftime("%d"),
        "hora": dt_obj.strftime("%H:%M"),
    }


def _to_float_or_none(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _limpiar_texto_metodo(texto, prefijos):
    resultado = str(texto or "").strip()
    for prefijo in prefijos:
        if resultado.lower().startswith(prefijo.lower()):
            resultado = resultado[len(prefijo):].strip()
            break
    return resultado


def _parsear_lista_objetivos():
    try:
        return _parsear_lista_desde_env(VCENTER_OBJECTIVES_RAW)
    except Exception as e:
        print(f"[vCenter] Error leyendo VCENTER_OBJECTIVES_JSON: {e}")
        return []


def _normalizar_tipo_objetivo(nombre_objetivo):
    return "cluster" if str(nombre_objetivo).lower().startswith("cluster") else "host"


def _limpiar_valor_metricas(valor):
    texto = str(valor or "").strip()
    texto = texto.replace("Used:", "").replace("Capacity:", "")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _extraer_metricas_desde_texto(texto_bloque):
    texto = re.sub(r"\s+", " ", str(texto_bloque or "")).strip()
    porcentaje = None
    usado = ""
    capacidad = ""

    match_porcentaje = re.search(r"(\d+(?:[.,]\d+)?)%", texto)
    if match_porcentaje:
        porcentaje = match_porcentaje.group(1).replace(",", ".")

    match_usado = re.search(r"USADO:\s*(.+?)\s*(?:CAPACIDAD:|$)", texto, re.IGNORECASE)
    if match_usado:
        usado = _limpiar_valor_metricas(match_usado.group(1))

    match_capacidad = re.search(r"CAPACIDAD:\s*(.+)$", texto, re.IGNORECASE)
    if match_capacidad:
        capacidad = _limpiar_valor_metricas(match_capacidad.group(1))

    return {
        "porcentaje": porcentaje or "",
        "usado": usado,
        "capacidad": capacidad,
    }


def inicializar_csv(ruta_csv):
    if not SAVE_LOCAL_CSV:
        return
    if not ruta_csv.exists():
        with ruta_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(FIELD_KEYS)


def registrar_resultado_csv(ruta_csv, fila):
    if not SAVE_LOCAL_CSV:
        return
    with ruta_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow([fila.get(col, "") for col in FIELD_KEYS])


def aplicar_formato_tabla_sheets(ws):
    try:
        spreadsheet = ws.spreadsheet
        total_cols = len(FIELD_KEYS)
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 0,
                            "startColumnIndex": 0,
                            "endColumnIndex": total_cols,
                        }
                    }
                }
            },
            {
                "repeatCell": {
                    "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": total_cols},
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.12, "green": 0.35, "blue": 0.62},
                            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
                            "horizontalAlignment": "CENTER",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            },
            {"autoResizeDimensions": {"dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": total_cols}}},
        ]
        try:
            spreadsheet.batch_update({"requests": [{"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}} for _ in range(10)]})
        except Exception:
            pass
        ws.spreadsheet.batch_update({"requests": requests})
        print("[Sheets] Formato aplicado en vCenter.")
    except Exception as e:
        print(f"[Sheets] No se pudo aplicar formato visual: {e}")


def _obtener_worksheet_sheets():
    global _GSHEET_WS, _GSHEET_INICIALIZADO

    if not GOOGLE_SHEETS_ENABLED:
        return None
    if _GSHEET_WS is not None:
        return _GSHEET_WS
    if _GSHEET_INICIALIZADO:
        return None

    _GSHEET_INICIALIZADO = True

    if gspread is None:
        print("[Sheets] gspread no está instalado. Ejecuta: pip install gspread")
        return None

    if not GOOGLE_SHEETS_SPREADSHEET.strip():
        print("[Sheets] Falta GOOGLE_SHEETS_SPREADSHEET en el .env")
        return None

    try:
        client = gspread.service_account(filename=str(_resolver_path_proyecto(GOOGLE_SHEETS_CREDENTIALS_FILE)))
        spreadsheet = client.open(GOOGLE_SHEETS_SPREADSHEET)

        try:
            ws = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET)
        except WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=GOOGLE_SHEETS_WORKSHEET, rows=2000, cols=max(20, len(FIELD_KEYS) + 5))

        if ws.row_values(1) != SHEET_HEADERS:
            ws.update(values=[SHEET_HEADERS], range_name="A1")

        aplicar_formato_tabla_sheets(ws)
        _GSHEET_WS = ws
        print(f"[Sheets] Conectado: {GOOGLE_SHEETS_SPREADSHEET} / {GOOGLE_SHEETS_WORKSHEET}")
        return _GSHEET_WS
    except Exception as e:
        print(f"[Sheets] Error al inicializar Google Sheets: {e}")
        return None


def sincronizar_resultado_sheets(fila):
    ws = _obtener_worksheet_sheets()
    if ws is None:
        return False
    try:
        ws.append_row([fila.get(col, "") for col in FIELD_KEYS], value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[Sheets] Error al enviar fila: {e}")
        return False


async def _expandir_arbol_inventario(page):
    """Expande nodos visibles del árbol para que los hosts queden disponibles."""
    try:
        tree = page.locator(SEL_INVENTORY_TREE)
        if await tree.count() == 0:
            return

        toggles = tree.locator(SEL_INVENTORY_EXPAND_ICON)
        total = await toggles.count()
        for idx in range(min(total, 12)):
            try:
                await toggles.nth(idx).click(timeout=1500)
            except Exception:
                continue
        await page.wait_for_timeout(1000)
    except Exception:
        pass


async def _seleccionar_objetivo_vcenter(page, nombre_objetivo):
    """Selecciona objetivo por nombre en el árbol de inventario."""
    print(f"[vCenter] Seleccionando objetivo: {nombre_objetivo}")

    tree = page.locator(SEL_INVENTORY_TREE)
    await tree.wait_for(state="visible", timeout=20000)

    ultimo_error = None
    for _ in range(4):
        try:
            await tree.get_by_text(nombre_objetivo, exact=False).first.click(timeout=7000)
            await page.wait_for_timeout(1500)
            await page.wait_for_selector(f"{SEL_SUMMARY_LIST}, {SEL_RESOURCE_METER}", timeout=30000)
            return
        except Exception as e:
            ultimo_error = e
            # Expandir más nodos y reintentar (flujo real grabado con .k-icon.k-plus)
            toggles = tree.locator(SEL_INVENTORY_EXPAND_ICON)
            if await toggles.count() > 0:
                await toggles.first.click(timeout=3000)
                await page.wait_for_timeout(900)

    raise RuntimeError(f"No se pudo seleccionar el objetivo '{nombre_objetivo}' en el árbol: {ultimo_error}")


def _extraer_bloque_metricas(page, nombre_bloque):
    bloque = page.locator(SEL_RESOURCE_METER).filter(has_text=nombre_bloque)
    return bloque


async def _extraer_metricas_bloque(page, nombre_bloque):
    datos = {}
    bloque = _extraer_bloque_metricas(page, nombre_bloque)
    if await bloque.count() == 0:
        return datos

    try:
        progreso = await bloque.locator(SEL_RESOURCE_PROGRESS).get_attribute("value")
        texto_usado = await bloque.locator(SEL_RESOURCE_BOTTOM_LEFT).inner_text()
        texto_capacidad = await bloque.locator(SEL_RESOURCE_BOTTOM_RIGHT).inner_text()
        datos = {
            "porcentaje": progreso or "",
            "usado": _limpiar_valor_metricas(texto_usado),
            "capacidad": _limpiar_valor_metricas(texto_capacidad),
        }
    except Exception:
        return {}

    return datos


async def extraer_datos_vcenter(page, nombre_objetivo):
    print(f"\n--- Analizando: {nombre_objetivo} ---")
    await _seleccionar_objetivo_vcenter(page, nombre_objetivo)

    cpu = await _extraer_metricas_bloque(page, "CPU")
    memory = await _extraer_metricas_bloque(page, "Memory")
    storage = await _extraer_metricas_bloque(page, "Storage")

    print(f"CPU: {cpu.get('porcentaje', 'N/A')}% ({cpu.get('usado', 'N/A')} / {cpu.get('capacidad', 'N/A')})")
    print(f"RAM: {memory.get('porcentaje', 'N/A')}% ({memory.get('usado', 'N/A')} / {memory.get('capacidad', 'N/A')})")
    print(f"Disco: {storage.get('porcentaje', 'N/A')}% ({storage.get('usado', 'N/A')} / {storage.get('capacidad', 'N/A')})")

    tz_local = _obtener_zona_horaria_local()
    now_dt = datetime.now(tz_local)
    time_parts = _build_time_parts(now_dt)

    fila_resultado = {
        "anio": time_parts["anio"],
        "mes": time_parts["mes"],
        "dia": time_parts["dia"],
        "hora": time_parts["hora"],
        "objetivo": nombre_objetivo,
        "tipo_objetivo": _normalizar_tipo_objetivo(nombre_objetivo),
        "cpu_porcentaje": cpu.get("porcentaje", ""),
        "cpu_usado": cpu.get("usado", ""),
        "cpu_capacidad": cpu.get("capacidad", ""),
        "memory_porcentaje": memory.get("porcentaje", ""),
        "memory_usado": memory.get("usado", ""),
        "memory_capacidad": memory.get("capacidad", ""),
        "storage_porcentaje": storage.get("porcentaje", ""),
        "storage_usado": storage.get("usado", ""),
        "storage_capacidad": storage.get("capacidad", ""),
        "error": "",
    }

    registrar_resultado_csv(CSV_PATH, fila_resultado)
    sincronizar_resultado_sheets(fila_resultado)
    return fila_resultado


async def _extraer_metricas_esxi(page):
    datos = {}

    for nombre, selector in (
        ("cpu", SEL_ESXI_CPU),
        ("memory", SEL_ESXI_MEMORY),
        ("storage", SEL_ESXI_STORAGE),
    ):
        bloque = page.locator(selector)
        if await bloque.count() == 0:
            datos[nombre] = {}
            continue

        try:
            texto_bloque = await bloque.first.inner_text()
            datos[nombre] = _extraer_metricas_desde_texto(texto_bloque)
        except Exception:
            datos[nombre] = {}

    return datos


async def extraer_datos_esxi(page, nombre_objetivo):
    print(f"\n--- Analizando ESXi: {nombre_objetivo} ---")

    metrics = await _extraer_metricas_esxi(page)
    cpu = metrics.get("cpu", {})
    memory = metrics.get("memory", {})
    storage = metrics.get("storage", {})

    print(f"CPU: {cpu.get('porcentaje', 'N/A')}% ({cpu.get('usado', 'N/A')} / {cpu.get('capacidad', 'N/A')})")
    print(f"RAM: {memory.get('porcentaje', 'N/A')}% ({memory.get('usado', 'N/A')} / {memory.get('capacidad', 'N/A')})")
    print(f"Disco: {storage.get('porcentaje', 'N/A')}% ({storage.get('usado', 'N/A')} / {storage.get('capacidad', 'N/A')})")

    tz_local = _obtener_zona_horaria_local()
    now_dt = datetime.now(tz_local)
    time_parts = _build_time_parts(now_dt)

    fila_resultado = {
        "anio": time_parts["anio"],
        "mes": time_parts["mes"],
        "dia": time_parts["dia"],
        "hora": time_parts["hora"],
        "objetivo": nombre_objetivo,
        "tipo_objetivo": "esxi",
        "cpu_porcentaje": cpu.get("porcentaje", ""),
        "cpu_usado": cpu.get("usado", ""),
        "cpu_capacidad": cpu.get("capacidad", ""),
        "memory_porcentaje": memory.get("porcentaje", ""),
        "memory_usado": memory.get("usado", ""),
        "memory_capacidad": memory.get("capacidad", ""),
        "storage_porcentaje": storage.get("porcentaje", ""),
        "storage_usado": storage.get("usado", ""),
        "storage_capacidad": storage.get("capacidad", ""),
        "error": "",
    }

    registrar_resultado_csv(CSV_PATH, fila_resultado)
    sincronizar_resultado_sheets(fila_resultado)
    return fila_resultado


def _build_esxi_login_url(base_url):
    base = str(base_url or "").rstrip("/")
    if not base:
        return ""
    if "/ui/#/login" in base:
        return base
    return f"{base}/ui/#/login"


async def main():
    if GOOGLE_SHEETS_ENABLED:
        _obtener_worksheet_sheets()

    if SAVE_LOCAL_CSV:
        inicializar_csv(CSV_PATH)

    async with async_playwright() as pw:
        if VCENTER_URL.strip() and VCENTER_USER.strip() and VCENTER_PASS.strip():
            objetivos = _parsear_lista_objetivos()
            if not objetivos:
                print("[vCenter] No hay objetivos configurados. Revisa VCENTER_OBJECTIVES_JSON en el .env")
            else:
                browser = await pw.chromium.launch(headless=VCENTER_HEADLESS)
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                try:
                    await page.goto(VCENTER_URL, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
                    
                    # Hacer clic en el botón "Iniciar vSphere Client (HTML5)"
                    print("[vCenter] Iniciando vSphere Client...")
                    await page.locator(SEL_VCENTER_CLIENT_BUTTON).click(timeout=10000)
                    
                    # Esperar la redirección lenta del SSO de VMware (le damos hasta 60 segundos)
                    print("[vCenter] Esperando redirección al login SSO (puede tardar)...")
                    await page.wait_for_selector(SEL_LOGIN_USER, state="visible", timeout=60000)
                    
                    # Login
                    print("[vCenter] Pantalla de login detectada. Ingresando credenciales...")
                    await page.locator(SEL_LOGIN_USER).first.fill(VCENTER_USER)
                    await page.locator(SEL_LOGIN_PASS).first.fill(VCENTER_PASS)
                    await page.locator(SEL_LOGIN_BUTTON).first.click(timeout=10000)

                    await page.wait_for_load_state("networkidle", timeout=30000)
                    await page.wait_for_selector(f"{SEL_INVENTORY_TREE}, {SEL_SEARCH_BAR}", timeout=30000)
                    await _expandir_arbol_inventario(page)

                    for objetivo in objetivos:
                        await extraer_datos_vcenter(page, objetivo)
                finally:
                    await browser.close()

        if ESXI_URL.strip() and ESXI_USER.strip() and ESXI_PASS.strip():
            browser = await pw.chromium.launch(headless=VCENTER_HEADLESS)
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            try:
                esxi_login_url = _build_esxi_login_url(ESXI_URL)
                await page.goto(esxi_login_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                # Flujo principal grabado con inspector
                try:
                    await page.get_by_role("textbox", name="Nombre de usuario").fill(ESXI_USER, timeout=10000)
                except Exception:
                    await page.locator(SEL_LOGIN_USER).first.fill(ESXI_USER, timeout=10000)

                try:
                    await page.get_by_role("textbox", name="Contraseña").fill(ESXI_PASS, timeout=10000)
                except Exception:
                    await page.locator(SEL_LOGIN_PASS).first.fill(ESXI_PASS, timeout=10000)

                try:
                    await page.locator(SEL_ESXI_LOGIN_BUTTON).first.click(timeout=10000)
                except Exception:
                    try:
                        await page.get_by_role("button", name="Iniciar").click(timeout=10000)
                    except Exception:
                        await page.locator(SEL_LOGIN_BUTTON).first.click(timeout=10000)

                try:
                    await page.wait_for_selector(SEL_ESXI_METRICS, timeout=30000)
                except Exception:
                    await page.get_by_text("CPU LIBRE").first.wait_for(timeout=30000)
                await page.wait_for_timeout(1500)
                await extraer_datos_esxi(page, ESXI_NAME or ESXI_URL)
            finally:
                await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[vCenter] Ejecucion interrumpida por el usuario.")