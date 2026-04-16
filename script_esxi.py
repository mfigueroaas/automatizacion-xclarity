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

HOST_CSV_PATH = BASE_DIR / "auditoria_esxi_host.csv"
VM_CSV_PATH = BASE_DIR / "auditoria_esxi_vms.csv"


def _normalize_url(url: str) -> str:
    if url and not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


ESXI_URL = _normalize_url(os.getenv("ESXI_DIRECTO_URL", os.getenv("ESXI_URL", "")))
ESXI_USER = os.getenv("ESXI_DIRECTO_USER", os.getenv("ESXI_USER", ""))
ESXI_PASS = os.getenv("ESXI_DIRECTO_PASS", os.getenv("ESXI_PASS", ""))
ESXI_NAME = os.getenv("ESXI_DIRECTO_NAME", os.getenv("ESXI_NAME", "ESXI"))
ESXI_HOST_NAME = os.getenv("ESXI_HOST_NAME", "SVM003")
ESXI_HEADLESS = os.getenv("ESXI_DIRECTO_HEADLESS", os.getenv("VCENTER_HEADLESS", "true")).lower() in ("1", "true", "yes", "si")
GOOGLE_SHEETS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() in ("1", "true", "yes", "si")
GOOGLE_SHEETS_SPREADSHEET = os.getenv("GOOGLE_SHEETS_SPREADSHEET", "")
GOOGLE_SHEETS_WORKSHEET_ESXI_HOST = os.getenv(
    "GOOGLE_SHEETS_WORKSHEET_ESXI_HOST",
    os.getenv("GOOGLE_SHEETS_WORKSHEET_ESXI", "auditoria_esxi_host"),
)
GOOGLE_SHEETS_WORKSHEET_ESXI_VM = os.getenv(
    "GOOGLE_SHEETS_WORKSHEET_ESXI_VM",
    "auditoria_esxi_vms",
)
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credenciales.json")
SAVE_LOCAL_CSV = os.getenv("SAVE_LOCAL_CSV", "false").lower() in ("1", "true", "yes", "si")
TIMEZONE_NAME = os.getenv("TZ", "America/Santiago")

HOST_FIELD_KEYS = [
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

HOST_SHEET_HEADERS = [
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

VM_FIELD_KEYS = [
    "anio",
    "mes",
    "dia",
    "hora",
    "objetivo",
    "tipo_objetivo",
    "VM Nombre",
    "VM Condición",
    "VM Espacio utilizado",
    "CPU de host",
    "Memoria de host",
    "error",
]

VM_SHEET_HEADERS = [
    "Año",
    "Mes",
    "Día",
    "Hora",
    "Objetivo",
    "Tipo de objetivo",
    "VM Nombre",
    "VM Condición",
    "VM Espacio utilizado",
    "CPU de host",
    "Memoria de host",
    "Error",
]

SEL_LOGIN_USER = "#username"
SEL_LOGIN_PASS = "#password"
SEL_LOGIN_BUTTON = "#submit"
SEL_SEARCH_BAR = "#search-term-ref"
SEL_INVENTORY_TREE = ".scrollableInventoryTree"
SEL_INVENTORY_EXPAND_ICON = ".k-icon.k-plus"
SEL_SUMMARY_LIST = "ul.summary-items-list"
SEL_RESOURCE_METER = "li.resource-meter"
SEL_RESOURCE_PROGRESS = "progress"
SEL_RESOURCE_BOTTOM_LEFT = ".resource-meter-bottom-left-info"
SEL_RESOURCE_BOTTOM_RIGHT = ".resource-meter-bottom-right-info"
SEL_ESXI_METRICS = ".metrics > div"
SEL_VM_LINK = "a:has-text('Máquinas virtuales')"
SEL_VM_TABLE = "#vmList table"

_GSHEET_WS_BY_NAME = {}
_GSHEET_INICIALIZADO = False


def _resolver_path_proyecto(valor_ruta):
    ruta = Path(valor_ruta)
    if ruta.is_absolute():
        return ruta
    return BASE_DIR / ruta


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


def _limpiar_valor_metricas(valor):
    texto = str(valor or "").strip()
    texto = texto.replace("Used:", "").replace("Capacity:", "")
    return re.sub(r"\s+", " ", texto).strip()


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

    return {"porcentaje": porcentaje or "", "usado": usado, "capacidad": capacidad}


def inicializar_csv(ruta_csv, field_keys):
    if not SAVE_LOCAL_CSV:
        return
    if not ruta_csv.exists():
        with ruta_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(field_keys)


def registrar_resultado_csv(ruta_csv, fila, field_keys):
    if not SAVE_LOCAL_CSV:
        return
    with ruta_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow([fila.get(col, "") for col in field_keys])


def aplicar_formato_tabla_sheets(ws, total_cols, etiqueta):
    try:
        spreadsheet = ws.spreadsheet
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "setBasicFilter": {
                    "filter": {"range": {"sheetId": ws.id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": total_cols}}
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
        print(f"[Sheets] Formato aplicado en ESXi ({etiqueta}).")
    except Exception as e:
        print(f"[Sheets] No se pudo aplicar formato visual: {e}")


def _obtener_worksheet_sheets(worksheet_name, headers, field_keys, etiqueta):
    global _GSHEET_WS_BY_NAME, _GSHEET_INICIALIZADO

    if not GOOGLE_SHEETS_ENABLED:
        return None
    cache_key = f"{worksheet_name}"
    if cache_key in _GSHEET_WS_BY_NAME:
        return _GSHEET_WS_BY_NAME[cache_key]

    if gspread is None:
        print("[Sheets] gspread no está instalado. Ejecuta: pip install gspread")
        return None

    if not GOOGLE_SHEETS_SPREADSHEET.strip():
        print("[Sheets] Falta GOOGLE_SHEETS_SPREADSHEET en el .env")
        return None

    try:
        _GSHEET_INICIALIZADO = True
        client = gspread.service_account(filename=str(_resolver_path_proyecto(GOOGLE_SHEETS_CREDENTIALS_FILE)))
        spreadsheet = client.open(GOOGLE_SHEETS_SPREADSHEET)

        try:
            ws = spreadsheet.worksheet(worksheet_name)
        except WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=worksheet_name, rows=2000, cols=max(20, len(field_keys) + 5))

        if ws.row_values(1) != headers:
            ws.update(values=[headers], range_name="A1")

        aplicar_formato_tabla_sheets(ws, len(field_keys), etiqueta)
        _GSHEET_WS_BY_NAME[cache_key] = ws
        print(f"[Sheets] Conectado ({etiqueta}): {GOOGLE_SHEETS_SPREADSHEET} / {worksheet_name}")
        return ws
    except Exception as e:
        print(f"[Sheets] Error al inicializar Google Sheets: {e}")
        return None


def sincronizar_resultado_sheets(worksheet_name, headers, field_keys, etiqueta, fila):
    ws = _obtener_worksheet_sheets(worksheet_name, headers, field_keys, etiqueta)
    if ws is None:
        return False
    try:
        ws.append_row([fila.get(col, "") for col in field_keys], value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[Sheets] Error al enviar fila: {e}")
        return False


def _build_base_row(time_parts, objetivo):
    return {
        "anio": time_parts["anio"],
        "mes": time_parts["mes"],
        "dia": time_parts["dia"],
        "hora": time_parts["hora"],
        "objetivo": objetivo,
        "tipo_objetivo": "esxi",
        "error": "",
    }


def _normalize_header(texto):
    texto = str(texto or "").strip().lower()
    reemplazos = (
        ("á", "a"),
        ("é", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ú", "u"),
        ("ñ", "n"),
    )
    for old, new in reemplazos:
        texto = texto.replace(old, new)
    return re.sub(r"\s+", " ", texto)


def _header_idx(headers, candidates):
    for idx, value in enumerate(headers):
        normalized = _normalize_header(value)
        for cand in candidates:
            if cand in normalized:
                return idx
    return None


async def _extraer_tabla_vms(page):
    raw = await page.evaluate(
                r"""
        () => {
                        const tables = Array.from(document.querySelectorAll('#vmList table, table[role="grid"], table.k-selectable, table'));
                        // Prefer a table that actually contains VM data rows.
                        const table = tables.find(t => t.querySelectorAll('tbody tr[data-uid], tbody tr').length > 0);
            if (!table) {
              return { headers: [], rows: [] };
            }

                        const rowElements = Array.from(table.querySelectorAll('tbody tr[data-uid], tbody tr'));
                        const textOf = (el) => (el && el.innerText ? el.innerText : '').replace(/\s+/g, ' ').trim();

                        const records = rowElements.map(tr => {
                            const cells = Array.from(tr.querySelectorAll('td[role="gridcell"]'));
                            // Mapeo estructural del grid ESXi según HTML observado:
                            // 1 VM, 3 Condición, 4 Espacio, 12 CPU de host, 13 Memoria de host.
                            return {
                                vm_nombre: textOf(cells[1]),
                                vm_condicion: textOf(cells[3]),
                                vm_espacio_utilizado: textOf(cells[4]),
                                vm_cpu_host: textOf(cells[12]),
                                vm_memoria_host: textOf(cells[13]),
                            };
                        }).filter(r => r.vm_nombre || r.vm_condicion || r.vm_espacio_utilizado || r.vm_cpu_host || r.vm_memoria_host);

                        return { headers: [], rows: records };
        }
                """
    )
    return raw.get("headers", []), raw.get("rows", [])


async def _seleccionar_host_esxi(page):
        host_name = str(ESXI_HOST_NAME or ESXI_NAME or "").strip()
        if not host_name:
                return
        try:
                await page.get_by_text(host_name).nth(1).click(timeout=10000)
        except Exception:
                try:
                        await page.get_by_text(host_name).first.click(timeout=10000)
                except Exception:
                        print(f"[ESXi] No se pudo seleccionar explícitamente el host {host_name}. Continuando.")
                        return
        await page.wait_for_timeout(1500)


def _build_esxi_login_url(base_url):
    base = str(base_url or "").rstrip("/")
    if not base:
        return ""
    if "/ui/#/login" in base:
        return base
    return f"{base}/ui/#/login"


async def _extraer_metricas_esxi(page):
    # Prefer DOM-structured extraction (more stable than plain text parsing).
    datos = await page.evaluate(
        r"""
        () => {
          const parseMetric = (selector) => {
            const root = document.querySelector(selector);
            if (!root) return {};

            const allText = (root.innerText || '').replace(/\s+/g, ' ').trim();
            const pctEl = root.querySelector('.progress[aria-valuenow]');
            const pct = pctEl ? String(pctEl.getAttribute('aria-valuenow') || '').trim() : '';

            const usadoMatch = allText.match(/USADO:\s*(.+?)\s*CAPACIDAD:/i);
            const capacidadMatch = allText.match(/CAPACIDAD:\s*(.+)$/i);

            return {
              porcentaje: pct,
              usado: usadoMatch ? usadoMatch[1].trim() : '',
              capacidad: capacidadMatch ? capacidadMatch[1].trim() : '',
            };
          };

          return {
            cpu: parseMetric('.metrics .cpu[title="CPU"]'),
            memory: parseMetric('.metrics .memory[title="MEMORIA"]'),
            storage: parseMetric('.metrics .storage[title="ALMACENAMIENTO"]'),
          };
        }
        """
    )

    # Fallback if any block came incomplete.
    for nombre, selector in (("cpu", ".metrics .cpu[title='CPU']"), ("memory", ".metrics .memory[title='MEMORIA']"), ("storage", ".metrics .storage[title='ALMACENAMIENTO']")):
        actual = datos.get(nombre) or {}
        if actual.get("porcentaje") and actual.get("usado") and actual.get("capacidad"):
            continue
        bloque = page.locator(selector)
        if await bloque.count() == 0:
            datos[nombre] = actual or {}
            continue
        try:
            texto_bloque = await bloque.first.inner_text()
            parsed = _extraer_metricas_desde_texto(texto_bloque)
            datos[nombre] = {
                "porcentaje": actual.get("porcentaje") or parsed.get("porcentaje", ""),
                "usado": actual.get("usado") or parsed.get("usado", ""),
                "capacidad": actual.get("capacidad") or parsed.get("capacidad", ""),
            }
        except Exception:
            datos[nombre] = actual or {}

    return datos


def _looks_like_cpu_value(value):
    text = str(value or "").strip().lower()
    return bool(re.search(r"\b(mhz|ghz)\b", text))


def _looks_like_memory_value(value):
    text = str(value or "").strip().lower()
    return bool(re.search(r"\b(kb|mb|gb|tb)\b", text))


def _corregir_valores_vm_cpu_mem(vm_cpu, vm_mem):
    # Defensive correction when columns shift in UI rendering.
    if _looks_like_cpu_value(vm_mem) and not _looks_like_cpu_value(vm_cpu):
        return vm_mem, vm_cpu
    return vm_cpu, vm_mem


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

    filas_resultado = []

    fila_host = _build_base_row(time_parts, nombre_objetivo)
    fila_host.update(
        {
            "cpu_porcentaje": cpu.get("porcentaje", ""),
            "cpu_usado": cpu.get("usado", ""),
            "cpu_capacidad": cpu.get("capacidad", ""),
            "memory_porcentaje": memory.get("porcentaje", ""),
            "memory_usado": memory.get("usado", ""),
            "memory_capacidad": memory.get("capacidad", ""),
            "storage_porcentaje": storage.get("porcentaje", ""),
            "storage_usado": storage.get("usado", ""),
            "storage_capacidad": storage.get("capacidad", ""),
        }
    )
    filas_resultado.append({"kind": "host", "data": fila_host})

    print("[ESXi] Navegando a la vista de Máquinas virtuales...")
    try:
        await page.locator(".esx-main-content").first.click(timeout=5000)
    except Exception:
        pass
    try:
        await page.get_by_role("link", name="Máquinas virtuales").click(timeout=10000)
    except Exception:
        await page.locator(SEL_VM_LINK).first.click(timeout=10000)

    await page.wait_for_load_state("networkidle", timeout=30000)
    await page.wait_for_selector(f"{SEL_VM_TABLE}, #vmList", timeout=30000)
    await page.wait_for_timeout(3000)

    _headers, rows = await _extraer_tabla_vms(page)
    if not rows:
        print("[ESXi] No se encontraron filas en la tabla de máquinas virtuales.")
    else:
        for row in rows:
            fila_vm = _build_base_row(time_parts, nombre_objetivo)
            fila_vm["VM Nombre"] = row.get("vm_nombre", "")
            fila_vm["VM Condición"] = row.get("vm_condicion", "")
            fila_vm["VM Espacio utilizado"] = row.get("vm_espacio_utilizado", "")
            vm_cpu, vm_mem = _corregir_valores_vm_cpu_mem(row.get("vm_cpu_host", ""), row.get("vm_memoria_host", ""))
            fila_vm["CPU de host"] = vm_cpu
            fila_vm["Memoria de host"] = vm_mem
            filas_resultado.append({"kind": "vm", "data": fila_vm})

        print(f"[ESXi] Máquinas virtuales detectadas: {len(rows)}")

    for item in filas_resultado:
        kind = item["kind"]
        fila = item["data"]
        if kind == "host":
            registrar_resultado_csv(HOST_CSV_PATH, fila, HOST_FIELD_KEYS)
            sincronizar_resultado_sheets(
                GOOGLE_SHEETS_WORKSHEET_ESXI_HOST,
                HOST_SHEET_HEADERS,
                HOST_FIELD_KEYS,
                "host",
                fila,
            )
        else:
            registrar_resultado_csv(VM_CSV_PATH, fila, VM_FIELD_KEYS)
            sincronizar_resultado_sheets(
                GOOGLE_SHEETS_WORKSHEET_ESXI_VM,
                VM_SHEET_HEADERS,
                VM_FIELD_KEYS,
                "vms",
                fila,
            )

    return filas_resultado


async def main():
    if GOOGLE_SHEETS_ENABLED:
        _obtener_worksheet_sheets(
            GOOGLE_SHEETS_WORKSHEET_ESXI_HOST,
            HOST_SHEET_HEADERS,
            HOST_FIELD_KEYS,
            "host",
        )
        _obtener_worksheet_sheets(
            GOOGLE_SHEETS_WORKSHEET_ESXI_VM,
            VM_SHEET_HEADERS,
            VM_FIELD_KEYS,
            "vms",
        )

    if SAVE_LOCAL_CSV:
        inicializar_csv(HOST_CSV_PATH, HOST_FIELD_KEYS)
        inicializar_csv(VM_CSV_PATH, VM_FIELD_KEYS)

    if not ESXI_URL.strip() or not ESXI_USER.strip() or not ESXI_PASS.strip():
        print("[ESXi] Faltan ESXI_DIRECTO_URL, ESXI_DIRECTO_USER o ESXI_DIRECTO_PASS en el .env")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=ESXI_HEADLESS)
        context = await browser.new_context(ignore_https_errors=True, locale="es-ES", timezone_id="America/Santiago")
        page = await context.new_page()
        try:
            esxi_login_url = _build_esxi_login_url(ESXI_URL)
            await page.goto(esxi_login_url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(2000)

            try:
                await page.get_by_role("textbox", name="Nombre de usuario").fill(ESXI_USER, timeout=10000)
            except Exception:
                await page.locator(SEL_LOGIN_USER).first.fill(ESXI_USER, timeout=10000)

            try:
                await page.get_by_role("textbox", name="Contraseña").fill(ESXI_PASS, timeout=10000)
            except Exception:
                await page.locator(SEL_LOGIN_PASS).first.fill(ESXI_PASS, timeout=10000)

            try:
                await page.locator("[data-test-id='login-action-button']").first.click(timeout=10000)
            except Exception:
                try:
                    await page.get_by_role("button", name="Iniciar sesión").click(timeout=10000)
                except Exception:
                    try:
                        await page.get_by_role("button", name="Iniciar").click(timeout=10000)
                    except Exception:
                        await page.locator(SEL_LOGIN_BUTTON).first.click(timeout=10000)

            await _seleccionar_host_esxi(page)

            try:
                await page.wait_for_selector(SEL_ESXI_METRICS, timeout=30000)
            except Exception:
                await page.get_by_text("CPU LIBRE").first.wait_for(timeout=30000)
            await page.wait_for_timeout(3000)
            await extraer_datos_esxi(page, ESXI_NAME or ESXI_URL)
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[ESXi] Ejecucion interrumpida por el usuario.")