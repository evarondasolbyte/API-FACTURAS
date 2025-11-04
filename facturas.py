from datetime import datetime
from pathlib import Path
from typing import Dict
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import uvicorn
import re
import asyncio
import os

app = FastAPI(title="API de Facturas Cursor", description="API para descargar facturas de Cursor.com")

# ------------------------- Utilidades -------------------------
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
    """Empujón de scroll muy rápido para forzar lazy-load sin bucles largos."""
    try:
        page_or_frame.evaluate("""
            () => {
                const el = document.scrollingElement || document.documentElement || document.body;
                el.scrollTo(0, el.scrollTop + 3000);
            }
        """)
    except:
        pass
    page_or_frame.wait_for_timeout(120)

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
            target.wait_for_timeout(250)
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

def _find_first_invoice_href_fast(target):
    """
    Devuelve el primer href de factura (invoice.stripe.com/i/...) sin scrollear.
    Si no aparece a la primera, pulsa “Ver más” y hace un mini scroll rápido.
    """
    def _grab():
        try:
            return target.evaluate("""
                () => {
                    const as = Array.from(document.querySelectorAll("a[href*='invoice.stripe.com/i/']"));
                    return as.length ? as[0].href : null;
                }
            """)
        except:
            return None

    href = _grab()
    if href:
        return href

    # Intento 1: 'Ver más'
    try:
        btn = target.get_by_text("Ver más", exact=False).first
        btn.click(timeout=800)
        target.wait_for_timeout(250)
        href = _grab()
        if href:
            return href
    except:
        pass

    # Intento 2: mini scroll rápido y reintento
    _quick_scroll(target)
    href = _grab()
    if href:
        return href

    # Intento 3: otro 'Ver más' si existe
    try:
        btn = target.get_by_text("Ver más", exact=False).first
        btn.click(timeout=800)
        target.wait_for_timeout(250)
        href = _grab()
        if href:
            return href
    except:
        pass

    return None

def _extract_invoice_date(page) -> str:
    """
    Devuelve 'YYYY_MM_DD' a partir del contenido visible en la factura.
    Soporta:
      - 25 de octubre de 2025
      - 25 oct 2025
      - October 25, 2025
      - 25/10/2025 y 2025-10-25
    Si no encuentra fecha válida, devuelve la fecha de hoy.
    """
    def _norm(s: str) -> str:
        return (s or "").strip().lower()\
            .replace("á","a").replace("é","e").replace("í","i")\
            .replace("ó","o").replace("ú","u").replace("ç","c")

    month_map = {
        "ene":1, "enero":1, "feb":2, "febrero":2, "mar":3, "marzo":3, "abr":4, "abril":4,
        "may":5, "mayo":5, "jun":6, "junio":6, "jul":7, "julio":7, "ago":8, "agosto":8,
        "sep":9, "sept":9, "septi":9, "septiembre":9, "set":9, "setiembre":9,
        "oct":10, "octubre":10, "nov":11, "noviembre":11, "dic":12, "diciembre":12,
        "jan":1, "january":1, "february":2, "mar":3, "march":3, "apr":4, "april":4,
        "may":5, "june":6, "jun":6, "july":7, "jul":7, "aug":8, "august":8,
        "september":9, "october":10, "november":11, "dec":12, "december":12,
    }

    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except:
        pass
    page.wait_for_timeout(300)

    try:
        body = page.inner_text("body")
    except:
        body = ""
    text = _norm(body)

    def _fmt(y:int,m:int,d:int) -> str | None:
        if 2020 <= y <= 2035 and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y}_{str(m).zfill(2)}_{str(d).zfill(2)}"
        return None

    # 1) Español largo
    m = re.search(r"(\d{1,2})\s+de\s+([a-zñ]+)\s+de\s+(\d{4})", text)
    if m:
        d, mon, y = m.groups()
        mon = month_map.get(mon, None)
        if mon:
            out = _fmt(int(y), mon, int(d))
            if out: return out

    # 2) Español corto
    m = re.search(r"(\d{1,2})\s+([a-zñ]+)\s+(\d{4})", text)
    if m:
        d, mon, y = m.groups()
        mon = month_map.get(mon, None)
        if mon:
            out = _fmt(int(y), mon, int(d))
            if out: return out

    # 3) Inglés
    m = re.search(r"([a-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
    if m:
        mon, d, y = m.groups()
        mon = month_map.get(mon, None)
        if mon:
            out = _fmt(int(y), mon, int(d))
            if out: return out

    # 4) Numéricos YYYY-MM-DD
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        y, mth, d = map(int, m.groups())
        out = _fmt(y, mth, d)
        if out: return out

    # 5) Numéricos DD/MM/YYYY
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if m:
        d, mth, y = map(int, m.groups())
        out = _fmt(y, mth, d)
        if out: return out

    # Fallback: hoy
    return datetime.now().strftime("%Y_%m_%d")

# ------------------------- Flujo principal -------------------------
def descargar_factura() -> Dict[str, str]:
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()

        print("🌐 Lanzando Chrome no persistente...\n")
        browser = playwright.chromium.launch(
            headless=False,
            channel="chrome",
            args=['--disable-blink-features=AutomationControlled','--disable-dev-shm-usage']
        )

        # Contexto con cookies si existen
        browser_context_path = Path(".browser_context")
        browser_context_path.mkdir(exist_ok=True)
        state_file = browser_context_path / "state.json"

        context = browser.new_context(
            storage_state=str(state_file) if state_file.exists() else None,
            accept_downloads=True,
        )
        page = context.new_page()

        # ---- Login / sesión
        has_cookies = False
        if state_file.exists():
            try:
                print("📋 Verificando sesión con cookies guardadas...")
                page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=25000)
                try: page.wait_for_load_state("networkidle", timeout=8000)
                except: pass
                url = (page.url or "").lower()
                body = ""
                try: body = page.inner_text("body").lower()
                except: pass
                if ("dashboard" in url and "login" not in url and "sign" not in url and "authenticator" not in url and "can't verify" not in body):
                    has_cookies = True
                    print("✅ Sesión válida detectada.\n")
            except Exception as e:
                print(f"⚠ Error verificando cookies: {e}")

        if not has_cookies:
            print("🔐 Inicio de sesión manual requerido.")
            page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=25000)
            start = datetime.now()
            while (datetime.now() - start).total_seconds() < 300:
                url = (page.url or "").lower()
                if "dashboard" in url and all(k not in url for k in ("login","sign","authenticator")):
                    break
                page.wait_for_timeout(1500)
            try:
                context.storage_state(path=str(state_file))
                print("💾 Cookies guardadas.\n")
            except:
                pass

        # ---- Dashboard listo
        print("📂 Confirmando dashboard…")
        page.goto("https://cursor.com/dashboard", wait_until="networkidle", timeout=25000)
        if any(k in (page.url or "").lower() for k in ["login","sign","authenticator"]):
            raise Exception("No se ha completado el login.")
        print("✅ Dashboard listo.\n")

        # ---- Billing & Invoices
        print("🔍 Abriendo 'Billing & Invoices'…")
        abierto = False
        try:
            el = page.get_by_text("Billing & Invoices", exact=False).first
            el.wait_for(state="visible", timeout=6000)
            el.click()
            abierto = True
        except:
            try:
                ok = page.evaluate("""
                    () => {
                        const txt = 'Billing & Invoices';
                        for (const el of document.querySelectorAll('a,button,[role="link"],[role="button"]')) {
                            if (el.innerText && el.innerText.includes(txt)) { el.click(); return true; }
                        }
                        return false;
                    }
                """)
                if ok: abierto = True
            except:
                pass
        if not abierto:
            raise Exception("No se encontró 'Billing & Invoices'.")

        page.wait_for_load_state("networkidle", timeout=15000)

        # ---- Manage subscription
        print("🧭 Abriendo 'Manage subscription'…")
        manage_btn = None
        for txt in ["Manage subscription", "Gestionar suscripción", "Manage", "Gestionar"]:
            try:
                cand = page.get_by_text(txt, exact=False).first
                cand.wait_for(state="visible", timeout=3000)
                manage_btn = cand
                break
            except:
                continue
        if not manage_btn:
            try:
                cand = page.locator('button:has-text("Manage"), a:has-text("Manage")').first
                cand.wait_for(state="visible", timeout=3000)
                manage_btn = cand
            except:
                pass
        if not manage_btn:
            raise Exception("No se encontró 'Manage subscription'.")

        # Preferimos MISMA pestaña
        try:
            manage_btn.click()
        except:
            page.evaluate("el => el.click()", manage_btn)
        page.wait_for_timeout(1200)

        new_page = context.pages[-1]  # misma o nueva: la última
        try: new_page.bring_to_front()
        except: pass
        try: new_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except: pass
        try: new_page.wait_for_load_state("networkidle", timeout=20000)
        except: pass

        # ---- Dentro del portal
        billing_frame = _find_billing_frame(new_page)
        target = billing_frame if billing_frame else new_page
        _focus_invoice_tab_if_needed(target)

        # ======= BLOQUE RÁPIDO PARA ENCONTRAR LA FACTURA =======
        print("⚡ Buscando enlace de factura sin scroll pesado…")
        href = _find_first_invoice_href_fast(target)
        if not href:
            # último recurso rápido: mini scroll + detección
            _quick_scroll(target)
            _wait_for_invoice_list(target, timeout_ms=3000)
            href = _find_first_invoice_href_fast(target)

        if not href:
            # como fallback extremo, un scroll algo más profundo pero corto
            _auto_scroll_until_bottom(target, step_px=1800, max_tries=10, pause_ms=120)
            href = _find_first_invoice_href_fast(target)

        if not href:
            raise Exception("No se encontraron enlaces directos de facturas (invoice.stripe.com).")

        # Abrir factura en la MISMA pestaña
        print("🖱️ Abriendo la factura en la misma pestaña con goto()…")
        new_page.goto(href, wait_until="domcontentloaded", timeout=30000)
        try: new_page.wait_for_load_state("networkidle", timeout=15000)
        except: pass

        invoice_page = new_page
        invoice_page.wait_for_timeout(800)

        # ---- Fecha para la carpeta
        print("🗓️ Extrayendo fecha de la factura…")
        fecha_texto = _extract_invoice_date(invoice_page)

        # ---- Carpeta destino
        desktop_path = Path.home() / "Desktop"
        facturas_dir = desktop_path / "FACTURAS"
        facturas_dir.mkdir(exist_ok=True)
        directorio_destino = facturas_dir / f"cursor_{fecha_texto}"
        directorio_destino.mkdir(exist_ok=True)
        ruta_archivo = directorio_destino / "invoice.pdf"
        print(f"📁 Directorio de factura: {directorio_destino}")

        # ---- Click en “Descargar factura” (ACELERADO)
        print("⬇️ Pulsando 'Descargar factura' (rápido)…")
        DOWNLOAD_WAIT_VISIBLE = 1500   # 1.5s para que aparezca
        DOWNLOAD_EXPECT_MS   = 8000    # 8s para que arranque la descarga

        download = None

        def _try_click_fast(loc):
            """Click inmediato/forzado y esperar descarga con timeout corto."""
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
            except Exception:
                return False

        # 1) data-testid oficial de Stripe
        try:
            btn = invoice_page.locator("[data-testid='download-invoice-pdf-button']").first
            if not _try_click_fast(btn):
                pass
        except Exception:
            pass

        # 2) Texto visible (fallback rápido)
        if not download:
            for txt in ["Descargar factura", "Download invoice", "Download", "Descargar", "Descargar PDF"]:
                try:
                    btn = invoice_page.get_by_text(txt, exact=False).first
                    if _try_click_fast(btn):
                        break
                except Exception:
                    continue

        # 3) Selectores genéricos (último recurso)
        if not download:
            for sel in [
                'button:has-text("Descargar factura")',
                'a:has-text("Descargar factura")',
                'button:has-text("Download")',
                'a:has-text("Download")',
                "[data-testid='download-invoice-receipt-pdf-button']"  # recibo si el otro falla
            ]:
                try:
                    btn = invoice_page.locator(sel).first
                    if _try_click_fast(btn):
                        break
                except Exception:
                    continue

        if not download:
            raise Exception("No se pudo iniciar la descarga del PDF de la factura (timeout rápido).")

        # Guardar archivo
        print(f"💾 Guardando en: {ruta_archivo}")
        try:
            download.save_as(str(ruta_archivo))
        except Exception as e:
            temp_path = None
            try:
                temp_path = download.path()
            except:
                pass
            if not temp_path or not Path(temp_path).exists():
                raise e
            Path(temp_path).replace(ruta_archivo)
        print("✅ Factura guardada correctamente.")

        # Persistir cookies
        try:
            context.storage_state(path=str(state_file))
        except:
            pass

        context.close()
        if browser:
            browser.close()
        playwright.stop()

        return {
            "estado": "exitoso",
            "mensaje": "Factura descargada exitosamente",
            "ruta": str(ruta_archivo.absolute()),
            "directorio": str(directorio_destino.absolute()),
            "carpeta": "Escritorio/FACTURAS"
        }

    except Exception as e:
        err = f"Error al descargar la factura: {str(e)}"
        print(f"\n❌ {err}")
        print("⚠ El navegador permanece abierto para inspección.")
        raise Exception(err)

# ------------------------- FastAPI -------------------------
@app.get("/")
async def root():
    return {"mensaje": "API de Facturas de Cursor", "endpoints": {"descargar_factura": "/api/factura/descargar"}}

@app.post("/api/factura/descargar")
async def descargar_factura_endpoint():
    try:
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, descargar_factura)
        return JSONResponse(status_code=200, content=resultado)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al descargar la factura: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
