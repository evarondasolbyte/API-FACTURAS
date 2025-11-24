# En este script monto una pequeña API + CLI para descargar
# las facturas de Cursor.com usando Playwright contra el portal de Stripe.
# Está pensado para:
#  - Usarse desde línea de comandos con filtros de fecha o con --all

from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import uvicorn
import re
import asyncio
import argparse
import json
import pandas as pd
import gspread
from urllib.parse import urlparse
from google.oauth2.service_account import Credentials

GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def _excel_text(value: Any) -> Optional[str]:
    """
    Normaliza valores leídos desde Excel/Sheets para dejarlos como strings.
    Devuelvo None si el valor está vacío o es NaN.
    """
    if value is None:
        return None
    if isinstance(value, str):
        val = value.strip()
        return val or None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if pd.isna(value):
        return None
    val = str(value).strip()
    return val or None


def _excel_period(value: Any) -> Optional[str]:
    """
    Convierte los periodos (mes/año) en texto aceptado por descargar_facturas.
    """
    val = _excel_text(value)
    if not val:
        return None
    # Si viene como YYYY/MM lo paso a YYYY-MM
    val = val.replace("/", "-")
    return val


def _persist_excel_sheet(path: Path, sheet_name: str, df: pd.DataFrame) -> None:
    """
    Guarda un DataFrame en una hoja del Excel sin borrar el resto de hojas.
    """
    if path.exists():
        with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    else:
        with pd.ExcelWriter(path, engine="openpyxl", mode="w") as writer:
            df.to_excel(writer, sheet_name=sheet_name, index=False)


def _append_log_sheet(path: Path, entries: List[Dict[str, Any]], sheet_name: str = "Salida") -> None:
    """
    Añade nuevas entradas de log a un Excel de salida.
    """
    if not entries:
        return
    new_df = pd.DataFrame(entries)
    if path.exists():
        try:
            existing = pd.read_excel(path, sheet_name=sheet_name)
        except ValueError:
            existing = pd.DataFrame()
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    with pd.ExcelWriter(path, engine="openpyxl", mode="w") as writer:
        combined.to_excel(writer, sheet_name=sheet_name, index=False)


def _response_to_dict(response: Any) -> Dict[str, Any]:
    """
    Convierte la respuesta devuelta por descargar_facturas en un dict normal.
    """
    if isinstance(response, JSONResponse):
        try:
            return json.loads(response.body.decode("utf-8"))
        except Exception:
            return {"raw": response.body.decode("utf-8", errors="ignore")}
    if isinstance(response, dict):
        return response
    if hasattr(response, "dict"):
        try:
            return response.dict()  # type: ignore[attr-defined]
        except Exception:
            pass
    return {"raw": str(response)}


def _slugify_filename(text: Optional[str], default: str = "valor") -> str:
    """
    Convierte un texto en algo apto para nombre de archivo.
    """
    if not text:
        text = default
    t = _norm_txt(text)
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return t or default


def _entrada_alias(text: Optional[str]) -> str:
    """
    Obtiene un alias corto a partir de la columna Entrada.
    """
    if not text:
        return "cursor"
    parsed = urlparse(text)
    host = (parsed.netloc or parsed.path or "").lower()
    if host:
        host = host.split(":")[0]
        if "." in host:
            host = host.split(".")[0]
        return _slugify_filename(host, "cursor")
    return _slugify_filename(text, "cursor")


def _usuario_alias(text: Optional[str]) -> str:
    """
    Obtiene un alias corto para el usuario (sin dominio).
    """
    if not text:
        return "usuario"
    local = text.split("@")[0]
    return _slugify_filename(local, "usuario")


def _get_gspread_client(service_account_file: str) -> gspread.Client:
    """
    Autoriza y devuelve un cliente de gspread usando una cuenta de servicio.
    """
    creds = Credentials.from_service_account_file(
        service_account_file,
        scopes=GSHEETS_SCOPES
    )
    return gspread.authorize(creds)


def _get_worksheet(client: gspread.Client, sheet_id: str, sheet_name: str, *, create: bool = False):
    """
    Devuelve una worksheet (creándola si hace falta y se solicita).
    """
    sh = client.open_by_key(sheet_id)
    try:
        return sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        if not create:
            raise
        return sh.add_worksheet(title=sheet_name, rows=200, cols=26)


def _parse_sheet_values(values: List[List[str]]) -> Tuple[List[str], Dict[str, int], List[Dict[str, str]]]:
    """
    Recibe todas las filas de una hoja y devuelve:
      - Lista de cabeceras
      - Mapa cabecera -> índice de columna (1-based)
      - Lista de filas como diccionarios
    """
    if not values:
        return [], {}, []
    header = [h.strip() for h in values[0]]
    header_map = {name: idx + 1 for idx, name in enumerate(header) if name}
    rows: List[Dict[str, str]] = []
    for row_values in values[1:]:
        row_dict = {}
        for idx, col_name in enumerate(header):
            row_dict[col_name] = row_values[idx] if idx < len(row_values) else ""
        rows.append(row_dict)
    return header, header_map, rows

# Inicializo la app de FastAPI con un título y una descripción
app = FastAPI(title="API de Facturas Cursor", description="API para descargar facturas de Cursor.com")

# ==========================
# Utilidades de fecha/rango
# ==========================
# Aquí defino un diccionario para mapear abreviaturas y nombres de meses
# tanto en español como en inglés a su número de mes (1-12).
MONTHS = {
    # Español
    "ene":1, "enero":1, "feb":2, "febrero":2, "mar":3, "marzo":3, "abr":4, "abril":4,
    "may":5, "mayo":5, "jun":6, "junio":6, "jul":7, "julio":7, "ago":8, "agosto":8,
    "sep":9, "sept":9, "septi":9, "septiembre":9, "set":9, "setiembre":9,
    "oct":10, "octubre":10, "nov":11, "noviembre":11, "dic":12, "diciembre":12,
    # Inglés
    "jan":1, "january":1, "february":2, "mar":3, "march":3, "apr":4, "april":4,
    "may":5, "june":6, "jun":6, "july":7, "jul":7, "aug":8, "august":8,
    "september":9, "october":10, "november":11, "dec":12, "december":12,
}

def _parse_input_date(s: Optional[str], *, end=False) -> Optional[date]:
    """
    Esta función convierte cadenas de entrada tipo:
      - 'YYYY-MM'
      - 'YYYY-MM-DD'
    en objetos date.

    Si sólo viene 'YYYY-MM' y end=True, devuelvo el último día de ese mes.
    Si viene 'YYYY-MM' y end=False (por defecto), devuelvo el primer día del mes.
    Si la cadena no cumple los formatos, lanzo un ValueError.
    """
    if not s:
        return None
    s = s.strip()
    try:
        # Caso 1: formato completo 'YYYY-MM-DD'
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return datetime.strptime(s, "%Y-%m-%d").date()
        # Caso 2: sólo año y mes 'YYYY-MM'
        if re.fullmatch(r"\d{4}-\d{2}", s):
            d = datetime.strptime(s, "%Y-%m").date()
            if end:
                # Si piden fin de mes y es diciembre, devuelvo 31/12
                if d.month == 12:
                    return date(d.year, 12, 31)
                # Si no es diciembre, calculo el día anterior al primer día del mes siguiente
                nextm = date(d.year, d.month + 1, 1)
                return nextm - timedelta(days=1)
            # Si end=False, devuelvo el día 1 del mes
            return date(d.year, d.month, 1)
    except:
        # Si algo falla en el parseo, paso a lanzar error más abajo
        pass
    # Si no se ha podido interpretar, lanzo un error claro en español
    raise ValueError(f"Fecha inválida: {s} (usa YYYY-MM o YYYY-MM-DD)")

def _in_range_day(d: date, dfrom: Optional[date], dto: Optional[date]) -> bool:
    """
    Compruebo si una fecha d está dentro del rango [dfrom, dto].
    Si alguno de los extremos es None, no lo uso para limitar.
    """
    if dfrom and d < dfrom: return False
    if dto   and d > dto:   return False
    return True

def _norm_txt(s: str) -> str:
    """
    Normalizo texto para poder buscar patrones de forma más robusta:
    - Quito espacios en los extremos
    - Paso a minúsculas
    - Reemplazo vocales acentuadas y la 'ç'
    """
    return (s or "").strip().lower()\
        .replace("á","a").replace("é","e").replace("í","i")\
        .replace("ó","o").replace("ú","u").replace("ç","c")

def _parse_human_date_to_dateobj(text: str) -> Optional[date]:
    """
    Aquí convierto textos de fecha "humanos" en un date(YYYY, MM, DD).

    Ejemplos que intento reconocer:
      - '25 oct 2025'
      - '25 de octubre de 2025'
      - 'October 25, 2025'
      - Formatos ISO y típicos: '2025-10-25', '25/10/2025', etc.

    Devuelvo None si no consigo reconocer la fecha.
    """
    t = _norm_txt(text)

    # 1) '25 de octubre de 2025' (castellano con "de")
    m = re.search(r"(\d{1,2})\s+de\s+([a-zñ]+)\s+de\s+(\d{4})", t)
    if m:
        d, mon, y = m.groups()
        mon = MONTHS.get(mon)
        if mon: 
            try: return date(int(y), mon, int(d))
            except: return None

    # 2) '25 oct 2025' o variantes similares
    m = re.search(r"(\d{1,2})\s+([a-zñ]+)\s+(\d{4})", t)
    if m:
        d, mon, y = m.groups()
        mon = MONTHS.get(mon)
        if mon:
            try: return date(int(y), mon, int(d))
            except: return None

    # 3) 'October 25, 2025' (formato inglés)
    m = re.search(r"([a-z]+)\s+(\d{1,2}),\s*(\d{4})", t)
    if m:
        mon, d, y = m.groups()
        mon = MONTHS.get(mon)
        if mon:
            try: return date(int(y), mon, int(d))
            except: return None

    # 4) Formatos más estándar
    for fmt in ("%Y-%m-%d","%d/%m/%Y","%Y/%m/%d","%d-%m-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except:
            pass
    return None

# ==========================
# Playwright helpers
# ==========================
def _auto_scroll_until_bottom(page_or_frame, *, step_px: int = 1200, max_tries: int = 40, pause_ms: int = 200):
    """
    Esta función hace scroll automático hacia abajo en una página o frame
    en pasos de 'step_px'. Lo uso para intentar que se carguen más facturas
    en listados que van haciendo "infinite scroll".
    """
    for _ in range(max_tries):
        try:
            reached = page_or_frame.evaluate(f"""
                () => {{
                    const el = document.scrollingElement || document.documentElement || document.body;
                    const before = el.scrollTop;
                    el.scrollBy(0, {step_px});
                    const after = el.scrollTop;
                    const nearBottom = (el.scrollHeight - (after + el.clientHeight)) < 5;
                    return {{ advanced: after > before, nearBottom }};
                }}
            """)
            page_or_frame.wait_for_timeout(pause_ms)
            # Si detecto que ya estoy cerca del final, salgo
            if reached and reached.get("nearBottom"):
                break
        except:
            # Si algo falla (por ejemplo el frame desaparece), dejo de intentar
            break

def _quick_scroll(page_or_frame):
    """
    Aquí hago un scroll "rápido" y fuerte hacia abajo.
    Lo utilizo como apoyo para sacar a la vista posibles botones de "Ver más".
    """
    try:
        page_or_frame.evaluate("""
            () => {
                const el = document.scrollingElement || document.documentElement || document.body;
                el.scrollTo(0, el.scrollTop + 3000);
            }
        """)
    except:
        pass
    page_or_frame.wait_for_timeout(100)

def _find_billing_frame(page):
    """
    Algunos portales de Stripe de facturación se abren en iframes.
    Con esta función intento localizar el frame que contenga la parte de billing,
    buscando por URL o por textos típicos de facturas/historial.
    """
    for fr in page.frames:
        u = (fr.url or "").lower()
        # Primero miro si la URL contiene palabras clave típicas
        if any(k in u for k in ["stripe", "billing", "portal"]):
            return fr
        # Si no, intento localizar textos de "invoice/factura/historial"
        try:
            fr.get_by_text(re.compile(r"(invoice|invoices|factura|facturas|history|historial)", re.I)).first.wait_for(
                state="visible", timeout=800
            )
            return fr
        except:
            pass
    return None

def _focus_invoice_tab_if_needed(target):
    """
    A veces, dentro del portal hay varias pestañas (pagos, facturas, etc.).
    Aquí intento hacer click en cualquier pestaña relacionada con facturas/historial.
    """
    for txt in [
        "Invoices","Invoice history","Billing history","Payments","Statements",
        "Facturas","Historial de facturas","Historial","Pagos","View all invoices","Ver todas las facturas"
    ]:
        try:
            el = target.get_by_text(txt, exact=False).first
            el.wait_for(state="visible", timeout=800)
            el.click()
            target.wait_for_timeout(200)
            return True
        except:
            continue
    return False

def _wait_for_invoice_list(target, timeout_ms: int = 12000):
    """
    Espero a que aparezca algo que tenga pinta de listado de facturas.
    Lo hago de dos maneras:
      1) Buscando textos típicos
      2) Buscando tablas o elementos con data-testid relacionado
    """
    candidates = [
        "Invoice history","Invoices","Download invoice","View invoice",
        "Historial de facturas","Facturas","Descargar factura","Ver factura",
        "Payment history","Billing history","Pagos","Historial"
    ]
    # Primero intento por texto
    for txt in candidates:
        try:
            target.get_by_text(txt, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except:
            continue
    # Si no encuentro nada por texto, pruebo selectores genéricos de tabla
    for sel in ["table","[role='table']","tbody tr","[data-testid*='invoice']","[class*='invoice'] table"]:
        try:
            target.locator(sel).first.wait_for(state="visible", timeout=timeout_ms//2)
            return True
        except:
            continue
    return False

def _click_any_more_button(target, timeout_ms: int = 700) -> bool:
    """
    Intenta pulsar cualquier variante de "ver/mostrar más" para cargar más facturas.
    Devuelve True si pulsó algún botón/enlace, False si no encontró nada.

    Lo uso para ir expandiendo el listado de facturas paso a paso.
    """
    labels = [
        "Ver más", "Ver mas", "Mostrar más", "Mostrar mas",
        "View more", "Load more", "See more", "More"
    ]
    # 1) Intento primero por texto directo con Playwright
    forbidden = ["option", "options", "opcion", "opciones", "predeterminada", "método", "metodo", "payment"]
    for txt in labels:
        try:
            btn = target.get_by_text(txt, exact=False).first
            btn.wait_for(state="visible", timeout=timeout_ms)
            normalized = ""
            try:
                normalized = (btn.inner_text() or "").strip().lower()
            except:
                pass
            if not normalized:
                try:
                    normalized = (btn.text_content() or "").strip().lower()
                except:
                    normalized = ""
            if any(bad in normalized for bad in forbidden) or len(normalized) <= 3:
                continue
            btn.scroll_into_view_if_needed()
            btn.click(timeout=timeout_ms)
            target.wait_for_timeout(160)
            return True
        except:
            continue
    # 2) Si no va, pruebo con JS recorriendo botones y enlaces y mirando innerText
    try:
        clicked = target.evaluate("""
            () => {
                const labels = [
                    "ver más","ver mas","mostrar más","mostrar mas",
                    "view more","load more","see more","more"
                ];
                const forbidden = ["option","options","opcion","opciones","predeterminada","método","metodo","payment"];
                const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                for (const el of nodes) {
                    const t = (el.innerText || el.textContent || "").toLowerCase();
                    if (t.length <= 3) continue;
                    if (forbidden.some(b => t.includes(b))) continue;
                    if (labels.some(l => t.includes(l))) { el.click(); return true; }
                }
                return false;
            }
        """)
        if clicked:
            target.wait_for_timeout(160)
            return True
    except:
        pass
    return False

def _collect_invoice_items(target) -> List[Tuple[str, str]]:
    """
    Recojo del DOM todos los enlaces que tienen pinta de ser facturas de Stripe.

    Devuelvo una lista de tuplas: (href, texto_del_enlace).
    De momento sólo miro enlaces que contengan 'invoice.stripe.com/i/'.
    """
    try:
        items = target.evaluate("""
            () => Array.from(
                document.querySelectorAll("a[href*='invoice.stripe.com/i/']")
            ).map(a => [a.href, a.textContent || ""])
        """) or []
        return items
    except:
        return []

def _expand_all_invoices_all_languages(target, max_rounds: int = 40) -> None:
    """
    En este helper me dedico a pulsar botones de "Ver/Mostrar más" de manera
    iterativa para intentar cargar TODAS las facturas del historial.

    Voy:
      - Haciendo scroll a fondo
      - Intentando pulsar cualquier botón de "más"
      - Recalculando cuántos enlaces de factura tengo
      - Paro cuando deja de crecer el número de enlaces o llego al máximo de rondas.
    """
    rounds = 0
    last_count = len(_collect_invoice_items(target))
    while rounds < max_rounds:
        rounds += 1
        # Primero, scroll fuerte hacia el final (algunos portales sólo muestran el botón abajo del todo)
        _quick_scroll(target)
        _auto_scroll_until_bottom(target, step_px=2000, max_tries=2, pause_ms=120)
        # Intento pulsar algo que tenga pinta de "ver más"
        clicked = _click_any_more_button(target, timeout_ms=800)
        # Recalculo la lista de facturas visibles
        items = _collect_invoice_items(target)
        if not clicked:
            # Si no pulsé nada pero el scroll hizo aparecer nuevas facturas, repito otra vuelta
            if len(items) > last_count:
                last_count = len(items)
                continue
            # Si no hay más enlaces, salgo
            break
        # Si se pulsó, espero un poco y vuelvo a comprobar si ha crecido la lista
        target.wait_for_timeout(220)
        items = _collect_invoice_items(target)
        if len(items) <= last_count:
            # Si no ha aumentado, probablemente ya no hay más facturas que cargar
            break
        last_count = len(items)

# =================================================
# Flujo principal (filtrando ANTES de abrir)
# =================================================
def descargar_facturas(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    download_all: bool = False,  # si no hay rango y --all, expande y baja TODO
    *,
    entrada_tag: Optional[str] = None,
    usuario_tag: Optional[str] = None
) -> Dict[str, str]:
    """
    Esta es la función principal que se encarga de:
      - Gestionar la sesión de navegador con Playwright
      - Ir a la zona de Billing de Cursor
      - Llegar al portal de Stripe
      - Expandir el listado de facturas según rango/--all
      - Filtrar por fechas
      - Abrir cada factura y descargar el PDF en carpetas por fecha

    Argumentos:
      - date_from: fecha desde (string, 'YYYY-MM' o 'YYYY-MM-DD')
      - date_to:   fecha hasta (string, 'YYYY-MM' o 'YYYY-MM-DD')
      - download_all: si es True y NO hay rango, descargo TODO el historial
    """
    # Convierto los strings de entrada a objetos date (si vienen)
    dfrom = _parse_input_date(date_from, end=False) if date_from else None
    dto   = _parse_input_date(date_to,   end=True)  if date_to   else None
    print(f"🧭 Filtro -> from: {dfrom}  to: {dto}  | all={download_all}")

    browser = None
    context = None
    try:
        # Inicio Playwright y levanto un Chromium real (canal Chrome) en modo no headless
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage'
            ]
        )

        # Defino la carpeta donde guardo el estado de sesión del navegador
        ctx_dir = Path(".browser_context")
        ctx_dir.mkdir(exist_ok=True)
        state_file = ctx_dir / "state.json"

        # Creo el contexto de navegador usando storage_state si ya lo tengo,
        # para reaprovechar cookies/sesión entre ejecuciones
        context = browser.new_context(
            storage_state=str(state_file) if state_file.exists() else None,
            accept_downloads=True
        )
        page = context.new_page()

        # ==========================
        # 1) Gestión de sesión/login
        # ==========================
        has_cookies = False
        if state_file.exists():
            try:
                # Intento ir directamente al dashboard suponiendo que la sesión es válida
                page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except:
                    pass
                u = (page.url or "").lower()
                body = ""
                try:
                    body = page.inner_text("body").lower()
                except:
                    pass
                # Compruebo que estamos en el dashboard y no en páginas de login/authenticator/errores raros
                if ("dashboard" in u and "login" not in u and "sign" not in u and
                    "authenticator" not in u and "can't verify" not in body):
                    has_cookies = True
            except:
                pass

        # Si no tengo sesión válida, dejo que el usuario haga login manualmente
        if not has_cookies:
            page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=25000)
            start = datetime.now()
            # Le doy hasta 5 minutos para que complete el login (incluyendo 2FA si hace falta)
            while (datetime.now() - start).total_seconds() < 300:
                u = (page.url or "").lower()
                if "dashboard" in u and all(k not in u for k in ("login","sign","authenticator")):
                    break
                page.wait_for_timeout(1200)
            # Una vez logado, guardo el estado de la sesión
            try:
                context.storage_state(path=str(state_file))
            except:
                pass

        # ==========================
        # 2) Navegar a "Billing & Invoices"
        # ==========================
        try:
            # Primero intento localizar el enlace por texto directo con Playwright
            el = page.get_by_text("Billing & Invoices", exact=False).first
            el.wait_for(state="visible", timeout=6000)
            el.click()
        except:
            # Si falla, pruebo con JS buscando enlaces y botones que contengan ese texto
            ok = page.evaluate("""
                () => {
                    const txt = 'Billing & Invoices';
                    for (const el of document.querySelectorAll('a,button,[role="link"],[role="button"]')) {
                        if (el.innerText && el.innerText.includes(txt)) { el.click(); return true; }
                    }
                    return false;
                }
            """)
            if not ok:
                raise Exception("No se encontró 'Billing & Invoices'.")

        page.wait_for_load_state("networkidle", timeout=15000)

        # ==========================
        # 3) Botón "Manage subscription" (abre portal de Stripe)
        # ==========================
        manage_btn = None
        # Intento localizar varias variantes del texto
        for txt in ["Manage subscription", "Gestionar suscripción", "Manage", "Gestionar"]:
            try:
                cand = page.get_by_text(txt, exact=False).first
                cand.wait_for(state="visible", timeout=3000)
                manage_btn = cand
                break
            except:
                continue
        # Si todavía no lo tengo, uso selectores por botón/enlace
        if not manage_btn:
            try:
                cand = page.locator('button:has-text("Manage"), a:has-text("Manage")').first
                cand.wait_for(state="visible", timeout=3000)
                manage_btn = cand
            except:
                pass
        if not manage_btn:
            raise Exception("No se encontró 'Manage subscription'.")

        # Aquí espero a que se abra un popup (nueva pestaña/ventana) tras pulsar el botón
        new_page = None
        try:
            with page.expect_popup() as pinfo:
                manage_btn.click()
            new_page = pinfo.value
        except:
            # Si no hay popup "formal", puede que se abra en la misma pestaña o en otra no capturada
            try:
                manage_btn.click()
            except:
                page.evaluate("el => el.click()", manage_btn)
            page.wait_for_timeout(600)
            # Busco entre todas las páginas del contexto la que tenga pinta de billing/portal de Stripe
            for p in context.pages[::-1]:
                uu = (p.url or "").lower()
                if any(s in uu for s in ("billing.stripe", "invoice.stripe", "/p/session", "portal")):
                    new_page = p
                    break
        # Si no he conseguido localizar una página nueva, sigo en la misma
        if not new_page:
            new_page = page

        # Espero a que cargue
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except:
            pass
        try:
            new_page.wait_for_load_state("networkidle", timeout=20000)
        except:
            pass

        # ==========================
        # 4) Localizar el frame de facturación y la pestaña de facturas
        # ==========================
        billing_frame = _find_billing_frame(new_page)
        target = billing_frame if billing_frame else new_page
        _focus_invoice_tab_if_needed(target)

        # Me aseguro de que el listado de facturas esté visible
        _wait_for_invoice_list(target, timeout_ms=8000)

        # Recojo las facturas que ya están visibles sin hacer scroll extra
        all_items: List[Tuple[str,str]] = _collect_invoice_items(target)

        # Siempre intento expandir un poco el listado para asegurarme de que cargo todo
        # aunque haya rango definido (algunos portales sólo cargan 3-4 registros hasta
        # que detectan scroll).
        _expand_all_invoices_all_languages(target, max_rounds=25)
        all_items = _collect_invoice_items(target)

        # --------- Lógica de expansión según modo de uso ---------
        def oldest_date_in_items(items: List[Tuple[str,str]]) -> Optional[date]:
            """
            Obtengo la fecha más antigua que veo entre los textos de los enlaces de factura.
            Me sirve para saber si tengo que cargar más páginas para cubrir el rango.
            """
            ds = [_parse_human_date_to_dateobj(txt) for _, txt in items]
            ds = [d for d in ds if d]
            return min(ds) if ds else None

        # a) Si tengo un rango 'from', intento expandir hacia atrás hasta cubrir el rango (si hace falta)
        if dfrom:
            while True:
                od = oldest_date_in_items(all_items)
                # Si no tengo fecha más antigua o la más antigua es aún >= dfrom, intento cargar más
                need_more = (od is None) or (od >= dfrom)
                if not need_more:
                    break
                if not _click_any_more_button(target, timeout_ms=800):
                    break
                _quick_scroll(target)
                all_items = _collect_invoice_items(target)

        # b) Si NO hay rango y el usuario ha puesto --all, entonces intento expandir hasta el final
        if download_all and not dfrom and not dto:
            _expand_all_invoices_all_languages(target, max_rounds=60)
            all_items = _collect_invoice_items(target)

        # ==========================
        # 5) Filtrado por rango sin abrir cada factura
        # ==========================
        filtered: List[Tuple[str,str]] = []
        for href, txt in all_items:
            d = _parse_human_date_to_dateobj(txt)
            if not d:
                # Si no puedo extraer fecha del texto del enlace, lo dejo pasar y
                # ya lo validaré después leyendo la fecha dentro de la propia factura
                filtered.append((href, txt))
                continue
            if _in_range_day(d, dfrom, dto):
                filtered.append((href, txt))

        # Ordeno para que empiece siempre desde la fecha más antigua (periodo de inicio)
        def _sort_key(item: Tuple[str,str]):
            d = _parse_human_date_to_dateobj(item[1])
            return d or date.max
        filtered.sort(key=_sort_key)

        print(f"🧾 Enlaces totales: {len(all_items)} | En rango: {len(filtered)}")

        # Si después de filtrar no hay nada, devuelvo una respuesta limpia
        if not filtered:
            try:
                context.storage_state(path=str(state_file))
            except:
                pass
            context.close()
            browser.close()
            pw.stop()
            return JSONResponse(
                status_code=200,
                content={
                    "estado": "ok",
                    "mensaje": "No hay facturas en el rango indicado.",
                    "descargadas": 0,
                    "errores": [],
                    "filtro": {"from": str(dfrom), "to": str(dto), "all": download_all}
                }
            )

        # URL del listado para poder volver después de abrir cada factura
        list_url = getattr(target, "url", None) or new_page.url
        DOWNLOAD_WAIT_VISIBLE = 1200
        DOWNLOAD_EXPECT_MS   = 8000

        descargadas = 0
        errores = []

        # ==========================
        # 6) Bucle para abrir y descargar cada factura filtrada
        # ==========================
        for idx, (href, link_txt) in enumerate(filtered, 1):
            print(f"\n➡️  ({idx}/{len(filtered)}) Abriendo: {href}  | txt='{link_txt.strip()[:40]}'")
            new_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            try:
                new_page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            invoice_page = new_page
            invoice_page.wait_for_timeout(300)

            # Función interna para extraer una fecha fiable de la página de la factura
            def _extract_invoice_date(page) -> str:
                body = ""
                try:
                    body = page.inner_text("body")
                except:
                    pass
                # Intento parsear cualquier fecha reconocible en el body;
                # si no encuentro nada, uso la fecha de hoy como fallback
                d = _parse_human_date_to_dateobj(body) or date.today()
                return f"{d.year}_{str(d.month).zfill(2)}_{str(d.day).zfill(2)}"

            # Genero una fecha en formato YYYY_MM_DD para usarla en la carpeta
            fecha_texto = _extract_invoice_date(invoice_page)
            d_real = datetime.strptime(fecha_texto, "%Y_%m_%d").date()

            # Segunda verificación de rango con la fecha interna de la factura
            if not _in_range_day(d_real, dfrom, dto):
                print(f"⏭️  (verificación) {fecha_texto} fuera de rango. Saltando descarga.")
                # Si quedan más facturas, vuelvo al listado
                if idx < len(filtered):
                    new_page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        new_page.wait_for_load_state("networkidle", timeout=15000)
                    except:
                        pass
                continue

            # Carpeta base donde guardo todas las facturas: Escritorio/FACTURAS
            base = Path.home() / "Desktop" / "FACTURAS"
            base.mkdir(parents=True, exist_ok=True)
            entrada_slug = _slugify_filename(entrada_tag, "cursor")
            usuario_slug = _usuario_alias(usuario_tag)
            ruta_archivo = base / f"{fecha_texto}_{entrada_slug}_{usuario_slug}.pdf"

            print("⬇️ Descargando…")
            download = None

            # Función auxiliar para intentar hacer click en un localizador y esperar el download
            def _try_click_fast(loc):
                nonlocal download
                try:
                    loc.wait_for(state="visible", timeout=DOWNLOAD_WAIT_VISIBLE)
                except:
                    pass
                try:
                    with invoice_page.expect_download(timeout=DOWNLOAD_EXPECT_MS) as dlinfo:
                        loc.click(timeout=DOWNLOAD_WAIT_VISIBLE, force=True)
                    download = dlinfo.value
                    return True
                except:
                    return False

            ok_clicked = False
            # 1) Intento por data-testid específico de Stripe
            try:
                btn = invoice_page.locator("[data-testid='download-invoice-pdf-button']").first
                ok_clicked = _try_click_fast(btn)
            except:
                pass
            # 2) Si no, pruebo por textos de "Descargar/Download"
            if not ok_clicked:
                for txt in ["Descargar factura","Download invoice","Download","Descargar","Descargar PDF"]:
                    try:
                        btn = invoice_page.get_by_text(txt, exact=False).first
                        if _try_click_fast(btn):
                            ok_clicked = True
                            break
                    except:
                        continue
            # 3) Y por último, algunos selectores genéricos
            if not ok_clicked:
                for sel in [
                    'button:has-text("Descargar factura")',
                    'a:has-text("Descargar factura")',
                    'button:has-text("Download")',
                    'a:has-text("Download")',
                    "[data-testid='download-invoice-receipt-pdf-button']"
                ]:
                    try:
                        btn = invoice_page.locator(sel).first
                        if _try_click_fast(btn):
                            ok_clicked = True
                            break
                    except:
                        continue

            # Si no se ha disparado ningún download, registro un error
            if not download:
                msg = "No se pudo iniciar la descarga (timeout rápido)."
                print("❌ " + msg)
                errores.append({"href": href, "error": msg})
            else:
                # Si sí se ha descargado, intento guardar el archivo en la ruta deseada
                try:
                    download.save_as(str(ruta_archivo))
                except Exception as e:
                    # Si save_as falla (por ejemplo por permisos), intento mover el archivo temporal
                    try:
                        tmp = download.path()
                        Path(tmp).replace(ruta_archivo)
                    except:
                        errores.append({"href": href, "error": str(e)})
                print(f"✅ Guardada en: {ruta_archivo}")
                descargadas += 1

            # Vuelvo al listado si aún quedan facturas por procesar
            if idx < len(filtered):
                new_page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    new_page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass

        # Al terminar, guardo el estado de sesión y cierro navegador/Playwright
        try:
            context.storage_state(path=str(state_file))
        except:
            pass
        context.close()
        browser.close()
        pw.stop()

        # Devuelvo una respuesta JSON resumida con el resultado de la operación
        return JSONResponse(
            status_code=200,
            content={
                "estado": "exitoso",
                "mensaje": f"Descargadas {descargadas} factura(s).",
                "descargadas": descargadas,
                "errores": errores,
                "filtro": {"from": str(dfrom), "to": str(dto), "all": download_all},
                "carpeta_base": str((Path.home() / "Desktop" / "FACTURAS").absolute())
            }
        )

    except Exception as e:
        # Cualquier excepción global la encapsulo en un mensaje unificado
        err = f"Error al descargar facturas: {str(e)}"
        print("\n❌ " + err)
        # Dejo que el caller (API/CLI) decida cómo tratarlo
        raise Exception(err)


def ejecutar_desde_excel(
    excel_path: str,
    sheet_name: str = "Entrada",
    estado_disparador: str = "Ejecutar",
    log_excel_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Lee un Excel de configuración y ejecuta descargar_facturas por cada fila
    cuyo estado coincida con estado_disparador.
    """
    file_path = Path(excel_path)
    if not file_path.exists():
        raise FileNotFoundError(f"No se encontró el Excel de configuración: {excel_path}")

    df = pd.read_excel(file_path, sheet_name=sheet_name)
    if df.empty:
        return {"total_filas": 0, "procesadas": 0, "saltadas": 0, "log_registrado": 0}

    trigger = (estado_disparador or "").strip().lower()
    log_entries: List[Dict[str, Any]] = []
    procesadas = 0
    saltadas = 0

    for idx, row in df.iterrows():
        estado = (_excel_text(row.get("Estado")) or "").lower()
        if estado != trigger:
            saltadas += 1
            continue

        usuario_val = _excel_text(row.get("Usuario")) or "desconocido"
        entrada_val = _excel_text(row.get("Entrada")) or "https://cursor.com/dashboard"
        entrada_alias = _entrada_alias(entrada_val)
        usuario_alias = _usuario_alias(usuario_val)
        periodo_inicio = _excel_period(row.get("Periodo Inicio"))
        periodo_fin = _excel_period(row.get("Periodo Fin"))

        df.at[idx, "Estado"] = "Pendiente"
        _persist_excel_sheet(file_path, sheet_name, df)

        try:
            respuesta = descargar_facturas(
                periodo_inicio,
                periodo_fin,
                entrada_tag=entrada_alias,
                usuario_tag=usuario_alias
            )
            data = _response_to_dict(respuesta)
            mensaje = data.get("mensaje", "Descarga ejecutada")
            df.at[idx, "Estado"] = "Ejecutado"
            df.at[idx, "Resultado"] = mensaje
            procesadas += 1
            log_entries.append({
                "Mensaje": (
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"{entrada_val} - Usuario {usuario_val}: facturas de "
                    f"{periodo_inicio or 'inicio'} a {periodo_fin or 'fin'} | {mensaje}"
                )
            })
        except Exception as exc:
            df.at[idx, "Estado"] = "Error"
            df.at[idx, "Resultado"] = str(exc)
        finally:
            _persist_excel_sheet(file_path, sheet_name, df)

    if log_excel_path:
        _append_log_sheet(Path(log_excel_path), log_entries)

    return {
        "total_filas": len(df),
        "procesadas": procesadas,
        "saltadas": saltadas,
        "log_registrado": len(log_entries),
        "log_path": log_excel_path
    }


def ejecutar_desde_sheet(
    sheet_id: str,
    *,
    service_account_file: str = "service_account.json",
    sheet_name: str = "Entrada",
    estado_disparador: str = "Ejecutar",
    log_sheet_id: Optional[str] = None,
    log_sheet_name: str = "Salida"
) -> Dict[str, Any]:
    """
    Variante que lee/escribe directamente sobre Google Sheets usando gspread.
    """
    client = _get_gspread_client(service_account_file)
    worksheet = _get_worksheet(client, sheet_id, sheet_name)
    values = worksheet.get_all_values()
    header, header_map, rows = _parse_sheet_values(values)
    if not rows:
        return {"total_filas": 0, "procesadas": 0, "saltadas": 0, "log_registrado": 0}

    required_cols = ["Estado", "Resultado", "Usuario", "Periodo Inicio", "Periodo Fin", "Entrada"]
    missing = [col for col in required_cols if col not in header_map]
    if missing:
        raise ValueError(f"Faltan columnas en la hoja '{sheet_name}': {', '.join(missing)}")

    trigger = (estado_disparador or "").strip().lower()
    procesadas = 0
    saltadas = 0
    log_entries: List[List[str]] = []

    for offset, row in enumerate(rows, start=2):
        estado = (row.get("Estado") or "").strip().lower()
        if estado != trigger:
            saltadas += 1
            continue

        usuario_val = (row.get("Usuario") or "desconocido").strip() or "desconocido"
        entrada_val = (row.get("Entrada") or "https://cursor.com/dashboard").strip() or "https://cursor.com/dashboard"
        entrada_alias = _entrada_alias(entrada_val)
        usuario_alias = _usuario_alias(usuario_val)
        periodo_inicio = _excel_period(row.get("Periodo Inicio"))
        periodo_fin = _excel_period(row.get("Periodo Fin"))

        worksheet.update_cell(offset, header_map["Estado"], "Pendiente")

        try:
            respuesta = descargar_facturas(
                periodo_inicio,
                periodo_fin,
                entrada_tag=entrada_alias,
                usuario_tag=usuario_alias
            )
            data = _response_to_dict(respuesta)
            mensaje = data.get("mensaje", "Descarga ejecutada")
            worksheet.update_cell(offset, header_map["Estado"], "Ejecutado")
            worksheet.update_cell(offset, header_map["Resultado"], mensaje)
            procesadas += 1
            log_entries.append([
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{entrada_val} - Usuario {usuario_val}: {mensaje}"
            ])
        except Exception as exc:
            worksheet.update_cell(offset, header_map["Estado"], "Error")
            worksheet.update_cell(offset, header_map["Resultado"], str(exc))

    if log_entries:
        log_ws = _get_worksheet(
            client,
            log_sheet_id or sheet_id,
            log_sheet_name,
            create=True
        )
        log_ws.append_rows(log_entries, value_input_option="USER_ENTERED")

    return {
        "total_filas": len(rows),
        "procesadas": procesadas,
        "saltadas": saltadas,
        "log_registrado": len(log_entries),
        "log_sheet": log_sheet_name
    }

# ============ FastAPI ============
class DateRange(BaseModel):
    """
    Modelo de entrada para el endpoint POST.
    date_from y date_to son opcionales y siguen el mismo formato
    que los parámetros de línea de comandos.
    """
    date_from: Optional[str] = None
    date_to:   Optional[str] = None

@app.get("/")
async def root():
    """
    Endpoint raíz muy sencillo que sólo devuelve
    un mensaje informativo y la ruta principal para descargar facturas.
    """
    return {
        "mensaje": "API de Facturas de Cursor",
        "endpoints": {"descargar_facturas": "/api/facturas/descargar"}
    }

@app.post("/api/facturas/descargar")
async def descargar_facturas_endpoint(payload: DateRange):
    """
    Endpoint principal de la API.
    Llama a la función descargar_facturas en un executor (hilo)
    para no bloquear el event loop de FastAPI.
    """
    try:
        loop = asyncio.get_event_loop()
        # En la API mantengo el comportamiento SIN --all, sólo rango de fechas.
        resultado = await loop.run_in_executor(
            None,
            descargar_facturas,
            payload.date_from,
            payload.date_to
        )
        return resultado
    except Exception as e:
        # Si algo peta dentro del flujo, devuelvo un 500 con el detalle del error
        raise HTTPException(status_code=500, detail=f"Error al descargar facturas: {str(e)}")

# ============ CLI ============
if __name__ == "__main__":
    # Si ejecuto este archivo directamente, levanto un CLI con argparse.
    parser = argparse.ArgumentParser(
        description="Descargar facturas de Cursor con filtro de fechas opcional."
    )
    parser.add_argument(
        "--from", "-f",
        dest="date_from",
        help="Fecha desde (YYYY-MM o YYYY-MM-DD)",
        default=None
    )
    parser.add_argument(
        "--to", "-t",
        dest="date_to",
        help="Fecha hasta (YYYY-MM o YYYY-MM-DD)",
        default=None
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="download_all",
        help="Descargar TODAS las facturas (expande hasta el final) si no se indica rango."
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Arranca API FastAPI en vez de ejecutar la descarga directa"
    )
    parser.add_argument(
        "--excel",
        dest="excel_path",
        help="Ruta a un Excel de configuración para ejecutar múltiples descargas",
        default=None
    )
    parser.add_argument(
        "--excel-sheet",
        dest="excel_sheet",
        help="Nombre de la hoja dentro del Excel de configuración",
        default="Entrada"
    )
    parser.add_argument(
        "--excel-estado",
        dest="excel_estado",
        help="Valor de la columna Estado que dispara la ejecución (por defecto 'Ejecutar')",
        default="Ejecutar"
    )
    parser.add_argument(
        "--log-excel",
        dest="log_excel",
        help="Ruta a un Excel de salida para registrar el log",
        default=None
    )
    parser.add_argument(
        "--sheet-id",
        dest="sheet_id",
        help="ID de Google Sheet (entrada) para ejecutar sin descargar XLSX",
        default=None
    )
    parser.add_argument(
        "--sheet-tab",
        dest="sheet_tab",
        help="Nombre de la pestaña en Google Sheets",
        default="Entrada"
    )
    parser.add_argument(
        "--sheet-estado",
        dest="sheet_estado",
        help="Valor de la columna Estado que dispara la ejecución (por defecto 'Ejecutar')",
        default="Ejecutar"
    )
    parser.add_argument(
        "--sheet-log-id",
        dest="sheet_log_id",
        help="ID de Google Sheet para el log (por defecto el mismo que entrada)",
        default=None
    )
    parser.add_argument(
        "--sheet-log-tab",
        dest="sheet_log_tab",
        help="Nombre de la pestaña de log en Google Sheets",
        default="Salida"
    )
    parser.add_argument(
        "--service-account",
        dest="service_account",
        help="Ruta al JSON de la cuenta de servicio para Google Sheets",
        default="service_account.json"
    )
    args = parser.parse_args()

    if args.sheet_id:
        resumen = ejecutar_desde_sheet(
            args.sheet_id,
            service_account_file=args.service_account,
            sheet_name=args.sheet_tab,
            estado_disparador=args.sheet_estado,
            log_sheet_id=args.sheet_log_id,
            log_sheet_name=args.sheet_log_tab
        )
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
    elif args.excel_path:
        resumen = ejecutar_desde_excel(
            args.excel_path,
            sheet_name=args.excel_sheet,
            estado_disparador=args.excel_estado,
            log_excel_path=args.log_excel
        )
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
    elif args.api:
        # Si el usuario pone --api, levanto el servidor FastAPI en el puerto 8000
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        # Si no, ejecuto la descarga directa desde CLI
        out = descargar_facturas(args.date_from, args.date_to, download_all=args.download_all)
        try:
            # Si out es un JSONResponse, imprimo su body decodificado
            print(out.body.decode("utf-8"))
        except:
            # Por si acaso, lo imprimo tal cual
            print(out)
