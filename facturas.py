from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import uvicorn
import re
import asyncio
import argparse

app = FastAPI(title="API de Facturas Cursor", description="API para descargar facturas de Cursor.com")

# ==========================
# Utilidades de fecha/rango
# ==========================
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
    """Admite 'YYYY-MM' o 'YYYY-MM-DD'. end=True -> fin de mes."""
    if not s:
        return None
    s = s.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return datetime.strptime(s, "%Y-%m-%d").date()
        if re.fullmatch(r"\d{4}-\d{2}", s):
            d = datetime.strptime(s, "%Y-%m").date()
            if end:
                if d.month == 12:
                    return date(d.year, 12, 31)
                nextm = date(d.year, d.month + 1, 1)
                return nextm - timedelta(days=1)
            return date(d.year, d.month, 1)
    except:
        pass
    raise ValueError(f"Fecha inválida: {s} (usa YYYY-MM o YYYY-MM-DD)")

def _in_range_day(d: date, dfrom: Optional[date], dto: Optional[date]) -> bool:
    if dfrom and d < dfrom: return False
    if dto   and d > dto:   return False
    return True

def _norm_txt(s: str) -> str:
    return (s or "").strip().lower()\
        .replace("á","a").replace("é","e").replace("í","i")\
        .replace("ó","o").replace("ú","u").replace("ç","c")

def _parse_human_date_to_dateobj(text: str) -> Optional[date]:
    """
    Convierte textos como:
      - '25 oct 2025', '25 de octubre de 2025', 'October 25, 2025'
    a date(YYYY, MM, DD). Devuelve None si no reconoce.
    """
    t = _norm_txt(text)

    # 1) '25 de octubre de 2025'
    m = re.search(r"(\d{1,2})\s+de\s+([a-zñ]+)\s+de\s+(\d{4})", t)
    if m:
        d, mon, y = m.groups()
        mon = MONTHS.get(mon)
        if mon: 
            try: return date(int(y), mon, int(d))
            except: return None

    # 2) '25 oct 2025'
    m = re.search(r"(\d{1,2})\s+([a-zñ]+)\s+(\d{4})", t)
    if m:
        d, mon, y = m.groups()
        mon = MONTHS.get(mon)
        if mon:
            try: return date(int(y), mon, int(d))
            except: return None

    # 3) 'October 25, 2025'
    m = re.search(r"([a-z]+)\s+(\d{1,2}),\s*(\d{4})", t)
    if m:
        mon, d, y = m.groups()
        mon = MONTHS.get(mon)
        if mon:
            try: return date(int(y), mon, int(d))
            except: return None

    # 4) ISO '2025-10-25' o '25/10/2025'
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
            if reached and reached.get("nearBottom"):
                break
        except:
            break

def _quick_scroll(page_or_frame):
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
    for fr in page.frames:
        u = (fr.url or "").lower()
        if any(k in u for k in ["stripe", "billing", "portal"]):
            return fr
        try:
            fr.get_by_text(re.compile(r"(invoice|invoices|factura|facturas|history|historial)", re.I)).first.wait_for(
                state="visible", timeout=800
            )
            return fr
        except:
            pass
    return None

def _focus_invoice_tab_if_needed(target):
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
    candidates = [
        "Invoice history","Invoices","Download invoice","View invoice",
        "Historial de facturas","Facturas","Descargar factura","Ver factura",
        "Payment history","Billing history","Pagos","Historial"
    ]
    for txt in candidates:
        try:
            target.get_by_text(txt, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except:
            continue
    for sel in ["table","[role='table']","tbody tr","[data-testid*='invoice']","[class*='invoice'] table"]:
        try:
            target.locator(sel).first.wait_for(state="visible", timeout=timeout_ms//2)
            return True
        except:
            continue
    return False

def _click_ver_mas_if_any(target, timeout_ms: int = 500) -> bool:
    try:
        btn = target.get_by_text("Ver más", exact=False).first
        btn.wait_for(state="visible", timeout=timeout_ms)
        btn.scroll_into_view_if_needed()
        btn.click(timeout=timeout_ms)
        target.wait_for_timeout(150)
        return True
    except:
        return False

def _collect_invoice_items(target) -> List[Tuple[str, str]]:
    """
    Devuelve [(href, textContent_del_enlace), ...] para cada enlace de factura
    tal como aparece en el LISTADO (sin abrirlas).
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

# =================================================
# Flujo principal (filtrando ANTES de abrir)
# =================================================
def descargar_facturas(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, str]:
    dfrom = _parse_input_date(date_from, end=False) if date_from else None
    dto   = _parse_input_date(date_to,   end=True)  if date_to   else None
    print(f"🧭 Filtro -> from: {dfrom}  to: {dto}")

    browser = None
    context = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=False, channel="chrome",
                                     args=['--disable-blink-features=AutomationControlled','--disable-dev-shm-usage'])

        ctx_dir = Path(".browser_context"); ctx_dir.mkdir(exist_ok=True)
        state_file = ctx_dir / "state.json"

        context = browser.new_context(storage_state=str(state_file) if state_file.exists() else None,
                                      accept_downloads=True)
        page = context.new_page()

        # Sesión
        has_cookies = False
        if state_file.exists():
            try:
                page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=25000)
                try: page.wait_for_load_state("networkidle", timeout=8000)
                except: pass
                u = (page.url or "").lower()
                body = ""
                try: body = page.inner_text("body").lower()
                except: pass
                if ("dashboard" in u and "login" not in u and "sign" not in u and
                    "authenticator" not in u and "can't verify" not in body):
                    has_cookies = True
            except: pass

        if not has_cookies:
            page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=25000)
            start = datetime.now()
            while (datetime.now() - start).total_seconds() < 300:
                u = (page.url or "").lower()
                if "dashboard" in u and all(k not in u for k in ("login","sign","authenticator")):
                    break
                page.wait_for_timeout(1200)
            try: context.storage_state(path=str(state_file))
            except: pass

        # Billing
        try:
            el = page.get_by_text("Billing & Invoices", exact=False).first
            el.wait_for(state="visible", timeout=6000); el.click()
        except:
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

        # Manage subscription (abre popup generalmente)
        manage_btn = None
        for txt in ["Manage subscription", "Gestionar suscripción", "Manage", "Gestionar"]:
            try:
                cand = page.get_by_text(txt, exact=False).first
                cand.wait_for(state="visible", timeout=3000); manage_btn = cand; break
            except: continue
        if not manage_btn:
            try:
                cand = page.locator('button:has-text("Manage"), a:has-text("Manage")').first
                cand.wait_for(state="visible", timeout=3000); manage_btn = cand
            except: pass
        if not manage_btn:
            raise Exception("No se encontró 'Manage subscription'.")

        new_page = None
        try:
            with page.expect_popup() as pinfo:
                manage_btn.click()
            new_page = pinfo.value
        except:
            try: manage_btn.click()
            except: page.evaluate("el => el.click()", manage_btn)
            page.wait_for_timeout(600)
            for p in context.pages[::-1]:
                uu = (p.url or "").lower()
                if any(s in uu for s in ("billing.stripe", "invoice.stripe", "/p/session", "portal")):
                    new_page = p; break
        if not new_page: new_page = page

        try: new_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except: pass
        try: new_page.wait_for_load_state("networkidle", timeout=20000)
        except: pass

        billing_frame = _find_billing_frame(new_page)
        target = billing_frame if billing_frame else new_page
        _focus_invoice_tab_if_needed(target)

        # Mostrar listado y expandir sólo lo necesario
        _wait_for_invoice_list(target, timeout_ms=8000)

        def oldest_date_in_items(items: List[Tuple[str,str]]) -> Optional[date]:
            ds = [ _parse_human_date_to_dateobj(txt) for _,txt in items ]
            ds = [d for d in ds if d]
            return min(ds) if ds else None

        all_items: List[Tuple[str,str]] = _collect_invoice_items(target)

        # Si hay rango "from", expandir hasta que la más vieja < from (o ya no haya 'Ver más')
        if dfrom:
            while True:
                od = oldest_date_in_items(all_items)
                need_more = (od is None) or (od >= dfrom)
                if not need_more:
                    break
                if not _click_ver_mas_if_any(target):
                    break
                # recargar items
                all_items = _collect_invoice_items(target)

        # Filtrar por rango sin abrir páginas
        filtered: List[Tuple[str,str]] = []
        for href, txt in all_items:
            d = _parse_human_date_to_dateobj(txt)
            if not d:
                # si no saco fecha del texto del enlace, lo dejo para el parser de la página (opcional)
                filtered.append((href, txt))
                continue
            if _in_range_day(d, dfrom, dto):
                filtered.append((href, txt))

        print(f"🧾 Enlaces totales: {len(all_items)} | En rango: {len(filtered)}")

        if not filtered:
            # Persistir cookies y salir
            try: context.storage_state(path=str(state_file))
            except: pass
            context.close(); browser.close(); pw.stop()
            return JSONResponse(status_code=200, content={
                "estado":"ok", "mensaje":"No hay facturas en el rango indicado.",
                "descargadas":0, "errores":[], "filtro":{"from":str(dfrom),"to":str(dto)}
            })

        list_url = getattr(target, "url", None) or new_page.url
        DOWNLOAD_WAIT_VISIBLE = 1200
        DOWNLOAD_EXPECT_MS   = 8000

        descargadas = 0
        errores = []

        for idx, (href, link_txt) in enumerate(filtered, 1):
            print(f"\n➡️  ({idx}/{len(filtered)}) Abriendo: {href}  | txt='{link_txt.strip()[:40]}'")
            new_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            try: new_page.wait_for_load_state("networkidle", timeout=15000)
            except: pass
            invoice_page = new_page
            invoice_page.wait_for_timeout(300)

            # Fecha fiable desde la página (para carpeta)
            def _extract_invoice_date(page) -> str:
                body = ""
                try: body = page.inner_text("body")
                except: pass
                d = _parse_human_date_to_dateobj(body) or date.today()
                return f"{d.year}_{str(d.month).zfill(2)}_{str(d.day).zfill(2)}"

            fecha_texto = _extract_invoice_date(invoice_page)
            d_real = datetime.strptime(fecha_texto, "%Y_%m_%d").date()
            if not _in_range_day(d_real, dfrom, dto):
                print(f"⏭️  (verificación) {fecha_texto} fuera de rango. Saltando descarga.")
                new_page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                try: new_page.wait_for_load_state("networkidle", timeout=15000)
                except: pass
                continue

            base = Path.home() / "Desktop" / "FACTURAS"
            dest_dir = base / f"cursor_{fecha_texto}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            ruta_archivo = dest_dir / "invoice.pdf"

            print("⬇️ Descargando…")
            download = None
            def _try_click_fast(loc):
                nonlocal download
                try: loc.wait_for(state="visible", timeout=DOWNLOAD_WAIT_VISIBLE)
                except: pass
                try:
                    with invoice_page.expect_download(timeout=DOWNLOAD_EXPECT_MS) as dlinfo:
                        loc.click(timeout=DOWNLOAD_WAIT_VISIBLE, force=True)
                    download = dlinfo.value
                    return True
                except: return False

            ok_clicked = False
            try:
                btn = invoice_page.locator("[data-testid='download-invoice-pdf-button']").first
                ok_clicked = _try_click_fast(btn)
            except: pass
            if not ok_clicked:
                for txt in ["Descargar factura","Download invoice","Download","Descargar","Descargar PDF"]:
                    try:
                        btn = invoice_page.get_by_text(txt, exact=False).first
                        if _try_click_fast(btn): ok_clicked = True; break
                    except: continue
            if not ok_clicked:
                for sel in ['button:has-text("Descargar factura")','a:has-text("Descargar factura")',
                            'button:has-text("Download")','a:has-text("Download")',
                            "[data-testid='download-invoice-receipt-pdf-button']"]:
                    try:
                        btn = invoice_page.locator(sel).first
                        if _try_click_fast(btn): ok_clicked = True; break
                    except: continue

            if not download:
                msg = "No se pudo iniciar la descarga (timeout rápido)."
                print("❌ " + msg); errores.append({"href": href, "error": msg})
            else:
                try:
                    download.save_as(str(ruta_archivo))
                except Exception as e:
                    try:
                        tmp = download.path()
                        Path(tmp).replace(ruta_archivo)
                    except:
                        errores.append({"href": href, "error": str(e)})
                print(f"✅ Guardada en: {ruta_archivo}")
                descargadas += 1

            # Volver al listado sólo si quedan más
            if idx < len(filtered):
                new_page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                try: new_page.wait_for_load_state("networkidle", timeout=15000)
                except: pass

        try: context.storage_state(path=str(state_file))
        except: pass
        context.close(); browser.close(); pw.stop()

        return JSONResponse(status_code=200, content={
            "estado":"exitoso",
            "mensaje":f"Descargadas {descargadas} factura(s) dentro del rango.",
            "descargadas":descargadas,
            "errores":errores,
            "filtro":{"from":str(dfrom),"to":str(dto)},
            "carpeta_base": str((Path.home()/ "Desktop" / "FACTURAS").absolute())
        })

    except Exception as e:
        err = f"Error al descargar facturas: {str(e)}"
        print("\n❌ " + err)
        raise Exception(err)

# ============ FastAPI ============
class DateRange(BaseModel):
    date_from: Optional[str] = None
    date_to:   Optional[str] = None

@app.get("/")
async def root():
    return {"mensaje":"API de Facturas de Cursor","endpoints":{"descargar_facturas":"/api/facturas/descargar"}}

@app.post("/api/facturas/descargar")
async def descargar_facturas_endpoint(payload: DateRange):
    try:
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, descargar_facturas, payload.date_from, payload.date_to)
        return resultado
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al descargar facturas: {str(e)}")

# ============ CLI ============
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Descargar facturas de Cursor con filtro de fechas opcional.")
    parser.add_argument("--from", "-f", dest="date_from", help="Fecha desde (YYYY-MM o YYYY-MM-DD)", default=None)
    parser.add_argument("--to", "-t", dest="date_to",   help="Fecha hasta (YYYY-MM o YYYY-MM-DD)", default=None)
    parser.add_argument("--api", action="store_true",   help="Arranca API FastAPI en vez de ejecutar la descarga directa")
    args = parser.parse_args()

    if args.api:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        out = descargar_facturas(args.date_from, args.date_to)
        try:
            print(out.body.decode("utf-8"))
        except:
            print(out)
