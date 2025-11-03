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
        
        # Si no hay cookies válidas o no estamos logueados, hacer login automático
        if not has_cookies:
            print("\n" + "="*70)
            print("🔐 INICIANDO LOGIN AUTOMÁTICO")
            print("="*70)
            print("")
            print("Intentando login automático con las credenciales...")
            print("")
            
            # Si no tenemos contexto (no estamos usando perfil persistente), crear uno nuevo
            if not context:
                context = browser.new_context(
                    accept_downloads=True,
                )
                page = context.new_page()
            
            # Navegar directamente a la página de login de Cursor
            print("📂 Navegando a página de login...")
            # Intentar ir directamente a la URL de login si existe, sino ir a cursor.com
            page.goto("https://cursor.com/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            
            # Verificar si ya estamos en la página de login o necesitamos hacer clic en "Sign in"
            current_url = page.url.lower()
            
            # Intentar hacer login automáticamente
            try:
                # Si no estamos en una página de login, buscar el botón "Sign in"
                if "login" not in current_url and "sign" not in current_url and "authenticator" not in current_url:
                    print("🔍 Buscando botón 'Sign in' o 'Log in'...")
                    
                    # Buscar y hacer clic en "Sign in" o "Log in"
                    sign_in_found = False
                    sign_in_texts = ["Sign in", "Log in", "Login", "Iniciar sesión"]
                    
                    for texto in sign_in_texts:
                        try:
                            sign_in_btn = page.get_by_text(texto, exact=False).first
                            sign_in_btn.wait_for(state="visible", timeout=5000)
                            sign_in_btn.scroll_into_view_if_needed()
                            page.wait_for_timeout(500)
                            sign_in_btn.click()
                            sign_in_found = True
                            print(f"✅ Encontrado y clickeado: '{texto}'")
                            page.wait_for_timeout(5000)  # Esperar más tiempo después del clic
                            break
                        except:
                            continue
                    
                    if not sign_in_found:
                        # Intentar buscar por rol
                        try:
                            sign_in_btn = page.get_by_role("button", name="Sign in", exact=False).first
                            sign_in_btn.wait_for(state="visible", timeout=5000)
                            sign_in_btn.click()
                            sign_in_found = True
                            page.wait_for_timeout(5000)
                        except:
                            pass
                    
                    if not sign_in_found:
                        raise Exception("No se pudo encontrar el botón 'Sign in'")
                else:
                    print("✅ Ya estamos en la página de login")
                
                # Esperar a que se cargue la página de login
                print("⏳ Esperando a que cargue la página de login...")
                # Esperar más tiempo y verificar que realmente se cargó
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                page.wait_for_timeout(5000)  # Esperar más tiempo
                
                # Verificar que estamos en la página de login
                current_url = page.url.lower()
                if 'authenticator' not in current_url and 'login' not in current_url:
                    print(f"⚠ URL inesperada después de 'Sign in': {page.url}")
                    page.wait_for_timeout(3000)
                
                # Credenciales
                usuario = "cursor7@solbyte.com"
                contraseña = "We2&Je2-Pk3&Zz5)"
                
                print("📝 Introduciendo credenciales...")
                
                # PASO 1: Buscar y llenar el campo de email
                print("🔍 Buscando campo de email...")
                email_found = False
                email_input = None
                
                # Esperar un poco más para que la página se cargue completamente
                page.wait_for_timeout(3000)
                
                # Estrategia 1: Buscar por el label "Correo electrónico" y luego el input asociado
                try:
                    print("   Buscando por label 'Correo electrónico'...")
                    # Buscar el label y luego el input asociado
                    label = page.get_by_text("Correo electrónico", exact=False).first
                    if label.is_visible(timeout=5000):
                        # Buscar el input que sigue al label o está cerca
                        # Intentar buscar por relación con el label
                        try:
                            # Buscar todos los inputs en la página
                            all_inputs = page.locator('input').all()
                            for inp in all_inputs:
                                try:
                                    if inp.is_visible(timeout=2000):
                                        # Verificar el placeholder o atributos
                                        placeholder = inp.get_attribute("placeholder") or ""
                                        input_type = inp.get_attribute("type") or ""
                                        
                                        if "correo" in placeholder.lower() or "email" in placeholder.lower() or input_type == "email":
                                            email_input = inp
                                            print(f"   ✅ Encontrado campo cerca del label")
                                            break
                                except:
                                    continue
                        except:
                            pass
                except:
                    pass
                
                # Estrategia 2: Buscar directamente por selectores comunes
                if not email_input:
                    print("   Buscando por selectores CSS...")
                    email_selectors = [
                        'input[type="email"]',
                        'input[name="email"]',
                        'input[placeholder*="correo" i]',
                        'input[placeholder*="email" i]',
                        'input[placeholder*="Tu dirección de correo"]',
                        'input[placeholder*="correo electrónico" i]',
                    ]
                    
                    for selector in email_selectors:
                        try:
                            email_input = page.locator(selector).first
                            if email_input.is_visible(timeout=5000):
                                print(f"   ✅ Encontrado campo con selector: {selector}")
                                break
                        except:
                            continue
                
                # Estrategia 3: Buscar todos los inputs y filtrar
                if not email_input:
                    print("   Buscando en todos los inputs de la página...")
                    try:
                        all_inputs = page.locator('input').all()
                        for inp in all_inputs:
                            try:
                                if inp.is_visible(timeout=2000):
                                    input_type = inp.get_attribute("type") or ""
                                    input_name = inp.get_attribute("name") or ""
                                    input_placeholder = inp.get_attribute("placeholder") or ""
                                    input_id = inp.get_attribute("id") or ""
                                    
                                    # Verificar si es un campo de email
                                    if (input_type == "email" or 
                                        "email" in input_name.lower() or 
                                        "email" in input_placeholder.lower() or
                                        "email" in input_id.lower() or
                                        "correo" in input_placeholder.lower() or
                                        "correo electrónico" in input_placeholder.lower() or
                                        "dirección de correo" in input_placeholder.lower()):
                                        email_input = inp
                                        print(f"   ✅ Encontrado campo por atributos: type={input_type}, placeholder={input_placeholder[:50]}")
                                        break
                            except:
                                continue
                    except Exception as e:
                        print(f"   ⚠ Error buscando inputs: {str(e)[:50]}")
                
                # Si encontramos el campo, intentar escribir automáticamente, si falla esperar manual
                if email_input:
                    try:
                        print(f"   Intentando escribir email automáticamente: {usuario}")
                        
                        # Intentar escribir automáticamente (con timeout corto)
                        try:
                            email_input.scroll_into_view_if_needed(timeout=5000)
                            page.wait_for_timeout(500)
                            email_input.click(timeout=5000)
                            page.wait_for_timeout(500)
                            email_input.fill(usuario)
                            page.wait_for_timeout(1000)
                            valor = email_input.input_value()
                            
                            if valor and usuario.lower() in valor.lower():
                                email_found = True
                                print(f"✅ Email introducido automáticamente: {usuario}")
                            else:
                                print("⚠ No se pudo escribir automáticamente, esperando entrada manual...")
                                raise Exception("No se escribió automáticamente")
                        except:
                            # Si falla el intento automático, esperar entrada manual
                            print("⏳ Esperando a que introduzcas el email manualmente...")
                            print(f"   Por favor, escribe el email en el campo: {usuario}")
                            print("   Puedes hacer clic en 'Continuar' cuando termines.")
                            print("   El script detectará automáticamente cuando avances.")
                            
                            # Esperar hasta 2 minutos a que el usuario introduzca el email o haga clic en Continuar
                            max_wait_manual = 120000  # 2 minutos
                            start_manual_time = datetime.now()
                            
                            while (datetime.now() - start_manual_time).total_seconds() * 1000 < max_wait_manual:
                                try:
                                    # Opción 1: Verificar si hay email en el campo
                                    try:
                                        email_input = page.locator('input[type="email"], input[placeholder*="correo" i], input[placeholder*="email" i]').first
                                        valor = email_input.input_value(timeout=1000)
                                        
                                        if valor and len(valor) > 0 and "@" in valor and "." in valor:
                                            email_found = True
                                            print(f"✅ Email detectado en el campo: {valor}")
                                            print("✅ Continuando automáticamente...")
                                            page.wait_for_timeout(2000)
                                            break
                                    except:
                                        pass
                                    
                                    # Opción 2: Verificar si el usuario ya hizo clic en "Continuar" y estamos en la página de contraseña
                                    try:
                                        # Buscar campo de contraseña (esto significa que el usuario ya avanzó)
                                        password_check = page.locator('input[type="password"]').first
                                        if password_check.is_visible(timeout=1000):
                                            print("✅ Detectado que ya hiciste clic en 'Continuar' - estamos en la página de contraseña")
                                            email_found = True  # Marcar como completado para continuar
                                            break
                                    except:
                                        pass
                                    
                                    # Opción 3: Verificar la URL - si cambió de login a otra cosa, significa que avanzó
                                    current_url = page.url.lower()
                                    if "password" in current_url or "login" not in current_url or "sign" not in current_url:
                                        if "authenticator" not in current_url and "cursor.com" in current_url:
                                            print("✅ Detectado avance a otra página - continuando...")
                                            email_found = True
                                            break
                                except:
                                    pass
                                
                                page.wait_for_timeout(2000)  # Verificar cada 2 segundos
                            
                            if not email_found:
                                # Última verificación: ¿estamos en la página de contraseña?
                                try:
                                    password_check = page.locator('input[type="password"]').first
                                    if password_check.is_visible(timeout=1000):
                                        print("✅ Detectado campo de contraseña - el usuario ya avanzó manualmente")
                                        email_found = True
                                except:
                                    pass
                            
                            if not email_found:
                                raise Exception("No se detectó avance después de 2 minutos. Por favor, introduce el email y haz clic en 'Continuar'.")
                        
                        page.wait_for_timeout(2000)
                    except Exception as e:
                        print(f"   ❌ Error: {str(e)}")
                        # Intentar buscar el campo una vez más y verificar si hay algo
                        try:
                            email_input = page.locator('input[type="email"], input[placeholder*="correo" i], input[placeholder*="email" i]').first
                            valor = email_input.input_value()
                            if valor and "@" in valor:
                                print(f"✅ Email encontrado en el campo: {valor}")
                                email_found = True
                            else:
                                raise
                        except:
                            raise
                else:
                    # Si no se encontró, mostrar información de debug
                    try:
                        all_inputs = page.locator('input').all()
                        print(f"   ⚠ No se encontró campo de email. Inputs encontrados: {len(all_inputs)}")
                        for i, inp in enumerate(all_inputs[:5]):
                            try:
                                placeholder = inp.get_attribute("placeholder") or "sin placeholder"
                                input_type = inp.get_attribute("type") or "sin type"
                                print(f"      Input {i+1}: type={input_type}, placeholder={placeholder[:50]}")
                            except:
                                pass
                    except:
                        pass
                    raise Exception("No se pudo encontrar el campo de email. Verifica que la página se cargó correctamente.")
                
                # PASO 2: Hacer clic en "Continuar" después de introducir el email
                print("🔘 Buscando botón 'Continuar' después del email...")
                continue_clicked = False
                continue_texts = ["Continuar", "Continue", "Next", "Siguiente"]
                
                for texto in continue_texts:
                    try:
                        continue_btn = page.get_by_text(texto, exact=False).first
                        continue_btn.wait_for(state="visible", timeout=5000)
                        continue_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        continue_btn.click()
                        continue_clicked = True
                        print(f"✅ Botón '{texto}' clickeado")
                        page.wait_for_timeout(5000)  # Esperar a que cargue la siguiente página
                        break
                    except:
                        continue
                
                if not continue_clicked:
                    # Intentar presionar Enter en el campo de email
                    try:
                        email_input.press("Enter")
                        continue_clicked = True
                        print("✅ Enter presionado en campo de email")
                        page.wait_for_timeout(5000)
                    except:
                        pass
                
                if not continue_clicked:
                    print("⚠ No se encontró botón 'Continuar', pero continuando...")
                    page.wait_for_timeout(3000)
                
                # PASO 3: Buscar campo de contraseña (debe aparecer después de "Continuar")
                print("🔍 Buscando campo de contraseña...")
                password_found = False
                password_input = None
                
                # Esperar a que aparezca el campo de contraseña
                max_wait_password = 30000  # 30 segundos
                start_password_time = datetime.now()
                
                while (datetime.now() - start_password_time).total_seconds() * 1000 < max_wait_password:
                    password_selectors = [
                        'input[type="password"]',
                        'input[name="password"]',
                        'input[id*="password"]',
                        'input[id*="Password"]',
                    ]
                    
                    for selector in password_selectors:
                        try:
                            password_input = page.locator(selector).first
                            if password_input.is_visible(timeout=2000):
                                print(f"   ✅ Encontrado campo con selector: {selector}")
                                password_found = True
                                break
                        except:
                            continue
                    
                    if password_found:
                        break
                    
                    # Si no se encontró, buscar todos los inputs de tipo password
                    if not password_input:
                        try:
                            all_inputs = page.locator('input[type="password"]').all()
                            if all_inputs:
                                for inp in all_inputs:
                                    try:
                                        if inp.is_visible(timeout=2000):
                                            password_input = inp
                                            print(f"   ✅ Encontrado campo de contraseña")
                                            password_found = True
                                            break
                                    except:
                                        continue
                        except:
                            pass
                    
                    if password_found:
                        break
                    
                    page.wait_for_timeout(2000)
                
                if password_input and password_found:
                    try:
                        print(f"   Intentando escribir contraseña automáticamente...")
                        try:
                            password_input.scroll_into_view_if_needed(timeout=5000)
                            page.wait_for_timeout(500)
                            password_input.click(timeout=5000)
                            page.wait_for_timeout(500)
                            password_input.fill(contraseña)
                            page.wait_for_timeout(1000)
                            
                            # Verificar que se escribió
                            valor_pass = password_input.input_value()
                            if valor_pass and len(valor_pass) > 0:
                                password_found = True
                                print("✅ Contraseña introducida automáticamente")
                            else:
                                print("⚠ No se pudo escribir automáticamente, esperando entrada manual...")
                                raise Exception("No se escribió automáticamente")
                        except:
                            # Si falla, esperar entrada manual
                            print("⏳ Esperando a que introduzcas la contraseña manualmente...")
                            print("   El script continuará automáticamente cuando detecte la contraseña en el campo.")
                            
                            # Esperar hasta 2 minutos
                            max_wait_pass = 120000
                            start_pass_time = datetime.now()
                            
                            while (datetime.now() - start_pass_time).total_seconds() * 1000 < max_wait_pass:
                                try:
                                    password_input = page.locator('input[type="password"]').first
                                    valor_pass = password_input.input_value(timeout=1000)
                                    
                                    if valor_pass and len(valor_pass) > 0:
                                        password_found = True
                                        print("✅ Contraseña detectada en el campo")
                                        print("✅ Continuando automáticamente...")
                                        page.wait_for_timeout(2000)
                                        break
                                except:
                                    pass
                                
                                page.wait_for_timeout(2000)
                            
                            if not password_found:
                                raise Exception("No se detectó contraseña después de 2 minutos de espera")
                        
                        page.wait_for_timeout(2000)
                    except Exception as e:
                        print(f"   ⚠ Error al introducir contraseña: {str(e)[:50]}")
                        # Verificar una vez más si hay contraseña
                        try:
                            password_input = page.locator('input[type="password"]').first
                            valor_pass = password_input.input_value()
                            if valor_pass and len(valor_pass) > 0:
                                print("✅ Contraseña encontrada en el campo")
                                password_found = True
                            else:
                                raise
                        except:
                            raise
                else:
                    raise Exception("No se pudo encontrar el campo de contraseña después de introducir el email")
                
                # Buscar y hacer clic en el botón de submit/login o "Continuar"
                print("🔘 Buscando botón de login/submit...")
                submit_found = False
                submit_texts = ["Sign in", "Log in", "Login", "Iniciar sesión", "Submit", "Entrar", "Continuar", "Continue"]
                
                # Buscar por texto primero
                for texto in submit_texts:
                    try:
                        submit_btn = page.get_by_text(texto, exact=False).first
                        submit_btn.wait_for(state="visible", timeout=3000)
                        submit_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        submit_btn.click()
                        submit_found = True
                        print(f"✅ Botón '{texto}' clickeado")
                        page.wait_for_timeout(2000)
                        break
                    except:
                        continue
                
                if not submit_found:
                    # Buscar por rol
                    for texto in submit_texts:
                        try:
                            submit_btn = page.get_by_role("button", name=texto, exact=False).first
                            submit_btn.wait_for(state="visible", timeout=3000)
                            submit_btn.scroll_into_view_if_needed()
                            page.wait_for_timeout(500)
                            submit_btn.click()
                            submit_found = True
                            print(f"✅ Botón '{texto}' clickeado (por rol)")
                            page.wait_for_timeout(2000)
                            break
                        except:
                            continue
                
                if not submit_found:
                    # Intentar buscar input type submit o button type submit
                    try:
                        submit_btn = page.locator('button[type="submit"], input[type="submit"]').first
                        submit_btn.wait_for(state="visible", timeout=3000)
                        submit_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        submit_btn.click()
                        submit_found = True
                        print("✅ Botón submit clickeado")
                        page.wait_for_timeout(2000)
                    except:
                        pass
                
                if not submit_found:
                    # Intentar presionar Enter en el campo de contraseña o email
                    try:
                        if password_input:
                            password_input.press("Enter")
                            submit_found = True
                            print("✅ Enter presionado en campo de contraseña")
                            page.wait_for_timeout(2000)
                        elif email_input:
                            email_input.press("Enter")
                            submit_found = True
                            print("✅ Enter presionado en campo de email")
                            page.wait_for_timeout(2000)
                    except:
                        pass
                
                # Esperar a que se complete el login
                print("⏳ Esperando a que se complete el login...")
                page.wait_for_timeout(5000)
                
                # Esperar hasta llegar al dashboard
                max_wait_login = 30000  # 30 segundos
                start_login_time = datetime.now()
                login_completed = False
                
                while (datetime.now() - start_login_time).total_seconds() * 1000 < max_wait_login:
                    try:
                        current_url = page.url
                        
                        # Verificar si llegamos al dashboard
                        if 'dashboard' in current_url.lower():
                            # Verificar que no hay desafío de Cloudflare
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=5000)
                                page.wait_for_timeout(2000)
                                page_text = page.inner_text("body").lower()
                                
                                if 'can\'t verify' not in page_text and 'not a robot' not in page_text and 'verifica que eres' not in page_text:
                                    login_completed = True
                                    print("\n✅ ¡Login automático completado exitosamente!")
                                    print("   Dashboard detectado. Esperando 3 segundos antes de continuar...\n")
                                    page.wait_for_timeout(3000)
                                    break
                            except:
                                # Si hay error leyendo el contenido, pero estamos en dashboard, asumir éxito
                                if 'dashboard' in current_url.lower():
                                    login_completed = True
                                    print("\n✅ ¡Login completado! (dashboard detectado)")
                                    page.wait_for_timeout(3000)
                                    break
                        
                        page.wait_for_timeout(2000)
                        
                    except Exception as e:
                        page.wait_for_timeout(2000)
                        continue
                
                if not login_completed:
                    # Si no se completó automáticamente, intentar login manual como fallback
                    print("\n⚠ El login automático no completó en el tiempo esperado.")
                    print("   Verificando si ya estás logueado...")
                    
                    try:
                        page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(3000)
                        current_url = page.url
                        if 'dashboard' in current_url.lower():
                            login_completed = True
                            print("✅ Ya estás en el dashboard. Continuando...\n")
                    except:
                        pass
                
                if not login_completed:
                    raise Exception("No se pudo completar el login automático. Por favor, intenta manualmente.")
                
            except Exception as e:
                print(f"\n⚠ Error durante login automático: {str(e)}")
                print("   El navegador permanecerá abierto.")
                print("   Por favor, loguéate manualmente.")
                print("   El script detectará automáticamente cuando estés logueado y continuará.\n")
                
                # Esperar hasta 5 minutos a que el usuario se loguee manualmente
                max_wait_manual_login = 300000  # 5 minutos
                start_manual_login_time = datetime.now()
                login_manual_completed = False
                
                print("⏳ Esperando a que te loguees manualmente...")
                print("   El script verificará cada 3 segundos si ya estás logueado.")
                print("   Cuando detecte que estás en el dashboard, continuará automáticamente.\n")
                
                while (datetime.now() - start_manual_login_time).total_seconds() * 1000 < max_wait_manual_login:
                    try:
                        # Verificar la URL actual
                        current_url = page.url.lower()
                        
                        # Si estamos en dashboard, verificar que no hay desafío de Cloudflare
                        if 'dashboard' in current_url or 'cursor.com' in current_url:
                            # Verificar que no estamos en login/sign
                            if 'login' not in current_url and 'sign' not in current_url and 'authenticator' not in current_url:
                                try:
                                    # Verificar que la página tiene contenido del dashboard
                                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                                    page.wait_for_timeout(2000)
                                    
                                    # Buscar elementos característicos del dashboard
                                    try:
                                        # Buscar "Billing & Invoices" o elementos del sidebar
                                        page_text = page.inner_text("body").lower()
                                        
                                        # Verificar que no hay desafío de Cloudflare
                                        if 'can\'t verify' not in page_text and 'not a robot' not in page_text:
                                            # Si hay contenido del dashboard (billing, invoices, overview, etc.)
                                            if ('billing' in page_text or 'invoices' in page_text or 'overview' in page_text or 'dashboard' in page_text or 'settings' in page_text):
                                                login_manual_completed = True
                                                print("\n✅ ¡Login manual detectado! Estás en el dashboard.")
                                                print("   Continuando automáticamente con el proceso...\n")
                                                page.wait_for_timeout(2000)
                                                break
                                            # También verificar si no hay campos de login en la página
                                            elif 'email' not in page_text or ('email' in page_text and 'dashboard' in current_url):
                                                login_manual_completed = True
                                                print("\n✅ ¡Login manual detectado! (por contenido de página)")
                                                print("   Continuando automáticamente...\n")
                                                page.wait_for_timeout(2000)
                                                break
                                    except:
                                        # Si no podemos leer el contenido pero estamos en dashboard URL, asumir éxito
                                        if 'dashboard' in current_url:
                                            # Verificar una vez más que no estamos en login
                                            if 'login' not in current_url and 'sign' not in current_url and 'authenticator' not in current_url:
                                                login_manual_completed = True
                                                print("\n✅ ¡Login manual detectado! (por URL del dashboard)")
                                                print("   Continuando automáticamente...\n")
                                                page.wait_for_timeout(2000)
                                                break
                                        elif 'cursor.com' in current_url and 'login' not in current_url and 'sign' not in current_url and 'authenticator' not in current_url:
                                            # Estamos en cursor.com pero no en login, probablemente dashboard
                                            login_manual_completed = True
                                            print("\n✅ ¡Login manual detectado! (en cursor.com)")
                                            print("   Continuando automáticamente...\n")
                                            page.wait_for_timeout(2000)
                                            break
                                except:
                                    pass
                    except:
                        pass
                    
                    # Verificación rápida adicional: si ya estamos en dashboard, salir inmediatamente
                    try:
                        current_url_check = page.url.lower()
                        if 'dashboard' in current_url_check and 'login' not in current_url_check and 'sign' not in current_url_check and 'authenticator' not in current_url_check:
                            # Intentar verificar contenido rápidamente
                            try:
                                page_text_quick = page.inner_text("body", timeout=1000).lower()
                                if ('billing' in page_text_quick or 'overview' in page_text_quick or 'settings' in page_text_quick):
                                    login_manual_completed = True
                                    print("\n✅ ¡Login manual detectado! (verificación rápida)")
                                    print("   Continuando automáticamente...\n")
                                    break
                            except:
                                # Si no podemos leer el contenido pero URL es correcta, continuar
                                login_manual_completed = True
                                print("\n✅ ¡Login manual detectado! (URL verificada)")
                                print("   Continuando automáticamente...\n")
                                break
                    except:
                        pass
                    
                    page.wait_for_timeout(2000)  # Verificar cada 2 segundos (más rápido)
                
                if not login_manual_completed:
                    # Intentar navegar al dashboard como último recurso
                    try:
                        page.goto("https://cursor.com/dashboard", wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(2000)
                        current_url = page.url.lower()
                        if 'dashboard' in current_url and 'login' not in current_url and 'sign' not in current_url:
                            login_manual_completed = True
                            print("✅ Dashboard detectado. Continuando...\n")
                    except:
                        pass
                
                if not login_manual_completed:
                    # Última verificación: si la página actual no es login, asumir que estamos logueados
                    try:
                        current_url_final = page.url.lower()
                        if 'login' not in current_url_final and 'sign' not in current_url_final and 'authenticator' not in current_url_final:
                            if 'cursor.com' in current_url_final or 'dashboard' in current_url_final:
                                login_manual_completed = True
                                print("✅ Asumiendo login completado (URL no es de login). Continuando...\n")
                    except:
                        pass
                
                if not login_manual_completed:
                    raise Exception("No se detectó login manual después de 5 minutos. Por favor, loguéate manualmente y vuelve a ejecutar el script.")
            
            # Guardar cookies después del login manual (solo si no estamos usando perfil persistente)
            if not use_persistent_context:
                try:
                    browser_context_path = Path(".browser_context")
                    browser_context_path.mkdir(exist_ok=True)
                    state_file = browser_context_path / "state.json"
                    context.storage_state(path=str(state_file))
                    print("💾 Cookies guardadas exitosamente")
                    print("   ✓ En ejecuciones futuras se usarán estas cookies automáticamente\n")
                except Exception as e:
                    print(f"⚠ No se pudieron guardar cookies: {str(e)}")
            else:
                print("💾 Usando perfil persistente de Chrome (las cookies ya están guardadas)\n")
        
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
        
        # Buscar y hacer clic en "Billing & Invoices" del menú lateral
        print("🔍 Buscando sección 'Billing & Invoices' en el menú lateral...")
        billing_encontrado = False
        
        # Estrategia 1: Buscar directamente por texto en toda la página
        try:
            print("   Intentando estrategia 1: Buscar por texto exacto...")
            # Buscar todos los elementos que contengan "Billing & Invoices"
            billing_elements = page.get_by_text("Billing & Invoices", exact=False).all()
            for elem in billing_elements:
                try:
                    # Verificar que sea visible y clickeable
                    if elem.is_visible(timeout=2000):
                        elem.scroll_into_view_if_needed()
                        page.wait_for_timeout(1000)
                        # Verificar que no sea solo texto (debe ser un link o botón)
                        tag_name = elem.evaluate("el => el.tagName.toLowerCase()")
                        if tag_name in ['a', 'button', 'div', 'span', 'li']:
                            elem.click()
                            page.wait_for_timeout(3000)
                            billing_encontrado = True
                            print("✅ Encontrado y clickeado: 'Billing & Invoices'\n")
                            break
                except Exception as e:
                    continue
        except Exception as e:
            print(f"   ⚠ Error en estrategia 1: {str(e)[:50]}")
        
        # Estrategia 2: Buscar en el sidebar/menú lateral específicamente
        if not billing_encontrado:
            try:
                print("   Intentando estrategia 2: Buscar en sidebar...")
                # Buscar todos los elementos del menú que contengan "Billing" o "Invoice"
                sidebar_selectors = [
                    'nav a:has-text("Billing")',
                    'aside a:has-text("Billing")',
                    '[role="navigation"] a:has-text("Billing")',
                    'nav button:has-text("Billing")',
                    'aside button:has-text("Billing")',
                ]
                for selector in sidebar_selectors:
                    try:
                        elementos = page.locator(selector).all()
                        for elem in elementos:
                            try:
                                texto = elem.inner_text()
                                if "billing" in texto.lower() and "invoice" in texto.lower():
                                    if elem.is_visible(timeout=2000):
                                        elem.scroll_into_view_if_needed()
                                        page.wait_for_timeout(500)
                                        elem.click()
                                        page.wait_for_timeout(3000)
                                        billing_encontrado = True
                                        print(f"✅ Encontrado en sidebar: '{texto}'\n")
                                        break
                            except:
                                continue
                        if billing_encontrado:
                            break
                    except:
                        continue
            except Exception as e:
                print(f"   ⚠ Error en estrategia 2: {str(e)[:50]}")
        
        # Estrategia 3: Buscar usando XPath o localizadores más específicos
        if not billing_encontrado:
            try:
                print("   Intentando estrategia 3: Buscar en todos los enlaces...")
                # Buscar todos los enlaces y botones de la página
                all_links = page.locator('a, button, [role="link"], [role="button"]').all()
                for link in all_links[:20]:  # Primeros 20 elementos
                    try:
                        texto = link.inner_text().lower()
                        if "billing" in texto and "invoice" in texto:
                            if link.is_visible(timeout=2000):
                                link.scroll_into_view_if_needed()
                                page.wait_for_timeout(500)
                                link.click()
                                page.wait_for_timeout(3000)
                                billing_encontrado = True
                                print(f"✅ Encontrado en enlace: '{texto}'\n")
                                break
                    except:
                        continue
            except Exception as e:
                print(f"   ⚠ Error en estrategia 3: {str(e)[:50]}")
        
        # Estrategia 4: Buscar por el texto completo incluyendo el "&"
        if not billing_encontrado:
            try:
                print("   Intentando estrategia 4: JavaScript click directo...")
                # Usar JavaScript para encontrar y hacer clic directamente
                result = page.evaluate("""
                    () => {
                        const text = 'Billing & Invoices';
                        const elements = Array.from(document.querySelectorAll('*'));
                        for (let el of elements) {
                            if (el.innerText && el.innerText.includes(text)) {
                                // Encontrar el elemento clickeable (a o button)
                                let clickable = el;
                                while (clickable && !['A', 'BUTTON'].includes(clickable.tagName) && !clickable.onclick) {
                                    clickable = clickable.parentElement;
                                }
                                if (clickable && (clickable.tagName === 'A' || clickable.tagName === 'BUTTON' || clickable.onclick)) {
                                    clickable.scrollIntoView({behavior: 'smooth', block: 'center'});
                                    setTimeout(() => clickable.click(), 500);
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                if result:
                    page.wait_for_timeout(4000)
                    billing_encontrado = True
                    print("✅ Encontrado y clickeado con JavaScript: 'Billing & Invoices'\n")
            except Exception as e:
                print(f"   ⚠ Error en estrategia 4: {str(e)[:50]}")
        
        # Estrategia 5: Buscar por selectores CSS específicos del menú
        if not billing_encontrado:
            try:
                selectores = [
                    'a:has-text("Billing")[href*="billing"]',
                    'a:has-text("Billing")[href*="invoice"]',
                    '[data-testid*="billing" i]',
                    '[aria-label*="billing" i][aria-label*="invoice" i]',
                    'nav a, aside a, [role="navigation"] a',  # Todos los enlaces del menú
                ]
                for selector in selectores:
                    try:
                        elementos = page.locator(selector).all()
                        for elem in elementos:
                            try:
                                texto = elem.inner_text().lower()
                                if "billing" in texto and "invoice" in texto:
                                    elem.wait_for(state="visible", timeout=2000)
                                    elem.scroll_into_view_if_needed()
                                    page.wait_for_timeout(500)
                                    elem.click()
                                    page.wait_for_timeout(3000)
                                    billing_encontrado = True
                                    print(f"✅ Encontrado con selector CSS: {selector}\n")
                                    break
                            except:
                                continue
                        if billing_encontrado:
                            break
                    except:
                        continue
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
        
        # Esperar a que se cargue la nueva página
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(3000)
        
        # PASO 4: Bajar hacia abajo y buscar "HISTORIAL DE FACTURAS" o "Invoice History"
        print("🔍 Buscando 'HISTORIAL DE FACTURAS'...")
        print("   Haciendo scroll hacia abajo para encontrar la sección...")
        
        historial_found = False
        
        # Hacer scroll hacia abajo gradualmente para encontrar "HISTORIAL DE FACTURAS"
        scroll_steps = 5
        for i in range(scroll_steps):
            try:
                # Hacer scroll
                scroll_position = (i + 1) * (1.0 / scroll_steps)
                page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {scroll_position})")
                page.wait_for_timeout(1000)
                
                # Buscar "HISTORIAL DE FACTURAS" o "Invoice History"
                try:
                    historial_element = page.get_by_text("HISTORIAL DE FACTURAS", exact=False).first
                    if historial_element.is_visible(timeout=1000):
                        historial_element.scroll_into_view_if_needed()
                        historial_found = True
                        print("✅ Encontrado 'HISTORIAL DE FACTURAS'")
                        page.wait_for_timeout(1000)
                        break
                except:
                    try:
                        historial_element = page.get_by_text("Invoice History", exact=False).first
                        if historial_element.is_visible(timeout=1000):
                            historial_element.scroll_into_view_if_needed()
                            historial_found = True
                            print("✅ Encontrado 'Invoice History'")
                            page.wait_for_timeout(1000)
                            break
                    except:
                        pass
            except:
                continue
        
        # Si no se encontró, hacer scroll completo al final
        if not historial_found:
            print("   Haciendo scroll completo al final...")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            
            # Verificar si ahora está visible
            try:
                page_text = page.inner_text("body").lower()
                if "historial de facturas" in page_text or "invoice history" in page_text:
                    historial_found = True
                    print("✅ 'HISTORIAL DE FACTURAS' encontrado en la página")
            except:
                pass
        
        if not historial_found:
            print("⚠ No se encontró 'HISTORIAL DE FACTURAS', pero continuando a buscar facturas...")
        
        # Hacer scroll hacia arriba un poco para ver mejor las facturas
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.8)")
        page.wait_for_timeout(1000)
        
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
