from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright, Page, Frame, BrowserContext, Download, Response
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
                page_or_frame.evaluate("el => { el.scrollTop = Math.min(el.scrollTop + 2000, el.scrollHeight); }", handle)
                page_or_frame.wait_for_timeout(pause_ms)
                at_end = page_or_frame.evaluate("(el) => (el.scrollHeight - (el.scrollTop + el.clientHeight)) < 5", handle)
                if at_end:
                    break
            try:
                handle.dispose()
            except:
                pass
    except:
        pass

    try:
        for _ in range(8):
            page_or_frame.keyboard.press("PageDown")
            page_or_frame.wait_for_timeout(120)
    except:
        pass


def _find_billing_frame(page: Page) -> Optional[Frame]:
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
    for sel in ["table", "[role='table']", "tbody tr", "[data-testid*='invoice']", "[class*='invoice'] table"]:
        try:
            target.locator(sel).first.wait_for(state="visible", timeout=timeout_ms//2)
            return True
        except:
            continue
    return False


# =========================
# Descarga robusta del PDF en Stripe
# =========================
def _save_bytes(path: Path, data: bytes):
    path.write_bytes(data)

def _download_via_request(context: BrowserContext, url: str, destino: Path) -> bool:
    try:
        resp = context.request.get(url)
        if resp.ok:
            _save_bytes(destino, resp.body())
            return True
    except:
        pass
    return False

def download_invoice_pdf(invoice_page: Page, context: BrowserContext, destino: Path) -> None:
    """
    Pulsa 'Descargar factura' y guarda el PDF en 'destino'.
    Maneja 3 casos:
      1) Evento de descarga nativo (expect_download)
      2) Apertura de nueva pestaña con PDF (expect_popup / URL .pdf)
      3) Enlace directo <a href="...pdf"> (descarga por request)
    """
    # Asegurar carga
    try:
        invoice_page.wait_for_load_state("networkidle", timeout=15000)
    except:
        pass
    invoice_page.wait_for_timeout(500)

    # Localizar el botón de descarga (varias variantes)
    locators_to_try = [
        ("text", "Descargar factura", True),
        ("text", "Descargar factura", False),
        ("css" , "[data-testid='download-invoice-pdf-button']", None),
        ("text", "Download invoice", False),
        ("text", "Descargar PDF", False),
        ("css" , 'button:has-text("Descargar factura")', None),
        ("css" , 'a:has-text("Descargar factura")', None),
        ("css" , 'button:has-text("Download")', None),
        ("css" , 'a:has-text("Download")', None),
    ]

    boton = None
    for kind, query, exact in locators_to_try:
        try:
            if kind == "text":
                cand = invoice_page.get_by_text(query, exact=exact).first
            else:
                cand = invoice_page.locator(query).first
            cand.wait_for(state="visible", timeout=6000)
            boton = cand
            break
        except:
            continue

    if boton is None:
        # Como último recurso, busca un link al PDF en el DOM
        anchor_pdf = None
        try:
            anchor_pdf = invoice_page.locator("a[href$='.pdf'], a[href*='.pdf']").first
            anchor_pdf.wait_for(state="visible", timeout=3000)
            pdf_url = anchor_pdf.get_attribute("href") or ""
            if pdf_url:
                if not _download_via_request(context, pdf_url, destino):
                    # intenta hacer clic, por si dispara descarga nativa
                    with invoice_page.expect_download(timeout=15000) as di:
                        anchor_pdf.click()
                    di.value.save_as(destino)
                return
        except:
            pass
        raise Exception("No se encontró el botón/enlace de 'Descargar factura'.")

    # Intento A: evento de descarga nativo + popup simultáneo
    download_obj: Optional[Download] = None
    pdf_new_page: Optional[Page] = None
    with invoice_page.expect_event("download", timeout=1_000, predicate=lambda d: True) as maybe_download:
        with invoice_page.expect_event("popup", timeout=5_000, predicate=lambda p: True) as maybe_popup:
            try:
                boton.scroll_into_view_if_needed()
            except:
                pass
            invoice_page.wait_for_timeout(150)
            try:
                boton.click()
            except:
                # Forzar click JS si el overlay de Stripe estorba
                try:
                    invoice_page.evaluate("(el)=>el.click()", boton)
                except:
                    pass
        try:
            pdf_new_page = maybe_popup.value
        except:
            pdf_new_page = None
        try:
            download_obj = maybe_download.value
        except:
            download_obj = None

    # Caso 1: descarga nativa
    if download_obj:
        download_obj.save_as(destino)
        return

    # Caso 2: hubo popup o navegación a PDF
    candidate_pages = [p for p in context.pages]
    if pdf_new_page and pdf_new_page not in candidate_pages:
        candidate_pages.append(pdf_new_page)

    # Espera pequeñas navegaciones y busca URL a PDF
    for p in candidate_pages:
        try:
            p.wait_for_load_state("domcontentloaded", timeout=5000)
        except:
            pass

    # 2a) URL directa .pdf
    for p in candidate_pages:
        url = (p.url or "").lower()
        if url.endswith(".pdf") or (".pdf?" in url):
            if not _download_via_request(context, url, destino):
                # En algunos casos sí dispara un download event si re-clickas
                try:
                    with p.expect_download(timeout=8000) as di:
                        pass
                except:
                    pass
            else:
                return

    # 2b) Si no tenemos .pdf aún, escuchar respuesta con content-type PDF
    try:
        resp: Response = invoice_page.wait_for_event(
            "response",
            timeout=8000,
            predicate=lambda r: ("application/pdf" in (r.headers.get("content-type","").lower()))
        )
        pdf_url = resp.url
        if not _download_via_request(context, pdf_url, destino):
            # fallback: intenta abrir la URL en una nueva pestaña y esperar download
            tmp = context.new_page()
            tmp.goto(pdf_url, wait_until="load")
            try:
                with tmp.expect_download(timeout=8000) as di:
                    pass
                di.value.save_as(destino)
                tmp.close()
                return
            except:
                tmp.close()
                pass
        else:
            return
    except:
        pass

    # 2c) Último recurso: reintentar localizar <a href="...pdf">
    try:
        anchor_pdf = invoice_page.locator("a[href$='.pdf'], a[href*='.pdf']").first
        anchor_pdf.wait_for(state="visible", timeout=3000)
        pdf_url = anchor_pdf.get_attribute("href") or ""
        if pdf_url:
            if not _download_via_request(context, pdf_url, destino):
                with invoice_page.expect_download(timeout=12000) as di:
                    anchor_pdf.click()
                di.value.save_as(destino)
            return
    except:
        pass

    raise Exception("Se pulsó 'Descargar factura', pero no se pudo capturar el PDF (ni download ni URL).")


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
            with context.expect_page(timeout=12000) as new_tab:
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
        new_page.wait_for_load_state("domcontentloaded", timeout=20000)
        try:
            new_page.wait_for_load_state("networkidle", timeout=20000)
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

        # Abrir enlace de invoice.stripe.com/i/
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
            with new_page.context.expect_page(timeout=15000) as pinfo:
                first_invoice.click()
            candidate = pinfo.value
            candidate.wait_for_load_state("domcontentloaded", timeout=20000)
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
        try:
            invoice_page.wait_for_load_state("networkidle", timeout=20000)
        except:
            pass
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

        destino_pdf = directorio_destino / "invoice.pdf"

        # CLICK & DESCARGA ROBUSTA
        print("⬇️ Pulsando 'Descargar factura' y capturando PDF…")
        download_invoice_pdf(invoice_page, context, destino_pdf)
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
            "ruta": str(destino_pdf.absolute()),
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
