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
# Utilidades de scroll/portal
# =========================
def _auto_scroll_until_bottom(page_or_frame, *, step_px: int = 1200, max_tries: int = 40, pause_ms: int = 250):
    """
    Desplaza de forma incremental hasta el final del documento o del contenedor principal scrollable.
    page_or_frame: puede ser Page o Frame
    """
    # 1) intenta con el documento principal
    for _ in range(max_tries):
        reached = page_or_frame.evaluate("""
            () => {
                const el = document.scrollingElement || document.documentElement || document.body;
                const before = el.scrollTop;
                el.scrollBy(0, 1200);
                const after = el.scrollTop;
                const nearBottom = (el.scrollHeight - (after + el.clientHeight)) < 5;
                return { advanced: after > before, nearBottom, pos: after };
            }
        """)
        page_or_frame.wait_for_timeout(pause_ms)
        if reached.get("nearBottom"):
            break

    # 2) también intenta con el contenedor scrollable más grande (si lo hay)
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


def _find_billing_frame(page):
    """
    Devuelve el Frame del portal de facturación si existe (Stripe/portal/billing),
    o None si no hay iframe relevante.
    """
    for fr in page.frames:
        u = (fr.url or "").lower()
        if any(k in u for k in ["stripe", "billing", "portal"]):
            return fr
        # heurística por texto
        try:
            if fr.get_by_text(re.compile(r"(invoice|invoices|factura|facturas)", re.I)).first.wait_for(state="visible", timeout=1000):
                return fr
        except:
            pass
    return None


def wait_for_invoice_history(target, timeout_ms: int = 15000):
    """
    Intenta localizar indicadores de historial/listado de facturas.
    target puede ser Page o Frame.
    """
    candidates = [
        "Invoice history", "Invoices", "Download invoice", "View invoice",
        "Historial de facturas", "Facturas", "Descargar factura", "Ver factura"
    ]
    for txt in candidates:
        try:
            target.get_by_text(txt, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except:
            continue
    # Fallbacks típicos: tabla de facturas
    selectors = [
        "table", "[role='table']", "tbody tr", "[data-testid*='invoice']"
    ]
    for sel in selectors:
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
    Función que descarga la factura de Cursor.com usando Playwright
    Usa navegador Chrome (no persistente) con almacenamiento de cookies en .browser_context/state.json
    """
    browser = None
    context = None
    try:
        # Detectar perfil real de Chrome (no se usa por defecto para evitar conflictos)
        user_data_dir = None
        if os.name == 'nt':  # Windows
            chrome_user_data = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
            if chrome_user_data.exists():
                default_profile = chrome_user_data / "Default"
                if default_profile.exists():
                    user_data_dir = str(chrome_user_data)
                    print(f"📁 Perfil de Chrome detectado: {default_profile}")

        playwright = sync_playwright().start()

        # Siempre usar navegador normal para evitar conflictos con Chrome abierto
        print("🌐 Lanzando Chrome no persistente...\n")
        browser = playwright.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
            ]
        )

        # Crear contexto con/ sin cookies previas
        browser_context_path = Path(".browser_context")
        browser_context_path.mkdir(exist_ok=True)
        state_file = browser_context_path / "state.json"

        context = browser.new_context(
            storage_state=str(state_file) if state_file.exists() else None,
            accept_downloads=True,
        )
        page = context.new_page()

        # Verificar sesión
        has_cookies = False
        if state_file.exists():
            try:
                print("📋 Verificando sesión con cookies guardadas...")
                page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(4000)
                page.wait_for_load_state("networkidle", timeout=10000)
                page.wait_for_timeout(1000)
                current_url = (page.url or "").lower()
                body_text = ""
                try:
                    body_text = page.inner_text("body").lower()
                except:
                    pass

                if ('dashboard' in current_url and
                    "can't verify" not in body_text and
                    'login' not in current_url and 'sign' not in current_url and 'authenticator' not in current_url):
                    has_cookies = True
                    print("✅ Sesión válida detectada.\n")
                else:
                    print("⚠ Cookies no válidas o redirigido al login.\n")
            except Exception as e:
                print(f"⚠ Error verificando cookies: {e}\n")

        if not has_cookies:
            print("\n⚠ No estás logueado.")
            print("   Se abrirá el dashboard para login manual. El script detectará el login y seguirá.\n")

            if not context:
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()

            page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

            max_wait_manual_login_ms = 300000  # 5 minutos
            start_ts = datetime.now()

            print("⏳ Esperando login manual (revisando cada 2s)...\n")
            logged = False
            while (datetime.now() - start_ts).total_seconds() * 1000 < max_wait_manual_login_ms:
                try:
                    current_url = (page.url or "").lower()
                    if ('dashboard' in current_url and
                        'login' not in current_url and 'sign' not in current_url and 'authenticator' not in current_url):
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=3000)
                            page.wait_for_timeout(1000)
                            body_text = ""
                            try:
                                body_text = page.inner_text("body").lower()
                            except:
                                pass
                            if ("billing" in body_text or "invoices" in body_text or "settings" in body_text or "overview" in body_text or "dashboard" in body_text):
                                if "can't verify" not in body_text and "not a robot" not in body_text:
                                    logged = True
                                    break
                        except:
                            logged = True
                            break
                except:
                    pass
                page.wait_for_timeout(2000)

            if not logged:
                raise Exception("No se detectó login manual después de 5 minutos.")

            # Guardar cookies
            try:
                context.storage_state(path=str(state_file))
                print("💾 Cookies guardadas.\n")
            except Exception as e:
                print(f"⚠ No se pudieron guardar cookies: {e}\n")

            page.goto("https://cursor.com/dashboard", wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(2000)

        # Confirmar dashboard
        print("📂 Navegando/confirmando dashboard…")
        page.goto("https://cursor.com/dashboard", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(1500)
        current_url = (page.url or "").lower()
        if any(k in current_url for k in ['login', 'sign', 'authenticator']):
            raise Exception("No se ha completado el login. Estás en login/authenticator.")

        print("✅ Dashboard listo.\n")

        # Ir a Billing & Invoices
        print("🔍 Buscando 'Billing & Invoices'…")
        billing_encontrado = False
        try:
            billing_elem = page.get_by_text("Billing & Invoices", exact=False).first
            billing_elem.wait_for(state="visible", timeout=5000)
            billing_elem.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            billing_elem.click()
            billing_encontrado = True
            page.wait_for_timeout(2000)
            print("✅ Click en 'Billing & Invoices'\n")
        except:
            try:
                result = page.evaluate("""
                    () => {
                        const text = 'Billing & Invoices';
                        const els = Array.from(document.querySelectorAll('a, button, [role="link"], [role="button"]'));
                        for (const el of els) {
                            if (el.innerText && el.innerText.includes(text)) {
                                el.scrollIntoView({behavior:'auto', block:'center'});
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if result:
                    billing_encontrado = True
                    page.wait_for_timeout(2000)
                    print("✅ Click en 'Billing & Invoices' (fallback JS)\n")
            except:
                pass

        if not billing_encontrado:
            raise Exception("No se pudo encontrar 'Billing & Invoices' en el menú.")

        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(1000)

        # Buscar y abrir Manage subscription capturando la nueva pestaña si se abre
        print("🔍 Buscando 'Manage subscription'…")
        manage_btn = None
        manage_texts = ["Manage subscription", "Gestionar suscripción", "Manage", "Gestionar"]
        for txt in manage_texts:
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
            raise Exception("No se pudo encontrar el botón 'Manage subscription'.")

        # Click + expect_page (si abre nueva pestaña). Si no abre, seguimos en la misma.
        new_page = None
        try:
            with context.expect_page(timeout=8000) as new_tab_info:
                manage_btn.scroll_into_view_if_needed()
                page.wait_for_timeout(200)
                manage_btn.click()
            new_page = new_tab_info.value
            new_page.bring_to_front()
            print("✅ Portal de suscripción abierto en nueva pestaña.")
        except:
            # Puede que haya cargado en la misma pestaña
            try:
                manage_btn.scroll_into_view_if_needed()
                page.wait_for_timeout(200)
                manage_btn.click()
            except:
                pass
            page.wait_for_timeout(3000)
            pages = context.pages
            if len(pages) > 1:
                new_page = pages[-1]
                new_page.bring_to_front()
                print("✅ Portal de suscripción detectado en nueva pestaña (fallback).")
            else:
                new_page = page
                print("ℹ️ El portal cargó en la misma pestaña.")

        # Esperas de carga
        new_page.wait_for_load_state("domcontentloaded", timeout=15000)
        new_page.wait_for_timeout(800)
        try:
            new_page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass
        new_page.wait_for_timeout(800)

        # Scroll hasta el historial de facturas
        print("\n📜 Desplazando hasta el historial de facturas…\n")
        billing_frame = _find_billing_frame(new_page)
        target = billing_frame if billing_frame else new_page

        _auto_scroll_until_bottom(target, step_px=1500, max_tries=50, pause_ms=200)
        if not wait_for_invoice_history(target, timeout_ms=6000):
            _auto_scroll_until_bottom(target, step_px=2000, max_tries=30, pause_ms=200)

        print("✅ Zona de historial de facturas visible (o muy cerca).\n")

        # Intentar seleccionar una factura (estrategias robustas sin .all())
        print("🔍 Buscando facturas disponibles…")
        factura_seleccionada = False

        # Helper para iterar filas de tablas
        def _click_first_view_like(tgt):
            nonlocal factura_seleccionada
            rows = tgt.locator("table tr, [role='row'], tbody tr")
            try:
                count = rows.count()
            except:
                count = 0
            for i in range(min(count, 50)):  # inspecciona primeras 50 filas
                row = rows.nth(i)
                try:
                    row_text = row.inner_text(timeout=1000).lower()
                except:
                    row_text = ""
                if any(k in row_text for k in ["paid", "pagada", "invoice", "factura", "cursor", "pro", "usd", "eur", "$", "€"]):
                    try:
                        view = row.locator('button:has-text("View"), a:has-text("View"), button:has-text("ver"), a:has-text("ver")').first
                        view.wait_for(state="visible", timeout=1200)
                        view.scroll_into_view_if_needed()
                        tgt.wait_for_timeout(200)
                        view.click()
                        factura_seleccionada = True
                        tgt.wait_for_timeout(2000)
                        print("✅ Factura abierta desde la tabla.")
                        return True
                    except:
                        try:
                            row.scroll_into_view_if_needed()
                            tgt.wait_for_timeout(150)
                            row.click()
                            factura_seleccionada = True
                            tgt.wait_for_timeout(2000)
                            print("✅ Fila de factura clicada.")
                            return True
                        except:
                            pass
            return False

        if not _click_first_view_like(target):
            # Búsqueda genérica de botones/enlaces "invoice/factura"
            clickable = target.locator("button, a")
            try:
                total = clickable.count()
            except:
                total = 0
            for i in range(min(total, 60)):
                el = clickable.nth(i)
                try:
                    txt = (el.inner_text(timeout=800) or "").lower()
                except:
                    txt = ""
                if any(k in txt for k in ["invoice", "factura", "download", "descargar", "view", "ver"]):
                    try:
                        el.scroll_into_view_if_needed()
                        target.wait_for_timeout(150)
                        el.click()
                        factura_seleccionada = True
                        target.wait_for_timeout(2000)
                        print(f"✅ Abierta factura desde control: {txt[:30]}")
                        break
                    except:
                        continue

        if not factura_seleccionada:
            raise Exception("No se encontró ninguna factura disponible. Revisa que existan facturas en el portal.")

        # Cambiar a la pestaña de la factura si se abrió nueva
        new_pages = context.pages
        if len(new_pages) > 1:
            invoice_page = new_pages[-1]
        else:
            invoice_page = new_page
        try:
            invoice_page.bring_to_front()
        except:
            pass

        invoice_page.wait_for_load_state("networkidle", timeout=15000)
        invoice_page.wait_for_timeout(1500)

        # Extraer fecha para nombrar carpeta
        print("🗓️ Extrayendo fecha de la factura…")
        fecha_factura = None
        fecha_texto = None
        try:
            invoice_page.wait_for_load_state("domcontentloaded", timeout=5000)
            invoice_page.wait_for_timeout(500)
            try:
                body = invoice_page.inner_text("body")
            except:
                body = ""
            patrones = [
                r'(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})',
                r'(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})',
            ]
            for patron in patrones:
                matches = re.findall(patron, body)
                if matches:
                    for match in matches:
                        if len(match[0]) == 4:  # YYYY-MM-DD
                            año, mes, dia = match
                        else:  # DD/MM/YYYY
                            dia, mes, año = match
                        try:
                            año_int = int(año)
                            mes_int = int(mes)
                            dia_int = int(dia)
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

        # Preparar carpeta de descarga
        nombre_directorio = f"cursor_{fecha_texto}"
        desktop_path = Path.home() / "Desktop"
        facturas_dir = desktop_path / "FACTURAS"
        facturas_dir.mkdir(exist_ok=True)
        directorio_destino = facturas_dir / nombre_directorio
        directorio_destino.mkdir(exist_ok=True)
        print(f"📁 Directorio de factura: {directorio_destino}")

        # Descargar PDF
        print("⬇️ Buscando botón/enlace de descarga…")
        download = None
        download_texts = ["Descargar factura", "Download invoice", "Descargar", "Download", "Descargar PDF"]
        for texto in download_texts:
            try:
                boton = invoice_page.get_by_text(texto, exact=False).first
                boton.wait_for(state="visible", timeout=5000)
                boton.scroll_into_view_if_needed()
                invoice_page.wait_for_timeout(200)
                with invoice_page.expect_download(timeout=15000) as download_info:
                    boton.click()
                download = download_info.value
                print(f"✅ Descarga iniciada: {texto}")
                break
            except:
                continue

        if not download:
            selectors = [
                'button:has-text("Download")',
                'a:has-text("Download")',
                'button:has-text("Descargar")',
                'a:has-text("Descargar")',
                'a[href*=".pdf"]',
                'a[href*="pdf"]',
            ]
            for sel in selectors:
                try:
                    boton = invoice_page.locator(sel).first
                    boton.wait_for(state="visible", timeout=4000)
                    boton.scroll_into_view_if_needed()
                    invoice_page.wait_for_timeout(150)
                    with invoice_page.expect_download(timeout=15000) as download_info:
                        boton.click()
                    download = download_info.value
                    print(f"✅ Descarga iniciada (selector): {sel}")
                    break
                except:
                    continue

        if not download:
            raise Exception("No se pudo iniciar la descarga automáticamente. Pulsa manualmente 'Download' en la pestaña abierta.")

        # Guardar PDF
        ruta_archivo = directorio_destino / "invoice.pdf"
        print(f"💾 Guardando en: {ruta_archivo}")
        download.save_as(ruta_archivo)
        print("✅ Factura guardada correctamente.")

        # Guardar cookies actualizadas
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
    """Endpoint raíz de la API"""
    return {
        "mensaje": "API de Facturas de Cursor",
        "endpoints": {
            "descargar_factura": "/api/factura/descargar"
        }
    }


@app.post("/api/factura/descargar")
async def descargar_factura_endpoint():
    """
    Endpoint para descargar la factura de Cursor.com
    """
    try:
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, descargar_factura)
        return JSONResponse(status_code=200, content=resultado)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al descargar la factura: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
