import asyncio
import re
import os
import json
import ast
import csv
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from PIL import Image
from zoneinfo import ZoneInfo

try:
    import gspread
    from gspread.exceptions import WorksheetNotFound
except Exception:
    gspread = None
    WorksheetNotFound = Exception

BASE_DIR = Path(__file__).resolve().parent


def _resolver_path_proyecto(valor_ruta):
    ruta = Path(valor_ruta)
    if ruta.is_absolute():
        return ruta
    return BASE_DIR / ruta


# Cargar variables de entorno desde el .env ubicado junto al script.
load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

def _parsear_servidores_desde_env(valor):
    texto = str(valor or "").strip()
    if not texto:
        raise ValueError("SERVIDORES_JSON esta vacio")

    # Acepta formato con comillas envolventes: '[]' o "[]".
    if (texto.startswith("'") and texto.endswith("'")) or (texto.startswith('"') and texto.endswith('"')):
        texto = texto[1:-1].strip()

    try:
        data = json.loads(texto)
    except json.JSONDecodeError:
        # Fallback para formatos estilo Python con comillas simples.
        data = ast.literal_eval(texto)

    if not isinstance(data, list):
        raise ValueError("SERVIDORES_JSON debe ser una lista")

    return data


# Leer la lista de servidores y convertirla de JSON a una lista de Python.
try:
    SERVIDORES = _parsear_servidores_desde_env(os.getenv("SERVIDORES_JSON", ""))
except Exception as e:
    print(f"Error leyendo SERVIDORES_JSON en .env: {e}")
    SERVIDORES = []

CSV_PATH = BASE_DIR / "auditoria_xclarity.csv"
GSHEETS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() in ("1", "true", "yes", "si")
GSHEETS_SPREADSHEET = os.getenv("GOOGLE_SHEETS_SPREADSHEET", "")
GSHEETS_WORKSHEET = os.getenv("GOOGLE_SHEETS_WORKSHEET_XCLARITY", os.getenv("GOOGLE_SHEETS_WORKSHEET", "auditoria"))
GSHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credenciales.json")
SAVE_LOCAL_CSV = os.getenv("SAVE_LOCAL_CSV", "false").lower() in ("1", "true", "yes", "si")
TIMEZONE_NAME = os.getenv("TZ", "America/Santiago")
FIELD_KEYS = [
    "anio",
    "mes",
    "dia",
    "hora",
    "servidor",
    "ip",
    "health_anomalias",
    "eventos_activos",
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
    "Eventos activos",
    "Temperatura CPU1 (°C)",
    "Temperatura CPU2 (°C)",
    "Estado de consola",
    "Estado general",
    "Detalle de alerta",
    "Error",
]

SEL_LOGIN_USER = "User name"
SEL_LOGIN_PASS = "Password"
SEL_HEALTH_TAB = ".el-tabs__item:has-text('Health Summary')"
SEL_HEALTH_ANOMALIES = ".hwMsgType.msgWarning,.hwMsgType.msgCritical"
SEL_EVENTS_TAB = ".el-tabs__item"
SEL_EVENTS_TAB_TEXT = "Active System Events"
SEL_CPU_CONTAINER = ".homeCpuTemp"
SEL_CPU_VALUE = ".cpu-temperature-data"
SEL_CAPTURE_BUTTON = "Capture Screen"

_GSHEET_WS = None
_GSHEET_INICIALIZADO = False


def _obtener_zona_horaria_local():
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        if TIMEZONE_NAME != "America/Santiago":
            print(f"[Warn] TZ invalida: {TIMEZONE_NAME}. Usando America/Santiago.")
        return ZoneInfo("America/Santiago")


def aplicar_formato_tabla_sheets(ws):
    """Aplica formato visual de tabla para facilitar filtros y analisis."""
    try:
        spreadsheet = ws.spreadsheet
        total_cols = len(FIELD_KEYS)
        # Columna estado_general para formato condicional (A=0)
        col_estado_general = FIELD_KEYS.index("estado_general")

        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": ws.id,
                        "gridProperties": {
                            "frozenRowCount": 1,
                        },
                    },
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
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": total_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 0.12,
                                "green": 0.35,
                                "blue": 0.62,
                            },
                            "textFormat": {
                                "foregroundColor": {
                                    "red": 1,
                                    "green": 1,
                                    "blue": 1,
                                },
                                "bold": True,
                            },
                            "horizontalAlignment": "CENTER",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "NUMBER",
                                "pattern": "0",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "startColumnIndex": 1,
                        "endColumnIndex": 3,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "NUMBER",
                                "pattern": "00",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "startColumnIndex": 3,
                        "endColumnIndex": 4,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "TIME",
                                "pattern": "HH:mm",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": ws.id,
                                "startRowIndex": 1,
                                "startColumnIndex": col_estado_general,
                                "endColumnIndex": col_estado_general + 1,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "ALERTA"}],
                            },
                            "format": {
                                "backgroundColor": {
                                    "red": 0.92,
                                    "green": 0.40,
                                    "blue": 0.40,
                                },
                                "textFormat": {"bold": True},
                            },
                        },
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": ws.id,
                                "startRowIndex": 1,
                                "startColumnIndex": col_estado_general,
                                "endColumnIndex": col_estado_general + 1,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "OK"}],
                            },
                            "format": {
                                "backgroundColor": {
                                    "red": 0.73,
                                    "green": 0.89,
                                    "blue": 0.74,
                                }
                            },
                        },
                    },
                    "index": 0,
                }
            },
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": total_cols,
                    }
                }
            },
        ]

        # Limpiar reglas previas para evitar duplicados en cada corrida.
        spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "deleteConditionalFormatRule": {
                            "sheetId": ws.id,
                            "index": 0,
                        }
                    }
                    for _ in range(10)
                ]
            }
        )
    except Exception:
        # Si no hay reglas para borrar, continuamos con el formato.
        pass

    try:
        ws.spreadsheet.batch_update({"requests": requests})
        print("[Sheets] Formato de tabla aplicado (filtros, encabezado y colores).")
    except Exception as e:
        print(f"[Sheets] No se pudo aplicar formato visual: {e}")


def _to_int_or_none(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _build_time_parts(dt_obj):
    return {
        "anio": dt_obj.strftime("%Y"),
        "mes": dt_obj.strftime("%m"),
        "dia": dt_obj.strftime("%d"),
        "hora": dt_obj.strftime("%H:%M"),
    }


def _time_parts_from_timestamp_text(timestamp_text):
    ts = str(timestamp_text or "").strip()
    if not ts:
        return {
            "anio": "",
            "mes": "",
            "dia": "",
            "hora": "",
        }

    dt_obj = None
    formatos = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formatos:
        try:
            dt_obj = datetime.strptime(ts, fmt)
            break
        except Exception:
            continue

    if dt_obj is None:
        try:
            dt_obj = datetime.fromisoformat(ts)
        except Exception:
            return {
                "anio": "",
                "mes": "",
                "dia": "",
                "hora": "",
            }

    return _build_time_parts(dt_obj)


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


def construir_detalle_alerta(
    errores_salud,
    num_events,
    temp_cpu1,
    temp_cpu2,
    estado_consola,
    error_detalle,
    detalles_salud=None,
    porcentaje_negro=None,
):
    motivos = []
    detalles_salud = detalles_salud or []

    if str(error_detalle).strip():
        motivos.append(f"Error de ejecucion: {error_detalle}")

    e_salud = _to_int_or_none(errores_salud)
    e_events = _to_int_or_none(num_events)
    t1 = _to_int_or_none(temp_cpu1)
    t2 = _to_int_or_none(temp_cpu2)

    if e_salud is None:
        motivos.append("No se pudo leer Health Summary")
    elif e_salud > 0:
        componentes = _componentes_salud_relevantes(detalles_salud)
        if componentes:
            motivos.append(f"Health Summary con {e_salud} anomalias en: {', '.join(componentes)}")
        else:
            motivos.append(f"Health Summary con {e_salud} anomalias")

    if e_events is None:
        motivos.append("No se pudo leer Active System Events")
    elif e_events > 0:
        motivos.append(f"Eventos activos detectados: {e_events}")

    # Regla pedida: con al menos una CPU reportada, no hay alerta por ausencia de la otra.
    if t1 is None and t2 is None:
        motivos.append("Sin datos de temperatura en CPU1/CPU2")
    else:
        if t1 is not None and not (30 <= t1 <= 50):
            motivos.append(f"CPU1 fuera de rango: {t1}C")
        if t2 is not None and not (30 <= t2 <= 50):
            motivos.append(f"CPU2 fuera de rango: {t2}C")

    if estado_consola != "NEGRA_OK":
        if porcentaje_negro is None:
            motivos.append("Consola no valida: pantalla no negra")
        else:
            motivos.append(f"Consola no negra: {porcentaje_negro:.1f}% de negro")

    if not motivos:
        return "SIN_ALERTAS"
    return " | ".join(motivos)


def calcular_estado_general(errores_salud, num_events, temp_cpu1, temp_cpu2, estado_consola, error_detalle, detalles_salud=None, porcentaje_negro=None):
    detalle = construir_detalle_alerta(
        errores_salud,
        num_events,
        temp_cpu1,
        temp_cpu2,
        estado_consola,
        error_detalle,
        detalles_salud=detalles_salud,
        porcentaje_negro=porcentaje_negro,
    )
    return "OK" if detalle == "SIN_ALERTAS" else "ALERTA"


def inicializar_csv(ruta_csv):
    if not SAVE_LOCAL_CSV:
        return

    if not ruta_csv.exists():
        with ruta_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(FIELD_KEYS)
        return

    # Migrar formato anterior (con coma y/o columna captura) al formato actual
    with ruta_csv.open("r", newline="", encoding="utf-8") as f:
        primera_linea = f.readline()
        delimiter = ';' if ';' in primera_linea else ','
        f.seek(0)
        reader = csv.DictReader(f, delimiter=delimiter)
        rows = list(reader)

    headers_actuales = reader.fieldnames or []
    requiere_migracion = (
        delimiter != ';'
        or "captura" in headers_actuales
        or headers_actuales != FIELD_KEYS
    )

    if not requiere_migracion:
        return

    with ruta_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(FIELD_KEYS)
        for row in rows:
            time_parts = _time_parts_from_timestamp_text(row.get("timestamp", ""))
            health = row.get("health_anomalias", "")
            events = row.get("eventos_activos", "")
            cpu1 = row.get("temp_cpu1", "")
            cpu2 = row.get("temp_cpu2", "")
            consola = row.get("estado_consola", "")
            error = row.get("error", "")
            detalle_alerta = row.get("detalle_alerta", "") or construir_detalle_alerta(
                health,
                events,
                cpu1,
                cpu2,
                consola,
                error,
            )
            estado_general = calcular_estado_general(
                health,
                events,
                cpu1,
                cpu2,
                consola,
                error,
            )

            writer.writerow([
                time_parts.get("anio", row.get("anio", "")),
                time_parts.get("mes", row.get("mes", "")),
                time_parts.get("dia", row.get("dia", row.get("dia_mes", ""))),
                time_parts.get("hora", ""),
                row.get("servidor", ""),
                row.get("ip", ""),
                health,
                events,
                cpu1,
                cpu2,
                consola,
                estado_general,
                detalle_alerta,
                error,
            ])


def registrar_resultado_csv(ruta_csv, fila):
    if not SAVE_LOCAL_CSV:
        return

    with ruta_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow([
            fila.get("anio", ""),
            fila.get("mes", ""),
            fila.get("dia", ""),
            fila.get("hora", ""),
            fila.get("servidor", ""),
            fila.get("ip", ""),
            fila.get("health_anomalias", ""),
            fila.get("eventos_activos", ""),
            fila.get("temp_cpu1", ""),
            fila.get("temp_cpu2", ""),
            fila.get("estado_consola", ""),
            fila.get("estado_general", ""),
            fila.get("detalle_alerta", ""),
            fila.get("error", ""),
        ])


def _obtener_worksheet_sheets():
    global _GSHEET_WS, _GSHEET_INICIALIZADO

    if not GSHEETS_ENABLED:
        return None
    if _GSHEET_WS is not None:
        return _GSHEET_WS
    if _GSHEET_INICIALIZADO:
        return None

    _GSHEET_INICIALIZADO = True

    if gspread is None:
        print("[Sheets] gspread no está instalado. Ejecuta: pip install gspread")
        return None

    if not GSHEETS_SPREADSHEET.strip():
        print("[Sheets] Falta GOOGLE_SHEETS_SPREADSHEET en el .env")
        return None

    try:
        client = gspread.service_account(filename=str(_resolver_path_proyecto(GSHEETS_CREDENTIALS_FILE)))
        spreadsheet = client.open(GSHEETS_SPREADSHEET)

        try:
            ws = spreadsheet.worksheet(GSHEETS_WORKSHEET)
        except WorksheetNotFound:
            ws = spreadsheet.add_worksheet(
                title=GSHEETS_WORKSHEET,
                rows=2000,
                cols=max(20, len(FIELD_KEYS) + 5),
            )

        encabezado_actual = ws.row_values(1)
        if encabezado_actual != SHEET_HEADERS:
            ws.update(values=[SHEET_HEADERS], range_name="A1")

        aplicar_formato_tabla_sheets(ws)
        _normalizar_columna_hora_sheets(ws)

        _GSHEET_WS = ws
        print(f"[Sheets] Conectado: {GSHEETS_SPREADSHEET} / {GSHEETS_WORKSHEET}")
        return _GSHEET_WS
    except Exception as e:
        print(f"[Sheets] Error al inicializar Google Sheets: {e}")
        return None


def sincronizar_resultado_sheets(fila):
    ws = _obtener_worksheet_sheets()
    if ws is None:
        return False

    try:
        valores = [fila.get(col, "") for col in FIELD_KEYS]
        ws.append_row(valores, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[Sheets] Error al enviar fila: {e}")
        return False


def _normalizar_columna_hora_sheets(ws, max_updates=5000):
    """Corrige horas historicas con comilla y normaliza a HH:mm."""
    try:
        hora_col_values = ws.col_values(4)
    except Exception:
        return

    if len(hora_col_values) <= 1:
        return

    updates = []
    for row_idx, raw_val in enumerate(hora_col_values[1:], start=2):
        text = str(raw_val or "").strip()
        if not text:
            continue

        if text.startswith("'"):
            text = text[1:].strip()

        match_hhmm = re.match(r"^(\d{1,2}):(\d{1,2})$", text)
        if match_hhmm:
            hh = int(match_hhmm.group(1))
            mm = int(match_hhmm.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                normalized = f"{hh:02d}:{mm:02d}"
            else:
                continue
        elif re.match(r"^\d{1,2}$", text):
            hh = int(text)
            if 0 <= hh <= 23:
                normalized = f"{hh:02d}:00"
            else:
                continue
        else:
            continue

        # Evitar writes innecesarios si ya esta normalizado y sin comilla.
        if raw_val == normalized:
            continue

        updates.append({"range": f"D{row_idx}", "values": [[normalized]]})
        if len(updates) >= max_updates:
            break

    if not updates:
        return

    try:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        print(f"[Sheets] Horas normalizadas en {len(updates)} filas.")
    except Exception as e:
        print(f"[Sheets] No se pudo normalizar columna hora: {e}")


def analizar_pantalla_negra(ruta_imagen):
    """
    Analiza los píxeles de una imagen para detectar si la pantalla está mayormente negra.
    Devuelve (es_negra, porcentaje_negro).
    """
    try:
        # Abrir imagen y convertir a modo RGB
        img = Image.open(ruta_imagen).convert('RGB')
        # Reducir tamaño para que el escaneo de píxeles sea instantáneo
        img.thumbnail((150, 150)) 
        # Usar reshape para convertir bytes a tuplas RGB (compatible con Pillow 14+)
        px_data = img.tobytes()
        pixels = [(px_data[i], px_data[i+1], px_data[i+2]) for i in range(0, len(px_data), 3)]

        pixeles_negros = 0
        for r, g, b in pixels:
            if r < 35 and g < 35 and b < 35:
                pixeles_negros += 1

        porcentaje_negro = (pixeles_negros / len(pixels)) * 100
        # Umbral configurable: si la mayoría de la pantalla es negra, se considera normal
        return porcentaje_negro >= 60, porcentaje_negro
    except Exception as e:
        print(f"Error analizando imagen {ruta_imagen}: {e}")
        return False, 0

async def auditar_servidor(servidor, pw):
    ip = servidor['ip']
    name = servidor['name']
    user = servidor['user']
    password = servidor['password']
    tz_local = _obtener_zona_horaria_local()
    now_dt = datetime.now(tz_local)
    time_parts = _build_time_parts(now_dt)

    errores_salud = ""
    num_events = ""
    temp_cpu1 = ""
    temp_cpu2 = ""
    estado_consola = "NO_VERIFICADA"
    ruta_captura = ""
    error_detalle = ""
    estado_general = "ALERTA"
    detalle_alerta = ""
    detalles_salud = []
    porcentaje_negro = None
    
    print(f"\n--- Iniciando revisión en {name} ({ip}) ---")

    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # --- LOGIN (Usando credenciales individuales del .env) ---
        await page.goto(f"https://{ip}/#/login")
        await page.get_by_placeholder(SEL_LOGIN_USER).fill(user)
        await page.get_by_placeholder(SEL_LOGIN_PASS).fill(password)
        await page.keyboard.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_selector(SEL_HEALTH_TAB, timeout=30000)
        await page.wait_for_timeout(5000)
        
        # --- A. HEALTH SUMMARY ---
        errores_salud = await page.locator(SEL_HEALTH_ANOMALIES).count()
        if errores_salud == 0:
            print("[✅] Health Summary: Todos los 8 íconos operativos (Verde).")
        else:
            salud_locator = page.locator(SEL_HEALTH_ANOMALIES)
            detalles_raw = await salud_locator.all_inner_texts()
            detalles_salud = [re.sub(r"\s+", " ", t).strip() for t in detalles_raw if str(t).strip()]
            print(f"[❌] Health Summary: Se detectaron {errores_salud} anomalías en el hardware.")
            if detalles_salud:
                print(f"[ℹ️] Detalles Health Summary: {' | '.join(detalles_salud[:3])}")

        # --- B. ACTIVE SYSTEM EVENTS ---
        events_tab = page.locator(SEL_EVENTS_TAB, has_text=SEL_EVENTS_TAB_TEXT)
        events_text = await events_tab.inner_text()
        
        match = re.search(r"\((\d+)\)", events_text)
        if match:
            num_events = int(match.group(1))
            if num_events == 0:
                print("[✅] Eventos: (0) Active System Events.")
            else:
                print(f"[⚠️] Eventos: Hay ({num_events}) eventos activos. (Se requiere atención)")
        else:
            print("[⚠️] No se pudo extraer el número de Active System Events.")

        # --- C. TEMPERATURA CPU ---
        for cpu in ["CPU1", "CPU2"]:
            temp_locator = page.locator(SEL_CPU_CONTAINER).filter(has_text=cpu).locator(SEL_CPU_VALUE)
            if await temp_locator.count() > 0:
                temp_text = await temp_locator.inner_text()
                match_temp = re.search(r"-?\d+", temp_text)
                if not match_temp:
                    print(f"[⚠️] No se pudo parsear la temperatura de {cpu}: {temp_text}")
                    continue

                temp = int(match_temp.group(0))
                if cpu == "CPU1":
                    temp_cpu1 = temp
                else:
                    temp_cpu2 = temp

                if 30 <= temp <= 50:
                    print(f"[✅] Temperatura {cpu}: {temp}°C (Dentro de norma)")
                else:
                    print(f"[❌] Temperatura {cpu}: {temp}°C (Fuera del rango 30-50°C)")
            else:
                print(f"[⚠️] No se encontraron datos de temperatura para {cpu}")

        # --- D. REMOTE CONSOLE PREVIEW (Análisis de Color) ---
        print("[*] Solicitando captura original al servidor para validar pantalla negra...")
        async with page.expect_download(timeout=30000) as download_info:
            await page.get_by_role("button", name=SEL_CAPTURE_BUTTON).click()
        
        download = await download_info.value
        ruta_captura = f"captura_{name}.png"
        await download.save_as(ruta_captura)
        
        # Análisis de la imagen descargada
        es_negra, porcentaje_negro = analizar_pantalla_negra(ruta_captura)
        if es_negra:
            estado_consola = "NEGRA_OK"
            print(f"[✅] Consola ESXi: Pantalla negra detectada ({porcentaje_negro:.1f}% negro). Captura guardada.")
        else:
            estado_consola = "NO_NEGRA_ALERTA"
            print(f"[❌] ALERTA: la consola no está negra ({porcentaje_negro:.1f}% negro). Revisar {ruta_captura}.")

    except Exception as e:
        error_detalle = str(e)
        estado_consola = "ERROR"
        print(f"[Error crítico] Fallo en la inspección de {name}: {e}")
    finally:
        try:
            await browser.close()
        except Exception as e:
            print(f"[Warn] No se pudo cerrar el navegador limpiamente para {name}: {e}")
        detalle_alerta = construir_detalle_alerta(
            errores_salud,
            num_events,
            temp_cpu1,
            temp_cpu2,
            estado_consola,
            error_detalle,
            detalles_salud=detalles_salud,
            porcentaje_negro=porcentaje_negro,
        )
        estado_general = calcular_estado_general(
            errores_salud,
            num_events,
            temp_cpu1,
            temp_cpu2,
            estado_consola,
            error_detalle,
            detalles_salud=detalles_salud,
            porcentaje_negro=porcentaje_negro,
        )
        fila_resultado = {
            "anio": time_parts.get("anio", ""),
            "mes": time_parts.get("mes", ""),
            "dia": time_parts.get("dia", ""),
            "hora": time_parts.get("hora", ""),
            "servidor": name,
            "ip": ip,
            "health_anomalias": errores_salud,
            "eventos_activos": num_events,
            "temp_cpu1": temp_cpu1,
            "temp_cpu2": temp_cpu2,
            "estado_consola": estado_consola,
            "estado_general": estado_general,
            "detalle_alerta": detalle_alerta,
            "error": error_detalle,
        }
        registrar_resultado_csv(CSV_PATH, fila_resultado)
        envio_ok = sincronizar_resultado_sheets(fila_resultado)
        if not envio_ok:
            raise RuntimeError(f"[Sheets] No se pudo sincronizar el resultado de {name}.")

async def main():
    # Si la lista está vacía, detener el script
    if not SERVIDORES:
        print("No hay servidores configurados. Revisa el archivo .env")
        return

    if not GSHEETS_ENABLED:
        print("Google Sheets es obligatorio. Activa GOOGLE_SHEETS_ENABLED=true en .env")
        return

    if not GSHEETS_SPREADSHEET.strip():
        print("Falta GOOGLE_SHEETS_SPREADSHEET en .env")
        return

    ws = _obtener_worksheet_sheets()
    if ws is None:
        print("No se pudo inicializar Google Sheets. Revisa credenciales/permisos.")
        return

    if SAVE_LOCAL_CSV:
        inicializar_csv(CSV_PATH)

    try:
        async with async_playwright() as pw:
            for server in SERVIDORES:
                await auditar_servidor(server, pw)
    except asyncio.CancelledError:
        print("Inicio de Playwright cancelado. Si no lo cancelaste manualmente, ejecuta: playwright install chromium")
        return

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Ejecucion interrumpida por el usuario.")