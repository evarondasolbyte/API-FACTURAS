from datetime import datetime
from pathlib import Path
from typing import Dict
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright
import uvicorn
import re
import asyncio
import json
import os


app = FastAPI(title="API de Facturas Cursor", description="API para descargar facturas de Cursor.com")


def descargar_factura() -> Dict[str, str]:
    """
    Función que descarga la factura de Cursor.com usando Playwright
    Usa perfil persistente de Chrome real para evitar detección de Cloudflare
    
    Returns:
        Dict con información sobre la descarga (ruta, estado, mensaje)
    """
    browser = None
    context = None
    try:
        # Obtener ruta del perfil de Chrome real del usuario
        user_data_dir = None
        if os.name == 'nt':  # Windows
            chrome_user_data = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
            if chrome_user_data.exists():
                # Usar el perfil "Default" que ya tiene cookies de sesiones manuales
                default_profile = chrome_user_data / "Default"
                if default_profile.exists():
                    user_data_dir = str(chrome_user_data)
                    print(f"📁 Usando perfil de Chrome real: {default_profile}")
        
        # Iniciar Playwright
        playwright = sync_playwright().start()
        
        # Si tenemos perfil de Chrome real, usar contexto persistente
        # PERO: Desactivamos esto porque causa conflictos con Chrome ya abierto
        # En su lugar, usaremos navegador normal con cookies guardadas
        use_persistent_context = False
        if False:  # Desactivado temporalmente
            if user_data_dir:
                try:
                    print("🔐 Usando perfil persistente de Chrome (con cookies de sesiones manuales)...")
                    print("⚠ IMPORTANTE: Cierra Chrome antes de ejecutar el script para evitar conflictos\n")
                    
                    # Usar contexto persistente con el perfil real de Chrome
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir,
                        headless=False,
                        channel="chrome",  # Usar Chrome instalado
                        args=[
                            '--disable-blink-features=AutomationControlled',
                            '--disable-dev-shm-usage',
                        ],
                        accept_downloads=True,
                    )
                    
                    # Obtener la primera página o crear una nueva
                    if len(context.pages) > 0:
                        page = context.pages[0]
                    else:
                        page = context.new_page()
                    
                    browser = None  # No necesitamos browser separado con launch_persistent_context
                    use_persistent_context = True
                    print("✅ Perfil de Chrome cargado correctamente\n")
                    
                except Exception as e:
                    print(f"⚠ No se pudo usar perfil de Chrome real: {str(e)}")
                    print("   Usando navegador normal...\n")
                    use_persistent_context = False
        
        # Siempre usar navegador normal para evitar conflictos
        if not use_persistent_context:
            print("🌐 Usando navegador normal (con cookies guardadas si existen)...\n")
            browser = playwright.chromium.launch(
                headless=False,
                channel="chrome",
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                ]
            )
        
        # Si no usamos contexto persistente, crear contexto normal
        if not use_persistent_context:
            # Verificar si hay cookies guardadas
            browser_context_path = Path(".browser_context")
            browser_context_path.mkdir(exist_ok=True)
            state_file = browser_context_path / "state.json"
            
            context = browser.new_context(
                storage_state=str(state_file) if state_file.exists() else None,
                accept_downloads=True,
            )
            page = context.new_page()
            
            # Verificar si hay cookies guardadas y son válidas
            has_cookies = False
            if state_file.exists():
                try:
                    print("📋 Cookies guardadas encontradas. Verificando si la sesión sigue activa...")
                    # Ya tenemos contexto con cookies cargadas, verificar si son válidas
                    page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(5000)  # Esperar más tiempo
                    
                    # Verificar la URL actual
                    current_url = page.url.lower()
                    print(f"   URL actual: {page.url}")
                    
                    # Si estamos en login o authenticator, NO estamos logueados
                    if 'login' in current_url or 'sign' in current_url or 'authenticator' in current_url:
                        has_cookies = False
                        print("⚠ Redirigido a página de login. Las cookies no son válidas.\n")
                    else:
                        # Esperar antes de leer el contenido para evitar errores de navegación
                        page.wait_for_load_state("networkidle", timeout=10000)
                        page.wait_for_timeout(2000)
                        
                        try:
                            page_text = page.inner_text("body").lower()
                        except:
                            page_text = ""
                        
                        # Verificar que estamos en dashboard y no hay desafío de Cloudflare
                        if 'dashboard' in current_url.lower() and 'can\'t verify' not in page_text and 'login' not in page_text:
                            # Verificar adicionalmente que no estamos en la página de login
                            if 'cursor.com/dashboard' in current_url or ('cursor.com' in current_url and 'login' not in current_url):
                                has_cookies = True
                                print("✅ Cookies válidas. Sesión activa encontrada.\n")
                            else:
                                has_cookies = False
                                print("⚠ No estamos en el dashboard. Las cookies no son válidas.\n")
                        else:
                            has_cookies = False
                            print("⚠ Redirigido o desafío de Cloudflare. Las cookies no son válidas.\n")
                except Exception as e:
                    print(f"⚠ Error verificando cookies: {str(e)}\n")
                    has_cookies = False
        else:
            # Ya estamos usando perfil persistente, verificar si estamos logueados
            has_cookies = False
            try:
                print("🔍 Verificando si ya estás logueado en Chrome...")
                page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(5000)  # Esperar más tiempo
                
                current_url = page.url.lower()
                print(f"   URL actual: {page.url}")
                
                # Si estamos en login o authenticator, NO estamos logueados
                if 'login' in current_url or 'sign' in current_url or 'authenticator' in current_url:
                    has_cookies = False
                    print("⚠ Redirigido a página de login. No estás logueado.\n")
                else:
                    # Esperar antes de leer el contenido para evitar errores de navegación
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_timeout(2000)
                    
                    try:
                        page_text = page.inner_text("body").lower()
                    except:
                        page_text = ""
                    
                    # Verificar que estamos en dashboard y no hay desafío de Cloudflare
                    if 'dashboard' in current_url.lower() and 'can\'t verify' not in page_text and 'login' not in page_text:
                        # Verificar adicionalmente que no estamos en la página de login
                        if 'cursor.com/dashboard' in current_url or ('cursor.com' in current_url and 'login' not in current_url):
                            has_cookies = True
                            print("✅ Ya estás logueado en Chrome. Usando sesión existente.\n")
                        else:
                            has_cookies = False
                            print("⚠ No estamos en el dashboard. No estás logueado.\n")
                    else:
                        has_cookies = False
                        print("⚠ Redirigido o desafío de Cloudflare. No estás logueado.\n")
            except Exception as e:
                print(f"⚠ Error verificando sesión: {str(e)}")
                has_cookies = False
        
        # Si no hay cookies válidas o no estamos logueados, esperar login manual
        if not has_cookies:
            print("\n⚠ No estás logueado.")
            print("   El navegador permanecerá abierto.")
            print("   Por favor, loguéate manualmente.")
            print("   El script detectará automáticamente cuando estés logueado y continuará.\n")
            
            # Navegar al dashboard para que el usuario se pueda loguear
            if not context:
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
            
            page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            
            # Esperar hasta 5 minutos a que el usuario se loguee manualmente
            max_wait_manual_login = 300000  # 5 minutos
            start_manual_login_time = datetime.now()
            login_manual_completed = False
            
            print("⏳ Esperando a que te loguees manualmente...")
            print("   El script verificará cada 2 segundos si ya estás logueado.")
            print("   Cuando detecte que estás en el dashboard, continuará automáticamente.\n")
            
            while (datetime.now() - start_manual_login_time).total_seconds() * 1000 < max_wait_manual_login:
                try:
                    current_url = page.url.lower()
                    
                    if 'dashboard' in current_url or ('cursor.com' in current_url and 'login' not in current_url and 'sign' not in current_url and 'authenticator' not in current_url):
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=3000)
                            page.wait_for_timeout(2000)
                            page_text = page.inner_text("body").lower()
                            
                            if ('billing' in page_text or 'invoices' in page_text or 'overview' in page_text or 'dashboard' in page_text or 'settings' in page_text):
                                if 'can\'t verify' not in page_text and 'not a robot' not in page_text:
                                    login_manual_completed = True
                                    print("\n✅ ¡Login manual detectado! Estás en el dashboard.")
                                    print("   Continuando automáticamente con el proceso...\n")
                                    page.wait_for_timeout(2000)
                                    break
                        except:
                            if 'dashboard' in current_url or ('cursor.com' in current_url and 'login' not in current_url):
                                login_manual_completed = True
                                print("\n✅ ¡Login manual detectado! (por URL)")
                                print("   Continuando automáticamente...\n")
                                page.wait_for_timeout(2000)
                                break
                except:
                    pass
                
                page.wait_for_timeout(2000)  # Verificar cada 2 segundos
            
            if not login_manual_completed:
                raise Exception("No se detectó login manual después de 5 minutos. Por favor, loguéate manualmente y vuelve a ejecutar el script.")
            
            # Guardar cookies después del login manual
            if not use_persistent_context:
                try:
                    browser_context_path = Path(".browser_context")
                    browser_context_path.mkdir(exist_ok=True)
                    state_file = browser_context_path / "state.json"
                    context.storage_state(path=str(state_file))
                    print("💾 Cookies guardadas exitosamente\n")
                except Exception as e:
                    print(f"⚠ No se pudieron guardar cookies: {str(e)}\n")
            
            # Continuar con el proceso - ir directamente al dashboard después del login manual
            page.goto("https://cursor.com/dashboard", wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(3000)

        
        # Ya estamos logueados, continuar con el proceso
        print("➡️ Continuando con la descarga de facturas...")
        
        # Si no tenemos página activa, crear una
        try:
            if len(context.pages) > 0:
                page = context.pages[0]
            else:
                page = context.new_page()
        except:
            page = context.new_page()
        
        # Navegar al dashboard primero, pero VERIFICAR que estamos logueados
        print("📂 Navegando al dashboard...")
        page.goto("https://cursor.com/dashboard", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(3000)
        
        # Verificar que realmente estamos logueados antes de continuar
        current_url = page.url.lower()
        print(f"   Verificando URL: {page.url}")
        
        if 'login' in current_url or 'sign' in current_url or 'authenticator' in current_url:
            raise Exception("No se ha completado el login. El script está en la página de login/authenticator. Por favor, verifica que el login automático funcione correctamente.")
        
        # Verificar que estamos en el dashboard
        if 'dashboard' not in current_url:
            print(f"⚠ URL actual: {page.url}")
            print("   Esperando a que se redirija al dashboard...")
            page.wait_for_timeout(5000)
            current_url = page.url.lower()
            
            if 'login' in current_url or 'sign' in current_url or 'authenticator' in current_url:
                raise Exception("No se ha completado el login. Por favor, verifica las credenciales o completa el login manualmente.")
        
        print("✅ Confirmado: Estamos en el dashboard. Continuando...\n")
        
        # Esperar a que la página se cargue completamente
        print("⏳ Esperando a que se cargue completamente...")
        page.wait_for_load_state("networkidle", timeout=10000)
        page.wait_for_timeout(2000)
        
        # Hacer scroll para asegurar que todos los elementos estén visibles
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)
        
        # Buscar y hacer clic en "Billing & Invoices"
        print("🔍 Buscando sección 'Billing & Invoices'...")
        billing_encontrado = False
        
        # Buscar directamente por texto
        try:
            billing_elem = page.get_by_text("Billing & Invoices", exact=False).first
            if billing_elem.is_visible(timeout=5000):
                billing_elem.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                billing_elem.click()
                page.wait_for_timeout(3000)
                billing_encontrado = True
                print("✅ Encontrado y clickeado: 'Billing & Invoices'\n")
        except:
            # Si no funciona, usar JavaScript como fallback
            try:
                result = page.evaluate("""
                    () => {
                        const text = 'Billing & Invoices';
                        const elements = Array.from(document.querySelectorAll('a, button, [role="link"], [role="button"]'));
                        for (let el of elements) {
                            if (el.innerText && el.innerText.includes(text)) {
                                el.scrollIntoView({behavior: 'auto', block: 'center'});
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if result:
                    page.wait_for_timeout(3000)
                    billing_encontrado = True
                    print("✅ Encontrado y clickeado con JavaScript: 'Billing & Invoices'\n")
            except:
                pass
        
        if not billing_encontrado:
            raise Exception("No se pudo encontrar 'Billing & Invoices' en el menú. Por favor, navega manualmente y vuelve a intentar.")
        
        # Esperar a que se cargue la página de facturas
        print("⏳ Esperando a que cargue la página de facturas...")
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(3000)
        
        # PASO 2: Buscar y hacer clic en "Manage subscription"
        print("🔍 Buscando botón 'Manage subscription'...")
        manage_subscription_found = False
        
        try:
            # Buscar "Manage subscription" o "Gestionar suscripción"
            manage_texts = ["Manage subscription", "Gestionar suscripción", "Manage", "Gestionar"]
            for texto in manage_texts:
                try:
                    manage_btn = page.get_by_text(texto, exact=False).first
                    manage_btn.wait_for(state="visible", timeout=5000)
                    manage_btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(500)
                    manage_btn.click()
                    manage_subscription_found = True
                    print(f"✅ Encontrado y clickeado: '{texto}'")
                    page.wait_for_timeout(3000)
                    break
                except:
                    continue
            
            # Si no se encontró por texto, buscar por selector
            if not manage_subscription_found:
                try:
                    manage_btn = page.locator('button:has-text("Manage"), a:has-text("Manage")').first
                    manage_btn.wait_for(state="visible", timeout=5000)
                    manage_btn.scroll_into_view_if_needed()
                    manage_btn.click()
                    manage_subscription_found = True
                    print("✅ Encontrado 'Manage subscription'")
                    page.wait_for_timeout(3000)
                except:
                    pass
        except Exception as e:
            print(f"⚠ Error buscando 'Manage subscription': {str(e)[:50]}")
        
        if not manage_subscription_found:
            raise Exception("No se pudo encontrar el botón 'Manage subscription'")
        
        # PASO 3: Esperar a que se abra la nueva pestaña
        print("⏳ Esperando a que se abra la nueva pestaña...")
        page.wait_for_timeout(5000)
        
        # Obtener todas las pestañas abiertas
        all_pages = context.pages
        print(f"📑 Pestañas abiertas: {len(all_pages)}")
        
        # La nueva pestaña debería ser la última
        if len(all_pages) > 1:
            # Cambiar a la última pestaña (la nueva)
            new_page = all_pages[-1]
            page = new_page
            page.bring_to_front()  # Traer la pestaña al frente
            print("✅ Cambiado a la nueva pestaña")
        else:
            # Si no se abrió nueva pestaña, esperar más
            print("⚠ No se detectó nueva pestaña, esperando...")
            page.wait_for_timeout(5000)
            all_pages = context.pages
            if len(all_pages) > 1:
                page = all_pages[-1]
                print("✅ Cambiado a la nueva pestaña (después de esperar)")
        
        # Esperar a que se cargue la nueva página completamente
        print("⏳ Esperando a que se cargue completamente la nueva página...")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(2000)
        
        # PASO 4: Hacer scroll hasta el final para llegar al historial de facturas
        print("\n📜 Bajando la barra lateral hasta el final...\n")
        
        # Hacer scroll agresivo y repetido hasta el final
        print("   Haciendo scroll hasta el final...")
        for intento in range(5):  # Intentar 5 veces para asegurar que llegamos al final
            # Scroll directo al final
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(300)
            
            # Scroll usando scrollIntoView en el body
            page.evaluate("document.body.scrollIntoView(false)")
            page.wait_for_timeout(300)
            
            # Scroll usando scrollBy
            page.evaluate("window.scrollBy(0, 10000)")
            page.wait_for_timeout(300)
        
        # Scroll final agresivo
        page.evaluate("""
            () => {
                window.scrollTo(0, Math.max(
                    document.body.scrollHeight,
                    document.documentElement.scrollHeight,
                    document.body.offsetHeight,
                    document.documentElement.offsetHeight,
                    document.body.clientHeight,
                    document.documentElement.clientHeight
                ));
            }
        """)
        page.wait_for_timeout(500)
        
        # También hacer scroll en contenedores internos
        page.evaluate("""
            () => {
                const allElements = document.querySelectorAll('*');
                for (const el of allElements) {
                    try {
                        const style = window.getComputedStyle(el);
                        if ((style.overflow === 'auto' || style.overflow === 'scroll' || 
                             style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            el.scrollHeight > el.clientHeight) {
                            el.scrollTop = el.scrollHeight;
                        }
                    } catch (e) {}
                }
            }
        """)
        page.wait_for_timeout(500)
        
        # Último scroll para asegurar
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
        
        print("✅ Scroll hasta el final completado\n")
        
        # PASO 5: Buscar y hacer clic en la primera factura disponible
        print("🔍 Buscando facturas disponibles para descargar...")
        factura_seleccionada = False
        
        # Estrategia 1: Buscar enlaces o botones de facturas (normalmente tienen fechas o números)
        try:
            # Buscar todas las filas de la tabla de facturas
            table_rows = page.locator('table tr, [role="row"], tbody tr').all()
            
            for row in table_rows:
                try:
                    # Verificar que la fila tenga contenido de factura (fecha, cantidad, etc.)
                    row_text = row.inner_text(timeout=1000).lower()
                    
                    # Buscar indicadores de que es una fila de factura
                    if any(keyword in row_text for keyword in ["pagada", "paid", "20,00", "us$", "cursor", "pro"]):
                        # Hacer clic en la fila completa (la primera factura)
                        row.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        row.click()
                        factura_seleccionada = True
                        print("✅ Clickeada la primera factura en la tabla")
                        page.wait_for_timeout(3000)
                        break
                except:
                    continue
        except Exception as e:
            print(f"   ⚠ Error en estrategia 1: {str(e)[:50]}")
        
        # Estrategia 2: Buscar elementos con fechas (facturas típicamente muestran fechas)
        if not factura_seleccionada:
            try:
                # Buscar elementos que contengan fechas en formato común
                fecha_elements = page.locator('text=/\\d{1,2}[\\/\\-\\s]\\w+[\\/\\-\\s]\\d{2,4}/').all()
                for elem in fecha_elements[:5]:  # Primeros 5 elementos con fechas
                    try:
                        parent = elem.locator('..')
                        # Buscar botón "view" o enlace cerca de la fecha
                        view_near = parent.locator('button:has-text("View"), a:has-text("View"), button:has-text("view")').first
                        view_near.wait_for(state="visible", timeout=2000)
                        view_near.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        view_near.click()
                        factura_seleccionada = True
                        page.wait_for_timeout(3000)
                        print("✅ Encontrada factura por fecha\n")
                        break
                    except:
                        continue
            except:
                pass
        
        # Estrategia 3: Buscar la primera fila de tabla de facturas
        if not factura_seleccionada:
            try:
                # Buscar en tablas de facturas
                table_rows = page.locator('table tr, [role="row"]').all()
                for row in table_rows[1:4]:  # Saltar header, tomar primeras 3 filas
                    try:
                        view_link = row.locator('button:has-text("View"), a:has-text("View"), button:has-text("view")').first
                        view_link.wait_for(state="visible", timeout=2000)
                        view_link.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        view_link.click()
                        factura_seleccionada = True
                        page.wait_for_timeout(3000)
                        print("✅ Encontrada factura en tabla\n")
                        break
                    except:
                        continue
            except:
                pass
        
        # Estrategia 4: Buscar cualquier enlace o botón clickeable que parezca una factura
        if not factura_seleccionada:
            try:
                # Buscar todos los botones y enlaces visibles
                all_clickable = page.locator('button:visible, a:visible').all()
                for elem in all_clickable[:10]:  # Primeros 10 elementos clickeables
                    try:
                        texto = elem.inner_text().lower()
                        # Buscar palabras clave de factura
                        if any(word in texto for word in ["view", "ver", "invoice", "factura", "download", "descargar"]):
                            # Evitar botones del menú lateral
                            href = elem.get_attribute("href") or ""
                            if "billing" not in href and "invoice" not in href and len(texto) < 30:
                                elem.wait_for(state="visible", timeout=2000)
                                elem.scroll_into_view_if_needed()
                                page.wait_for_timeout(500)
                                elem.click()
                                factura_seleccionada = True
                                page.wait_for_timeout(3000)
                                print(f"✅ Encontrada factura: {texto[:30]}\n")
                                break
                    except:
                        continue
            except:
                pass
        
        if not factura_seleccionada:
            raise Exception("No se pudo encontrar ninguna factura disponible. Verifica que haya facturas en la página.")
        
        # PASO 6: Esperar a que se abra la nueva pestaña con la factura
        print("⏳ Esperando a que se abra la pestaña con la factura...")
        page.wait_for_timeout(5000)
        
        # Obtener todas las pestañas abiertas
        all_pages = context.pages
        print(f"📑 Pestañas abiertas: {len(all_pages)}")
        
        # La nueva pestaña debería ser la última
        if len(all_pages) > 1:
            # Cambiar a la última pestaña (la de la factura)
            new_page = all_pages[-1]
            page = new_page
            page.bring_to_front()  # Traer la pestaña al frente
            print("✅ Cambiado a la pestaña de la factura")
        else:
            # Si no se abrió nueva pestaña, usar la actual
            print("⚠ No se detectó nueva pestaña, usando la actual")
        
        # Esperar a que se cargue la página de la factura
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(3000)
        
        # Hacer scroll para asegurar visibilidad
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)
        
        # PASO 7: Buscar y hacer clic en "Descargar factura" o "Download invoice"
        print("🔍 Buscando botón 'Descargar factura'...")
        
        # Extraer la fecha de la factura
        fecha_factura = None
        fecha_texto = None
        
        try:
            # Esperar antes de leer el contenido para evitar errores de navegación
            page.wait_for_load_state("domcontentloaded", timeout=5000)
            page.wait_for_timeout(1000)
            page_text = page.inner_text("body")
            patrones = [
                r'(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})',
                r'(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})',
            ]
            
            for patron in patrones:
                matches = re.findall(patron, page_text)
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
                                fecha_texto = f"{año}_{mes.zfill(2)}_{dia.zfill(2)}"
                                break
                        except ValueError:
                            continue
                    if fecha_factura:
                        break
        except:
            pass
        
        # Si no se encontró fecha, usar la actual
        if not fecha_factura:
            fecha_factura = datetime.now()
            fecha_texto = fecha_factura.strftime('%Y_%m_%d')
        
        # Crear directorio para la factura en Escritorio/FACTURAS
        nombre_directorio = f"cursor_{fecha_texto}"
        
        # Obtener ruta del Escritorio
        if os.name == 'nt':  # Windows
            desktop_path = Path.home() / "Desktop"
        else:  # Linux/Mac
            desktop_path = Path.home() / "Desktop"
        
        # Crear carpeta FACTURAS en el escritorio si no existe
        facturas_dir = desktop_path / "FACTURAS"
        facturas_dir.mkdir(exist_ok=True)
        print(f"📁 Carpeta FACTURAS: {facturas_dir}")
        
        # Crear el directorio con el nombre de la fecha dentro de FACTURAS
        directorio_destino = facturas_dir / nombre_directorio
        directorio_destino.mkdir(exist_ok=True)
        print(f"📁 Directorio de factura: {directorio_destino}")
        
        # Buscar botón "Descargar factura" o "Download invoice"
        download = None
        download_texts = ["Descargar factura", "Download invoice", "Descargar", "Download", "Download invoice", "Descargar PDF"]
        
        for texto in download_texts:
            try:
                boton = page.get_by_text(texto, exact=False).first
                boton.wait_for(state="visible", timeout=5000)
                boton.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                print(f"✅ Encontrado botón: '{texto}'")
                
                # Esperar la descarga
                with page.expect_download(timeout=10000) as download_info:
                    boton.click()
                download = download_info.value
                print("✅ Descarga iniciada")
                break
            except:
                continue
        
        # Si no se encontró por texto, buscar por selector
        if not download:
            try:
                download_selectors = [
                    'button:has-text("Download")',
                    'a:has-text("Download")',
                    'button:has-text("Descargar")',
                    'a:has-text("Descargar")',
                    'a[href*=".pdf"]',
                    'a[href*="pdf"]',
                ]
                for selector in download_selectors:
                    try:
                        boton = page.locator(selector).first
                        boton.wait_for(state="visible", timeout=3000)
                        boton.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        
                        with page.expect_download(timeout=10000) as download_info:
                            boton.click()
                        download = download_info.value
                        print(f"✅ Descarga iniciada con selector: {selector}")
                        break
                    except:
                        continue
            except:
                pass
        
        if not download:
            print("⚠ No se pudo encontrar el botón de descarga automáticamente.")
            print("   Por favor, haz clic manualmente en 'View' o 'Download' en el navegador.")
            print("   El archivo se descargará automáticamente...\n")
            
            # Esperar a que el usuario descargue manualmente
            max_wait_download = 120000  # 2 minutos
            start_download_time = datetime.now()
            download_file = None
            
            while (datetime.now() - start_download_time).total_seconds() * 1000 < max_wait_download:
                try:
                    # Buscar descargas en el contexto
                    downloads = context.cookies()
                    # Esperar a que aparezca la descarga
                    page.wait_for_timeout(2000)
                    
                    # Si hay una descarga esperando, tomarla
                    try:
                        with page.expect_download(timeout=1000) as download_info:
                            pass  # Ya hay una descarga en progreso
                        download = download_info.value
                        break
                    except:
                        pass
                except:
                    page.wait_for_timeout(2000)
            
            if not download:
                raise Exception("No se pudo encontrar el botón de descarga. Por favor, descarga manualmente la factura y colócala en el directorio correspondiente.")
        
        # Guardar la descarga en el directorio correspondiente
        ruta_archivo = directorio_destino / "invoice.pdf"
        
        # Si el archivo ya existe, se sobreescribirá automáticamente
        print(f"💾 Guardando factura en: {ruta_archivo}")
        download.save_as(ruta_archivo)
        print(f"✅ Factura guardada exitosamente")
        
        # Guardar cookies actualizadas (solo si no estamos usando perfil persistente)
        if not use_persistent_context:
            try:
                browser_context_path = Path(".browser_context")
                browser_context_path.mkdir(exist_ok=True)
                state_file = browser_context_path / "state.json"
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
        # NO cerrar el navegador si hay un error - dejar que el usuario vea qué pasó
        error_msg = f"Error al descargar la factura: {str(e)}"
        print(f"\n❌ {error_msg}")
        print("⚠ El navegador permanecerá abierto para que puedas ver qué pasó.")
        print("   Si quieres intentar de nuevo, puedes cerrar el navegador manualmente.\n")
        
        # Solo cerrar el navegador si el usuario lo solicita explícitamente
        # Por ahora, lo dejamos abierto para debugging
        # if browser:
        #     try:
        #         browser.close()
        #     except:
        #         pass
        # if context:
        #     try:
        #         context.close()
        #     except:
        #         pass
        
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
    
    Returns:
        JSON con el estado de la descarga y la ruta del archivo
    """
    try:
        # Ejecutar la función en un thread separado
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, descargar_factura)
        
        return JSONResponse(
            status_code=200,
            content=resultado
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al descargar la factura: {str(e)}"
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
    