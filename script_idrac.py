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

IDRAC_CONDITION_PANEL_CANDIDATES = [
    "#HtmlTable_sdrverInfo",
    "#summaryTable",
    "#systemSummary",
    "#sdrverInfo",
    "#serverinfo",
    "table[id*='sdrverInfo']",
    "table:has-text('Salud')",
    "table:has-text('Health')",
]
IDRAC_CONSOLE_PREVIEW_CANDIDATES = [
    "#console_preview_img_id",
    "#vKvmPreview",
    "img[id*='vKvm']",
    "img[id*='console']",
    "img[id*='preview']",
    "img[src*='preview']",
]
IDRAC_THERMAL_MENU_CANDIDATES = [
    "a:has-text('Alimentación / Térmico')",
    "a:has-text('Power / Thermal')",
    "a:has-text('Thermal')",
    "a:has-text('Alimentación')",
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
    "table:has-text('System')",
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
        if isinstance(detalle, dict):
            txt = str(detalle.get("label", "")).lower()
        else:
            txt = str(detalle).lower()
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
    da_frame = page.frame_locator('frame[name="da"]')
    result = {"total": 0, "alertas": [], "categorias": []}
    selectors = _build_selector_candidates(IDRAC_CONDITION_PANEL_SELECTOR, IDRAC_CONDITION_PANEL_CANDIDATES)

    for selector in selectors:
        try:
            locator = da_frame.locator(selector).first
            await locator.wait_for(state="visible", timeout=7000)
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
                            const html = (iconCell && iconCell.innerHTML ? iconCell.innerHTML : '').toLowerCase();
                            const text = (iconCell && iconCell.innerText ? iconCell.innerText : '').toLowerCase();
                            const ok = !!(
                                iconCell && (
                                    iconCell.querySelector('.status_ok, .Status_OK') ||
                                    (html.includes('ok') && html.includes('img')) ||
                                    text.includes('ok')
                                )
                            );
                            categorias.push({ label, ok });
                            if (!ok) alertas.push(label);
                        }
                    }
                    return { total: categorias.length, alertas, categorias };
                }
                """
            )
            if isinstance(result, dict) and result.get("total", 0) > 0:
                break
        except Exception:
            continue

    print(f"[iDRAC] Categorías evaluadas: {result.get('total', 0)}")
    if result.get("alertas"):
        print(f"[iDRAC] No verdes: {', '.join(result.get('alertas', []))}")
    elif result.get("total", 0) == 0:
        print("[iDRAC] No se encontró el panel de condición.")
    return result


async def _capturar_preview_consola(page):
    da_frame = page.frame_locator('frame[name="da"]')
    selectors = _build_selector_candidates(IDRAC_CONSOLE_PREVIEW_SELECTOR, IDRAC_CONSOLE_PREVIEW_CANDIDATES)

    for selector in selectors:
        try:
            locator = da_frame.locator(selector).first
            await locator.wait_for(state="visible", timeout=12000)

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
        except Exception:
            continue

    print("[iDRAC] No se encontró la vista previa de consola.")
    return "NO_NEGRA_ALERTA", 0


async def _extraer_temperaturas(page):
    rows = []
    treelist = page.frame_locator('frame[name="treelist"]')
    da_frame = page.frame_locator('frame[name="da"]')

    print("[iDRAC] Navegando a sección térmica...")
    try:
        menu_selectors = _build_selector_candidates(IDRAC_THERMAL_MENU_SELECTOR, IDRAC_THERMAL_MENU_CANDIDATES)
        for selector in menu_selectors:
            try:
                thermal_menu = treelist.locator(selector).first
                await thermal_menu.wait_for(state="visible", timeout=5000)
                await thermal_menu.click(timeout=5000)
                break
            except Exception:
                continue
    except Exception:
        print("[iDRAC] No se pudo clickear menú Térmico (puede que ya estemos ahí).")

    await page.wait_for_timeout(1500)

    try:
        tab_selectors = _build_selector_candidates(IDRAC_TEMPERATURE_TAB_SELECTOR, IDRAC_TEMPERATURE_TAB_CANDIDATES)
        for selector in tab_selectors:
            try:
                temp_tab = da_frame.locator(selector).first
                await temp_tab.wait_for(state="visible", timeout=5000)
                await temp_tab.click(timeout=5000)
                break
            except Exception:
                continue
    except Exception:
        print("[iDRAC] No se pudo clickear pestaña Temperaturas.")

    await page.wait_for_timeout(1500)

    table_selectors = _build_selector_candidates(IDRAC_TEMP_TABLE_SELECTOR, IDRAC_TEMP_TABLE_CANDIDATES)
    for selector in table_selectors:
        try:
            table = da_frame.locator(selector).first
            await table.wait_for(state="visible", timeout=6000)
            rows = await table.evaluate(
                r"""
                (table) => {
                    return Array.from(table.querySelectorAll('tbody tr, tr')).map(tr =>
                        Array.from(tr.querySelectorAll('td')).map(td => td.innerText.replace(/\s+/g, ' ').trim()).join(' | ')
                    ).filter(text => text);
                }
                """
            )
            if len(rows) > 1:
                break
        except Exception:
            continue

    if not rows:
        print("[iDRAC] No se encontró la tabla de temperaturas.")
        return "", "", []

    temp_cpu1 = ""
    temp_cpu2 = ""
    detalles = []

    for row_text in rows:
        row_lower = row_text.lower()
        if "cpu" not in row_lower and "system" not in row_lower:
            continue

        match = re.search(r"(?<!\d)(\d{2})(?=\s*[cC°]|\s*\|)", row_text)
        reading = match.group(1) if match else ""
        if not reading:
            continue

        detalles.append({"probe_name": row_text, "reading": reading, "status": ""})
        print(f"[iDRAC] Sensor detectado: {reading}°C en -> {row_text}")

        if not temp_cpu1 and ("cpu1" in row_lower or "cpu 1" in row_lower):
            temp_cpu1 = reading
        elif not temp_cpu2 and ("cpu2" in row_lower or "cpu 2" in row_lower):
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
            error_detalle = ""
            health = {"total": 0, "alertas": [], "categorias": []}
            estado_consola, porcentaje_negro = "NO_NEGRA_ALERTA", 0
            temp_cpu1, temp_cpu2, detalles_temp = "", "", []

            await _login_idrac(page)
            try:
                health = await _extraer_condicion_servidor(page)
            except Exception as e:
                error_detalle = f"Condition panel: {e}"

            try:
                estado_consola, porcentaje_negro = await _capturar_preview_consola(page)
            except Exception as e:
                pref = f"{error_detalle} | " if error_detalle else ""
                error_detalle = f"{pref}Preview consola: {e}"

            try:
                temp_cpu1, temp_cpu2, detalles_temp = await _extraer_temperaturas(page)
            except Exception as e:
                pref = f"{error_detalle} | " if error_detalle else ""
                error_detalle = f"{pref}Temperaturas: {e}"

            tz_local = _obtener_zona_horaria_local()
            time_parts = _build_time_parts(datetime.now(tz_local))

            health_anomalias = len(health.get("alertas", []))

            detalle_alerta = construir_detalle_alerta(health_anomalias, temp_cpu1, temp_cpu2, estado_consola, error_detalle, detalles_salud=health.get("categorias", []), porcentaje_negro=porcentaje_negro)
            estado_general = calcular_estado_general(health_anomalias, temp_cpu1, temp_cpu2, estado_consola, error_detalle, detalles_salud=health.get("categorias", []), porcentaje_negro=porcentaje_negro)

            fila_resultado = {
                "anio": time_parts["anio"],
                "mes": time_parts["mes"],
                "dia": time_parts["dia"],
                "hora": time_parts["hora"],
                "servidor": IDRAC_NAME,
                "ip": IDRAC_URL,
                "health_anomalias": health_anomalias,
                "temp_cpu1": temp_cpu1,
                "temp_cpu2": temp_cpu2,
                "estado_consola": estado_consola,
                "estado_general": estado_general,
                "detalle_alerta": detalle_alerta,
                "error": error_detalle,
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