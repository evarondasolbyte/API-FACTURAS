from datetime import datetime
from pathlib import Path
from typing import Dict
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright
import uvicorn
import re
import asyncio
import os

app = FastAPI(title="API de Facturas Cursor", description="API para descargar facturas de Cursor.com")

# =========================
# Utilidades de portal/scroll
# =========================
def _auto_scroll_until_bottom(page_or_frame, *, step_px: int = 1200, max_tries: int = 40, pause_ms: int = 250):
    """Desplaza de forma incremental hasta el final del documento o del contenedor principal scrollable."""
    # 1) documento principal
    for _ in range(max_tries):
        reached = page_or_frame.evaluate("""
            () => {
                const el = document.scrollingElement || document.documentElement || document.body;
                const before = el.scrollTop;
                el.scrollBy(0, 1200);
                const after = el.scrollTop;
                const nearBottom = (el.scrollHeight - (after + el.clientHeight)) < 5;
                return { advanced: after > before, nearBottom };
            }
        """)
        page_or_frame.wait_for_timeout(pause_ms)
        if reached.get("nearBottom"):
            break

    # 2) contenedor scrollable más grande
    try:
        handle = page_or_frame.evaluate_handle("""
            () => {
                let best = null, bestArea = 0;
                for (const el of document.querySelectorAll('*')) {
                    const st = getComputedStyle(el);
                    const scrollableY = ['auto','scroll'].includes(st.overflowY);
                    if (scrollableY && el.scrollHeight > el.clientHeight + 10) {
                        const area = el.clientWidth * el.clientHeight;
                        if (area > bestArea) { best = el; bestArea = area; }
                    }
                }
                return best;
            }
        """)
        if handle:
            for _ in range(max_tries // 2):
                page_or_frame.evaluate("""el => { el.scrollTop = Math.min(el.scrollTop + 2000, el.scrollHeight); }""", handle)
                page_or_frame.wait_for_timeout(pause_ms)
                at_end = page_or_frame.evaluate("""el => (el.scrollHeight - (el.scrollTop + el.clientHeight)) < 5""", handle)
                if at_end:
                    break
            try:
                handle.dispose()
            except:
                pass
    except:
        pass

    # 3) algunos portales reaccionan mejor a PageDown
    try:
        for _ in range(8):
            page_or_frame.keyboard.press("PageDown")
            page_or_frame.wait_for_timeout(120)
    except:
        pass


def _find_billing_frame(page):
    """Devuelve el Frame del portal de facturación si existe (Stripe/portal/billing), o None."""
    for fr in page.frames:
        u = (fr.url or "").lower()
        if any(k in u for k in ["stripe", "billing", "portal"]):
            return fr
        try:
            if fr.get_by_text(re.compile(r"(invoice|invoices|factura|facturas|history|historial)", re.I)).first.wait_for(state="visible", timeout=1000):
                return fr
        except:
            pass
    return None


def _focus_invoice_tab_if_needed(target):
    """Intenta cambiar a la pestaña/listado de facturas si el portal usa tabs."""
    candidates = [
        "Invoices", "Invoice history", "Billing history", "Payments", "Statements",
        "Facturas", "Historial de facturas", "Historial", "Pagos", "Extractos",
        "View all invoices", "Ver todas las facturas"
    ]
    for txt in candidates:
        try:
            el = target.get_by_text(txt, exact=False).first
            el.wait_for(state="visible", timeout=1500)
            el.scroll_into_view_if_needed()
            try:
                el.click()
                target.wait_for_timeout(500)
            except:
                pass
            return True
        except:
            continue
    # Fallback: tabs por rol
    try:
        tab = target.locator("[role='tab']:has-text('Invoice'), [role='tab']:has-text('Factur'), [data-testid*='invoice']").first
        tab.wait_for(state="visible", timeout=1200)
        tab.click()
        target.wait_for_timeout(400)
        return True
    except:
        return False


def wait_for_invoice_history(target, timeout_ms: int = 15000):
    """Espera a que indicadores de historial/listado de facturas estén visibles."""
    candidates = [
        "Invoice history", "Invoices", "Download invoice", "View invoice",
        "Historial de facturas", "Facturas", "Descargar factura", "Ver factura",
        "Payment history", "Billing history", "Pagos", "Historial"
    ]
    for txt in candidates:
        try:
            target.get_by_text(txt, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except:
            continue
    # Tabla o grid
    for sel in ["table", "[role='table']", "tbody tr", "[data-testid*='invoice']", "[class*='invoice'] table"]:
        try:
            target.locator(sel).first.wait_for(state="visible", timeout=timeout_ms//2)
            return True
        except:
            continue
    return False


# =========================
# Flujo principal
# =========================
def descargar_factura() -> Dict[str, str]:
    """
    Descarga una factura desde Cursor.com abriendo el portal de suscripción,
    navegando al historial y guardando el PDF en Desktop/FACTURAS/cursor_YYYY_MM_DD/invoice.pdf.
    """
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()

        # Chrome no persistente
        print("🌐 Lanzando Chrome no persistente...\n")
        browser = playwright.chromium.launch(
            headless=False,
            channel="chrome",
            args=['--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage']
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

        # Verificar/obtener sesión
        has_cookies = False
        if state_file.exists():
            try:
                print("📋 Verificando sesión con cookies guardadas...")
                page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except:
                    pass
                url = (page.url or "").lower()
                body = ""
                try:
                    body = page.inner_text("body").lower()
                except:
                    pass
                if ("dashboard" in url and "login" not in url and "sign" not in url and "authenticator" not in url and "can't verify" not in body):
                    has_cookies = True
                    print("✅ Sesión válida detectada.\n")
            except Exception as e:
                print(f"⚠ Error verificando cookies: {e}\n")

        if not has_cookies:
            print("🔐 Inicio de sesión manual requerido.")
            page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            start = datetime.now()
            while (datetime.now() - start).total_seconds() < 300:
                url = (page.url or "").lower()
                if ("dashboard" in url and "login" not in url and "sign" not in url and "authenticator" not in url):
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=2500)
                    except:
                        pass
                    break
                page.wait_for_timeout(1500)
            try:
                context.storage_state(path=str(state_file))
                print("💾 Cookies guardadas.\n")
            except:
                pass

        # Confirmar dashboard
        print("📂 Confirmando dashboard…")
        page.goto("https://cursor.com/dashboard", wait_until="networkidle", timeout=20000)
        url = (page.url or "").lower()
        if any(k in url for k in ["login", "sign", "authenticator"]):
            raise Exception("No se ha completado el login.")
        print("✅ Dashboard listo.\n")

        # Abrir Billing & Invoices
        print("🔍 Abriendo 'Billing & Invoices'…")
        abierto = False
        try:
            el = page.get_by_text("Billing & Invoices", exact=False).first
            el.wait_for(state="visible", timeout=5000)
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            el.click()
            abierto = True
        except:
            try:
                ok = page.evaluate("""
                    () => {
                        const txt = 'Billing & Invoices';
                        for (const el of document.querySelectorAll('a,button,[role="link"],[role="button"]')) {
                            if (el.innerText && el.innerText.includes(txt)) {
                                el.scrollIntoView({behavior:'auto',block:'center'});
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if ok:
                    abierto = True
            except:
                pass
        if not abierto:
            raise Exception("No se encontró 'Billing & Invoices'.")
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(800)

        # Abrir Manage subscription (posible nueva pestaña)
        print("🧭 Abriendo 'Manage subscription'…")
        manage_btn = None
        for txt in ["Manage subscription", "Gestionar suscripción", "Manage", "Gestionar"]:
            try:
                cand = page.get_by_text(txt, exact=False).first
                cand.wait_for(state="visible", timeout=2500)
                manage_btn = cand
                break
            except:
                continue
        if not manage_btn:
            try:
                cand = page.locator('button:has-text("Manage"), a:has-text("Manage")').first
                cand.wait_for(state="visible", timeout=2500)
                manage_btn = cand
            except:
                pass
        if not manage_btn:
            raise Exception("No se encontró 'Manage subscription'.")

        try:
            with context.expect_page(timeout=8000) as new_tab:
                manage_btn.scroll_into_view_if_needed()
                page.wait_for_timeout(150)
                manage_btn.click()
            new_page = new_tab.value
        except:
            page.wait_for_timeout(2500)
            new_page = context.pages[-1] if len(context.pages) > 1 else page
        try:
            new_page.bring_to_front()
        except:
            pass

        # Esperar portal
        new_page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            new_page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass
        new_page.wait_for_timeout(600)

        # Si hay iframe de portal, trabajar dentro
        billing_frame = _find_billing_frame(new_page)
        target = billing_frame if billing_frame else new_page

        # Ir a listado si aplica
        _focus_invoice_tab_if_needed(target)

        # Scroll hasta historial/listado
        print("📜 Haciendo scroll hasta historial/listado de facturas…")
        _auto_scroll_until_bottom(target, step_px=1500, max_tries=50, pause_ms=180)
        if not wait_for_invoice_history(target, timeout_ms=6000):
            _auto_scroll_until_bottom(target, step_px=2000, max_tries=30, pause_ms=180)
        print("✅ Historial de facturas visible (o muy cerca).")

        # Abrir SIEMPRE enlace real de factura de Stripe
        print("🔎 Localizando enlaces de factura (invoice.stripe.com/i/)...")
        invoice_links = target.locator("a[href*='invoice.stripe.com/i/']")
        try:
            link_count = invoice_links.count()
        except:
            link_count = 0
        if link_count == 0:
            invoice_links = target.locator("a[data-testid='hip-link'], a[href*='invoice.stripe.com/']")
            try:
                link_count = invoice_links.count()
            except:
                link_count = 0
        if link_count == 0:
            raise Exception("No se encontraron enlaces directos de facturas (invoice.stripe.com).")

        first_invoice = invoice_links.first
        first_invoice.scroll_into_view_if_needed()
        target.wait_for_timeout(150)

        print("🖱️ Abriendo la factura en nueva pestaña…")
        invoice_page = None
        try:
            with new_page.context.expect_page(timeout=10000) as pinfo:
                first_invoice.click()
            candidate = pinfo.value
            candidate.wait_for_load_state("domcontentloaded", timeout=15000)
            if "invoice.stripe.com" in (candidate.url or "") and "/i/" in candidate.url:
                invoice_page = candidate
            else:
                for p in new_page.context.pages:
                    u = (p.url or "").lower()
                    if "invoice.stripe.com" in u and "/i/" in u:
                        invoice_page = p
                        break
        except:
            for p in new_page.context.pages:
                u = (p.url or "").lower()
                if "invoice.stripe.com" in u and "/i/" in u:
                    invoice_page = p
                    break
        if not invoice_page:
            raise Exception("No se abrió la pestaña de la factura (invoice.stripe.com/i/).")

        try:
            invoice_page.bring_to_front()
        except:
            pass
        invoice_page.wait_for_load_state("networkidle", timeout=15000)
        invoice_page.wait_for_timeout(800)

        # Extraer fecha para carpeta
        print("🗓️ Extrayendo fecha de la factura…")
        fecha_factura = None
        fecha_texto = None
        try:
            invoice_page.wait_for_load_state("domcontentloaded", timeout=5000)
            body = ""
            try:
                body = invoice_page.inner_text("body")
            except:
                pass
            patrones = [
                r'(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})',
                r'(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})',
            ]
            for patron in patrones:
                for match in re.findall(patron, body):
                    if len(match[0]) == 4:
                        año, mes, dia = match
                    else:
                        dia, mes, año = match
                    try:
                        año_int = int(año); mes_int = int(mes); dia_int = int(dia)
                        if 2020 <= año_int <= 2030 and 1 <= mes_int <= 12 and 1 <= dia_int <= 31:
                            fecha_factura = datetime(año_int, mes_int, dia_int)
                            fecha_texto = f"{año}_{str(mes).zfill(2)}_{str(dia).zfill(2)}"
                            break
                    except:
                        continue
                if fecha_factura:
                    break
        except:
            pass
        if not fecha_factura:
            fecha_factura = datetime.now()
            fecha_texto = fecha_factura.strftime('%Y_%m_%d')

        # Preparar carpeta destino
        nombre_directorio = f"cursor_{fecha_texto}"
        desktop_path = Path.home() / "Desktop"
        facturas_dir = desktop_path / "FACTURAS"
        facturas_dir.mkdir(exist_ok=True)
        directorio_destino = facturas_dir / nombre_directorio
        directorio_destino.mkdir(exist_ok=True)
        print(f"📁 Directorio de factura: {directorio_destino}")

        # ===== CLICK EXACTO EN "Descargar factura" =====
        print("⬇️ Pulsando 'Descargar factura'…")
        download = None

        # 1) Selector más fiable de Stripe (de tu captura): data-testid="download-invoice-pdf-button"
        try:
            boton = invoice_page.locator("[data-testid='download-invoice-pdf-button']").first
            boton.wait_for(state="visible", timeout=8000)
            boton.scroll_into_view_if_needed()
            invoice_page.wait_for_timeout(150)
            with invoice_page.expect_download(timeout=20000) as download_info:
                boton.click()
            download = download_info.value
            print("✅ Descarga iniciada con data-testid=download-invoice-pdf-button")
        except:
            pass

        # 2) Fallbacks por texto
        if not download:
            for texto in ["Descargar factura", "Download invoice", "Descargar PDF", "Download"]:
                try:
                    boton = invoice_page.get_by_text(texto, exact=False).first
                    boton.wait_for(state="visible", timeout=5000)
                    boton.scroll_into_view_if_needed()
                    invoice_page.wait_for_timeout(150)
                    with invoice_page.expect_download(timeout=20000) as download_info:
                        boton.click()
                    download = download_info.value
                    print(f"✅ Descarga iniciada: {texto}")
                    break
                except:
                    continue

        # 3) Fallback selectores genéricos
        if not download:
            for sel in [
                'button:has-text("Download")',
                'a:has-text("Download")',
                'button:has-text("Descargar")',
                'a:has-text("Descargar")',
                'a[href*=".pdf"]',
                'a[href*="pdf"]',
            ]:
                try:
                    boton = invoice_page.locator(sel).first
                    boton.wait_for(state="visible", timeout=4000)
                    boton.scroll_into_view_if_needed()
                    invoice_page.wait_for_timeout(120)
                    with invoice_page.expect_download(timeout=20000) as download_info:
                        boton.click()
                    download = download_info.value
                    print(f"✅ Descarga iniciada (selector): {sel}")
                    break
                except:
                    continue

        if not download:
            raise Exception("No se pudo iniciar la descarga automáticamente. Pulsa manualmente 'Descargar factura' en la pestaña de la factura.")

        # Guardar PDF con el nombre indicado
        ruta_archivo = directorio_destino / "invoice.pdf"
        print(f"💾 Guardando en: {ruta_archivo}")
        download.save_as(ruta_archivo)
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
        error_msg = f"Error al descargar la factura: {str(e)}"
        print(f"\n❌ {error_msg}")
        print("⚠ El navegador permanece abierto para inspección.")
        raise Exception(error_msg)


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
