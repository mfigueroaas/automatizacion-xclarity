import asyncio
import csv
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image
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

CSV_PATH = BASE_DIR / "auditoria_idrac.csv"


def _normalize_url(url: str) -> str:
    if url and not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


IDRAC_URL = _normalize_url(os.getenv("IDRAC_URL", ""))
IDRAC_USER = os.getenv("IDRAC_USER", "")
IDRAC_PASS = os.getenv("IDRAC_PASS", "")
IDRAC_NAME = os.getenv("IDRAC_NAME", "iDRAC")
IDRAC_HEADLESS = os.getenv("IDRAC_HEADLESS", "true").lower() in ("1", "true", "yes", "si")
GOOGLE_SHEETS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() in ("1", "true", "yes", "si")
GOOGLE_SHEETS_SPREADSHEET = os.getenv("GOOGLE_SHEETS_SPREADSHEET", "")
GOOGLE_SHEETS_WORKSHEET = os.getenv("GOOGLE_SHEETS_WORKSHEET_IDRAC", os.getenv("GOOGLE_SHEETS_WORKSHEET", "auditoria_idrac"))
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credenciales.json")
SAVE_LOCAL_CSV = os.getenv("SAVE_LOCAL_CSV", "false").lower() in ("1", "true", "yes", "si")
TIMEZONE_NAME = os.getenv("TZ", "America/Santiago")

IDRAC_LOGIN_USER_SELECTOR = os.getenv("IDRAC_LOGIN_USER_SELECTOR", "#user")
IDRAC_LOGIN_PASS_SELECTOR = os.getenv("IDRAC_LOGIN_PASS_SELECTOR", "#password")
IDRAC_LOGIN_BUTTON_SELECTOR = os.getenv("IDRAC_LOGIN_BUTTON_SELECTOR", "")
IDRAC_CONDITION_PANEL_SELECTOR = os.getenv("IDRAC_CONDITION_PANEL_SELECTOR", "#HtmlTable_sdrverInfo")
IDRAC_CONSOLE_PREVIEW_SELECTOR = os.getenv("IDRAC_CONSOLE_PREVIEW_SELECTOR", "#console_preview_img_id")
IDRAC_THERMAL_MENU_SELECTOR = os.getenv("IDRAC_THERMAL_MENU_SELECTOR", "a:has-text('Alimentación / Térmico')")
IDRAC_TEMPERATURE_TAB_SELECTOR = os.getenv("IDRAC_TEMPERATURE_TAB_SELECTOR", "#li_T10")
IDRAC_TEMP_TABLE_SELECTOR = os.getenv("IDRAC_TEMP_TABLE_SELECTOR", "#temp_probe_table")

IDRAC_FRAME_PRIORITY = ("da", "treelist", "blank", "virtual", "title", "snb", "lsnb", "hidden_frame")
IDRAC_CONDITION_PANEL_CANDIDATES = [
    "#HtmlTable_sdrverInfo",
    "#sdrverInfo",
    "#serverinfo",
    "table[id*='sdrverInfo']",
    "table:has(.status_ok)",
    "table:has-text('CPU')",
]
IDRAC_CONSOLE_PREVIEW_CANDIDATES = [
    "#console_preview_img_id",
    "img[id*='console']",
    "img[id*='preview']",
    "img[src*='preview']",
]
IDRAC_THERMAL_MENU_CANDIDATES = [
    "a:has-text('Alimentación / Térmico')",
    "a:has-text('Power / Thermal')",
    "a:has-text('Térmico')",
]
IDRAC_TEMPERATURE_TAB_CANDIDATES = [
    "#li_T10",
    "a:has-text('Temperaturas')",
    "a:has-text('Temperatures')",
]
IDRAC_TEMP_TABLE_CANDIDATES = [
    "#temp_probe_table",
    "table[id*='temp']",
    "table:has-text('CPU')",
    "table:has-text('Temperaturas')",
]

FIELD_KEYS = [
    "anio",
    "mes",
    "dia",
    "hora",
    "servidor",
    "ip",
    "health_anomalias",
    "temp_cpu1",
    "temp_cpu2",
    "estado_consola",
    "estado_general",
    "detalle_alerta",
    "error",
]

SHEET_HEADERS = [
    "Año",
    "Mes",
    "Día",
    "Hora",
    "Servidor",
    "IP",
    "Anomalías de salud",
    "Temperatura CPU1 (°C)",
    "Temperatura CPU2 (°C)",
    "Estado de consola",
    "Estado general",
    "Detalle de alerta",
    "Error",
]

_GSHEET_WS = None
_GSHEET_INICIALIZADO = False


def _resolver_path_proyecto(valor_ruta):
    ruta = Path(valor_ruta)
    return ruta if ruta.is_absolute() else BASE_DIR / ruta


def _obtener_zona_horaria_local():
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        return ZoneInfo("America/Santiago")


def _build_time_parts(dt_obj):
    return {"anio": dt_obj.strftime("%Y"), "mes": dt_obj.strftime("%m"), "dia": dt_obj.strftime("%d"), "hora": dt_obj.strftime("%H:%M")}


def _to_int_or_none(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _build_selector_candidates(primary, fallbacks):
    candidates = []
    for selector in [primary, *fallbacks]:
        selector = str(selector or "").strip()
        if selector and selector not in candidates:
            candidates.append(selector)
    return candidates


async def _fill_first_available(page, selectors, value, timeout=10000):
    last_error = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.fill(value, timeout=timeout)
            return selector
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"No se pudo completar el campo usando selectores: {selectors}. Ultimo error: {last_error}")


def _obtener_frames_prioritarios(page):
    frames = []
    vistos = set()
    for frame_name in IDRAC_FRAME_PRIORITY:
        frame = page.frame(name=frame_name)
        if frame is not None and id(frame) not in vistos:
            frames.append(frame)
            vistos.add(id(frame))
    for frame in page.frames:
        if id(frame) not in vistos:
            frames.append(frame)
            vistos.add(id(frame))
    return frames


async def _buscar_locator_visible(page, selectors, frame_names=None, timeout=10000):
    frames = _obtener_frames_prioritarios(page)
    allowed = None
    if frame_names:
        allowed = {"" if name is None else str(name) for name in frame_names}

    # First pass: preferred frame names.
    if allowed is not None:
        for frame in frames:
            frame_name = frame.name or ""
            if frame_name not in allowed:
                continue
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    await locator.wait_for(state="visible", timeout=timeout)
                    return frame, selector, locator
                except Exception:
                    continue

    # Second pass: any frame (fallback for dynamic/unnamed frames).
    for frame in frames:
        for selector in selectors:
            try:
                locator = frame.locator(selector).first
                await locator.wait_for(state="visible", timeout=timeout)
                return frame, selector, locator
            except Exception:
                continue
    return None, None, None


async def _buscar_frame_con_selector(page, selector, frame_names=None, timeout=10000):
    frame, _selector, locator = await _buscar_locator_visible(page, [selector], frame_names=frame_names, timeout=timeout)
    return frame, locator


async def _invocar_sensor_page(page, sensor_name):
    for frame in _obtener_frames_prioritarios(page):
        try:
            invoked = await frame.evaluate(
                """
                (sensor) => {
                    if (typeof SensorPage === 'function') {
                        SensorPage(sensor);
                        return true;
                    }
                    return false;
                }
                """,
                sensor_name,
            )
            if invoked:
                return True
        except Exception:
            continue
    return False


def inicializar_csv(ruta_csv):
    if SAVE_LOCAL_CSV and not ruta_csv.exists():
        with ruta_csv.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter=";").writerow(FIELD_KEYS)


def registrar_resultado_csv(ruta_csv, fila):
    if not SAVE_LOCAL_CSV:
        return
    with ruta_csv.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter=";").writerow([fila.get(col, "") for col in FIELD_KEYS])


def aplicar_formato_tabla_sheets(ws):
    try:
        total_cols = len(FIELD_KEYS)
        requests = [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": ws.id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": total_cols}}}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": total_cols}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.12, "green": 0.35, "blue": 0.62}, "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}, "horizontalAlignment": "CENTER"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
            {"autoResizeDimensions": {"dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": total_cols}}},
        ]
        try:
            ws.spreadsheet.batch_update({"requests": [{"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}} for _ in range(10)]})
        except Exception:
            pass
        ws.spreadsheet.batch_update({"requests": requests})
        print("[Sheets] Formato aplicado en iDRAC.")
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
        return ws
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


def _black_screen_analysis(ruta_imagen):
    try:
        img = Image.open(ruta_imagen).convert("RGB")
        img.thumbnail((150, 150))
        px_data = img.tobytes()
        pixels = [(px_data[i], px_data[i + 1], px_data[i + 2]) for i in range(0, len(px_data), 3)]
        negros = sum(1 for r, g, b in pixels if r < 35 and g < 35 and b < 35)
        porcentaje_negro = (negros / len(pixels)) * 100 if pixels else 0
        return porcentaje_negro >= 60, porcentaje_negro
    except Exception as e:
        print(f"Error analizando imagen {ruta_imagen}: {e}")
        return False, 0


def _componentes_salud_relevantes(detalles_salud):
    componentes = set()
    for detalle in detalles_salud:
        txt = detalle.lower()
        if any(x in txt for x in ["disk", "drive", "storage", "raid", "ssd", "hdd", "nvme", "disco"]):
            componentes.add("disco")
        if any(x in txt for x in ["power", "psu", "fuente", "supply"]):
            componentes.add("fuente de alimentacion")
        if any(x in txt for x in ["fan", "vent", "cool"]):
            componentes.add("ventilacion")
    return sorted(componentes)


def construir_detalle_alerta(errores_salud, temp_cpu1, temp_cpu2, estado_consola, error_detalle, detalles_salud=None, porcentaje_negro=None):
    motivos = []
    detalles_salud = detalles_salud or []
    if str(error_detalle).strip():
        motivos.append(f"Error de ejecucion: {error_detalle}")
    e_salud = _to_int_or_none(errores_salud)
    t1 = _to_int_or_none(temp_cpu1)
    t2 = _to_int_or_none(temp_cpu2)
    if e_salud is None:
        motivos.append("No se pudo leer Condition Panel")
    elif e_salud > 0:
        componentes = _componentes_salud_relevantes(detalles_salud)
        motivos.append(f"Panel con {e_salud} categorias no verdes" + (f" en: {', '.join(componentes)}" if componentes else ""))
    if t1 is None and t2 is None:
        motivos.append("Sin datos de temperatura en CPU1/CPU2")
    else:
        if t1 is not None and not (30 <= t1 <= 50):
            motivos.append(f"CPU1 fuera de rango: {t1}C")
        if t2 is not None and not (30 <= t2 <= 50):
            motivos.append(f"CPU2 fuera de rango: {t2}C")
    if estado_consola != "NEGRA_OK":
        motivos.append("Consola no negra" if porcentaje_negro is None else f"Consola no negra: {porcentaje_negro:.1f}%")
    return "SIN_ALERTAS" if not motivos else " | ".join(motivos)


def calcular_estado_general(errores_salud, temp_cpu1, temp_cpu2, estado_consola, error_detalle, detalles_salud=None, porcentaje_negro=None):
    return "OK" if construir_detalle_alerta(errores_salud, temp_cpu1, temp_cpu2, estado_consola, error_detalle, detalles_salud=detalles_salud, porcentaje_negro=porcentaje_negro) == "SIN_ALERTAS" else "ALERTA"


async def _login_idrac(page):
    print("[iDRAC] Iniciando sesión...")
    await page.goto(IDRAC_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(1500)

    user_selectors = _build_selector_candidates(IDRAC_LOGIN_USER_SELECTOR, ["#user", "input[name='user']", "input[type='text']"])
    pass_selectors = _build_selector_candidates(IDRAC_LOGIN_PASS_SELECTOR, ["#password", "input[name='password']", "input[type='password']"])

    await _fill_first_available(page, user_selectors, IDRAC_USER, timeout=10000)
    await _fill_first_available(page, pass_selectors, IDRAC_PASS, timeout=10000)

    if IDRAC_LOGIN_BUTTON_SELECTOR.strip():
        try:
            await page.locator(IDRAC_LOGIN_BUTTON_SELECTOR).click(timeout=10000)
        except Exception:
            await page.keyboard.press("Enter")
    else:
        try:
            await page.locator("#btnOK").click(timeout=10000)
        except Exception:
            try:
                await page.get_by_text("Enviar", exact=True).click(timeout=10000)
            except Exception:
                await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle", timeout=60000)
    await page.wait_for_timeout(2000)


async def _extraer_condicion_servidor(page):
    frame = None
    locator = None
    result = {"total": 0, "alertas": [], "categorias": []}
    
    if IDRAC_CONDITION_PANEL_SELECTOR.strip():
        frame, locator = await _buscar_frame_con_selector(page, IDRAC_CONDITION_PANEL_SELECTOR, frame_names=("da", "blank", "virtual", None), timeout=8000)
    
    if frame is None or locator is None:
        frame, _selector, locator = await _buscar_locator_visible(page, IDRAC_CONDITION_PANEL_CANDIDATES, frame_names=("da", "blank", "virtual", None), timeout=8000)

    if locator is not None:
        result = await locator.evaluate(
            r"""
            (panel) => {
                const rows = Array.from(panel.querySelectorAll('tr'));
                const categorias = [];
                const alertas = [];
                for (const tr of rows) {
                    const cells = Array.from(tr.querySelectorAll('td'));
                    if (cells.length < 2) continue;
                    for (let i = 1; i < cells.length; i += 2) {
                        const label = (cells[i] && cells[i].innerText ? cells[i].innerText : '').replace(/\s+/g, ' ').trim();
                        if (!label) continue;
                        const iconCell = cells[i - 1];
                        const ok = !!(iconCell && iconCell.querySelector('.status_ok'));
                        categorias.push({ label, ok });
                        if (!ok) alertas.push(label);
                    }
                }
                return { total: categorias.length, alertas, categorias };
            }
            """
        )
    print(f"[iDRAC] Categorías evaluadas: {result.get('total', 0)}")
    if result.get("alertas"):
        print(f"[iDRAC] No verdes: {', '.join(result.get('alertas', []))}")
    return result


async def _capturar_preview_consola(page):
    frame, _selector, locator = await _buscar_locator_visible(page, _build_selector_candidates(IDRAC_CONSOLE_PREVIEW_SELECTOR, IDRAC_CONSOLE_PREVIEW_CANDIDATES), frame_names=("da", "virtual", "blank", None), timeout=30000)
    if locator is None:
        print("[iDRAC] No se encontró la vista previa de consola.")
        return "NO_NEGRA_ALERTA", 0
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        ruta = Path(tmp.name)
    try:
        await locator.screenshot(path=str(ruta))
        es_negra, porcentaje_negro = _black_screen_analysis(ruta)
        return ("NEGRA_OK" if es_negra else "NO_NEGRA_ALERTA"), porcentaje_negro
    finally:
        try:
            ruta.unlink(missing_ok=True)
        except Exception:
            pass


async def _extraer_temperaturas(page):
    rows = []
    tab_locator = None

    try:
        _, _, thermal_locator = await _buscar_locator_visible(
            page,
            _build_selector_candidates(IDRAC_THERMAL_MENU_SELECTOR, IDRAC_THERMAL_MENU_CANDIDATES),
            frame_names=("treelist", "da", None),
            timeout=10000,
        )
        if thermal_locator is not None:
            await thermal_locator.click(timeout=10000)
    except Exception:
        pass

    await page.wait_for_timeout(1000)

    try:
        _, _, tab_locator = await _buscar_locator_visible(
            page,
            _build_selector_candidates(IDRAC_TEMPERATURE_TAB_SELECTOR, IDRAC_TEMPERATURE_TAB_CANDIDATES),
            frame_names=("da", "treelist", None),
            timeout=10000,
        )
        if tab_locator is not None:
            await tab_locator.click(timeout=10000)
    except Exception:
        pass

    if tab_locator is None:
        if await _invocar_sensor_page(page, "temperatures"):
            await page.wait_for_timeout(1500)

    await page.wait_for_timeout(1000)

    frame_tabla = None
    locator_tabla = None
    if IDRAC_TEMP_TABLE_SELECTOR.strip():
        frame_tabla, locator_tabla = await _buscar_frame_con_selector(
            page,
            IDRAC_TEMP_TABLE_SELECTOR,
            frame_names=("da", "blank", "virtual", None),
            timeout=8000,
        )
    if frame_tabla is None or locator_tabla is None:
        frame_tabla, _selector, locator_tabla = await _buscar_locator_visible(
            page,
            IDRAC_TEMP_TABLE_CANDIDATES,
            frame_names=("da", "blank", "virtual", None),
            timeout=8000,
        )

    if frame_tabla is None:
        print("[iDRAC] No se encontró la tabla de temperaturas.")
        return "", "", []

    rows = await locator_tabla.evaluate(
        r"""
        (table) => {
            const bodyRows = Array.from(table.querySelectorAll('tbody tr'));
            if (!bodyRows.length) return [];
            return bodyRows.map(tr => {
                const cells = Array.from(tr.querySelectorAll('td'));
                const getCellText = (idx) => (cells[idx] && cells[idx].innerText ? cells[idx].innerText : '').replace(/\s+/g, ' ').trim();
                return { status: getCellText(1), probe_name: getCellText(2), reading: getCellText(3) };
            }).filter(row => row.probe_name || row.reading);
        }
        """
    )
    temp_cpu1 = ""
    temp_cpu2 = ""
    detalles = []
    for row in rows:
        probe = row.get("probe_name", "")
        reading = row.get("reading", "")
        status = row.get("status", "")
        detalles.append({"probe_name": probe, "reading": reading, "status": status})
        print(f"[iDRAC] Temperatura: {probe} -> {reading} ({status})")
        probe_lower = probe.lower()
        if not temp_cpu1 and "cpu1" in probe_lower:
            temp_cpu1 = reading
        if not temp_cpu2 and "cpu2" in probe_lower:
            temp_cpu2 = reading
    return temp_cpu1, temp_cpu2, detalles


async def main():
    if GOOGLE_SHEETS_ENABLED:
        _obtener_worksheet_sheets()
    if SAVE_LOCAL_CSV:
        inicializar_csv(CSV_PATH)
    if not IDRAC_URL.strip() or not IDRAC_USER.strip() or not IDRAC_PASS.strip():
        print("[iDRAC] Faltan IDRAC_URL, IDRAC_USER o IDRAC_PASS en el .env")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(ignore_https_errors=True, locale="es-ES", timezone_id="America/Santiago")
        page = await context.new_page()
        try:
            await _login_idrac(page)
            health = await _extraer_condicion_servidor(page)
            estado_consola, porcentaje_negro = await _capturar_preview_consola(page)
            temp_cpu1, temp_cpu2, detalles_temp = await _extraer_temperaturas(page)

            tz_local = _obtener_zona_horaria_local()
            time_parts = _build_time_parts(datetime.now(tz_local))

            detalle_alerta = construir_detalle_alerta(health.get("alertas", []), temp_cpu1, temp_cpu2, estado_consola, "", detalles_salud=health.get("categorias", []), porcentaje_negro=porcentaje_negro)
            estado_general = calcular_estado_general(health.get("alertas", []), temp_cpu1, temp_cpu2, estado_consola, "", detalles_salud=health.get("categorias", []), porcentaje_negro=porcentaje_negro)

            fila_resultado = {
                "anio": time_parts["anio"],
                "mes": time_parts["mes"],
                "dia": time_parts["dia"],
                "hora": time_parts["hora"],
                "servidor": IDRAC_NAME,
                "ip": IDRAC_URL,
                "health_anomalias": len(health.get("alertas", [])),
                "temp_cpu1": temp_cpu1,
                "temp_cpu2": temp_cpu2,
                "estado_consola": estado_consola,
                "estado_general": estado_general,
                "detalle_alerta": detalle_alerta,
                "error": "",
            }

            if SAVE_LOCAL_CSV:
                registrar_resultado_csv(CSV_PATH, fila_resultado)
            sincronizar_resultado_sheets(fila_resultado)
            print(f"[iDRAC] Temperaturas detectadas: {len(detalles_temp)}")
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[iDRAC] Ejecucion interrumpida por el usuario.")