import os
import base64
import json
import logging
import httpx
import re
import random
import string
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, CommandHandler, filters, ContextTypes

# =========================================================================
# BLOQUE 1: CONFIGURACIÓN, LOGGING Y VARIABLES DE ENTORNO
# =========================================================================
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ACCESS_ID = os.getenv("ACCESS_ID")
SECRET_KEY = os.getenv("SECRET_KEY")

# Credenciales SMTP cargadas desde el archivo .env
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

URL_TOKEN = "https://mx.teleows.com:443/adc-intg/api/intg/oauth2/token"
URL_ALARMAS = "https://300v-mx.teleows.com/adc-intg/api/rest/v1/MX_GNOC_Alarm_API/interface/interface_alarm/list"

# Variable global para filtrar eventos anteriores a esta fecha
FECHA_CORTE = "2025-01-01 00:00:00"

REGIONES_BASE = ["REGION CENTRO NORTE", "REGION CENTRO SUR", "REGION NORTE", "REGION SUR"]

IDS_PARCIAL = {
    "2G": ["21801", "28016"],
    "3G": ["28203", "22202"],
    "4G": ["29240"],
    "5G": ["29841"]
}

# --- CONFIGURACIÓN DE CASOS CRÍTICOS POR CATEGORÍA ---
CATEGORIAS_CRITICAS = {
    "FALLA_GEE": {
        "nombre": "ENTORNO_FALLA-DE-GEE",
        "ids": ["303", "65065", "65066", "65067", "65115", "65116", "65119", "10825", "13761", "1589", "202", "21946", "227", "2501150014", "2504230003", "2505120001", "2506060031", "2506060032", "2506060033", "2506090002", "2506090003", "2506090004", "2506090005", "2506090006", "36958", "40561", "40564", "9365"],
        "node_label": ["MAE_WLL", "NETECO"]
    },
    "ROBO_BATERIAS": {
        "nombre": "ENERGIA_ROBO_BATERIAS",
        "ids": ["25634", "65090", "13750", "13800", "13816", "13821", "61809", "903", "905"],
        "node_label": ["MAE_WLL", "NETECO"]
    },
    "PERDIDA_GESTION": {
        "nombre": "ACC_PERDIDA_GESTION_CONFIG",
        "ids": ["26753", "26757"],
        "node_label": ["MAE_WLL"]
    },
    "BAJO_COMBUSTIBLE": {
        "nombre": "ENTORNO_BAJO-NIVEL-COMBUSTIBLE",
        "ids": ["312", "65091", "65114", "65144", "2412100014", "2504160001", "2506060037", "301", "999100027"],
        "node_label": ["MAE_WLL", "NETECO"]
    },
    "FALLA_AA": {
        "nombre": "ENTORNO_FALLA_AIRE_ACONDICIONADO",
        "ids": ["65037", "65038", "65039", "65040", "65051", "65052", "65058", "65064", "65074", "65075", "65080", "65092", "65093", "65095", "65178", "2412100001", "2412100002", "2412100003", "2412100036", "2412100037", "2412100064", "2412100066", "2412100067", "2412100068", "2412100069", "2412100070", "2505090004", "2505090005", "2505090007", "2505130048", "2505130049", "2505130050", "2505130051", "2505290013", "2505290018", "2505290020", "2505290021", "2505290022", "2505290023", "2505290028", "2505290029", "2507030001"],
        "node_label": ["MAE_WLL", "NETECO"]
    },
    "FALLA_EQ_GESTION": {
        "nombre": "ENTORNO_FALLA EQ-GESTION",
        "ids": ["2501200006", "2502230002", "2502230003", "25602", "25628", "65034", "65041", "65056", "65088", "1030006", "10819", "10823", "11347", "13854", "13862", "1652", "17462", "19140", "22103", "2501150019", "2501160009", "2501160027", "2501200004", "2505130054", "313", "5200", "61625", "999999999"],
        "node_label": ["MAE_WLL", "NETECO"]
    },
    "GEE_RUNNING": {
        "nombre": "ENTORNO_GEE-RUNNING",
        "ids": ["65141", "65142", "2412100023", "2412100024", "2501200001", "2504250001", "2505170001"],
        "node_label": ["MAE_WLL", "NETECO"]
    }
}

# --- CONFIGURACIÓN DE KEEP-ALIVE PARA EL FIREWALL ---
LIMITES_RED = httpx.Limits(max_keepalive_connections=5, max_connections=10)


# =========================================================================
# BLOQUE 2: REGLAS DE NEGOCIO Y FILTRADO (EXTRACCIONES Y LIMPIEZA)
# =========================================================================

def es_fecha_valida(fecha_str, fecha_corte_str=FECHA_CORTE):
    """Valida si la fecha del evento es igual o superior a la fecha de corte."""
    if not fecha_str or fecha_str == "N/A": 
        return True 
    try:
        dt_evento = datetime.strptime(fecha_str, "%Y-%m-%d %H:%M:%S")
        dt_corte = datetime.strptime(fecha_corte_str, "%Y-%m-%d %H:%M:%S")
        return dt_evento >= dt_corte
    except:
        return True

def limpiar_nombre_sitio(sitename):
    """Remueve el código inicial del sitio cortando en el primer guion bajo."""
    s = str(sitename or "")
    if "_" in s:
        return s.split("_", 1)[1]
    return s if s else "SIN NOMBRE"

def extraer_cell_id(alertkey):
    """Extrae el Cell ID buscando patrones comunes en el alertkey."""
    if not alertkey:
        return "N/A"
    
    patrones = [
        r"Local\s+Cell\s+ID\s*=\s*(\d+)",
        r"NR\s+Cell\s+ID\s*=\s*(\d+)",
        r"Cell\s+ID\s*=\s*(\d+)"
    ]
    
    for p in patrones:
        resultado = re.search(p, alertkey, re.IGNORECASE)
        if resultado:
            return resultado.group(1)
            
    return "N/A"


def es_small_cell(sitename):
    """Detecta si un sitio es Small Cell por su nombre."""
    if not sitename:
        return False
    return "_SC_" in sitename.upper()


def es_sitio_excluido_san_borja(sitename, location):
    """
    Filtra de forma local e infalible el sitio San Borja MSO
    por su código único o coincidencia de texto.
    """
    s_name = str(sitename or "").upper()
    loc_str = str(location or "")
    
    if "0130522" in loc_str or "0130522_LM_IB_MSO_SAN_BORJA" in s_name:
        return True
    return False


def identificar_tecnologia(alarm_id_recibido):
    """Retorna la tecnología comparando el ID de la alarma (alarmid)."""
    aid = str(alarm_id_recibido)
    for tech, lista_ids in IDS_PARCIAL.items():
        if aid in lista_ids:
            return tech
    return "N/A"


def eliminar_duplicados_universal(lista_alarmas, is_all_tech, is_parcial, is_critico_or_energia):
    """
    Filtra y remueve duplicados exactos basándose en los campos dinámicos
    de cada tipo de vista, evaluando el sitename original de la API.
    """
    vistas = set()
    lista_limpia = []
    
    for a in lista_alarmas:
        if is_all_tech:
            huella = (a['tech'], a['cellid'], a['prioridad'], a['sitename'], a['tiempo'])
        elif is_parcial:
            huella = (a['cellid'], a['prioridad'], a['sitename'], a['tiempo'])
        elif is_critico_or_energia:
            huella = (a['prioridad'], a['alarmname'], a['sitename'], a['tiempo'])
        else: # Afectación Total
            huella = (a['prioridad'], a['sitename'], a['tiempo'])
            
        if huella not in vistas:
            vistas.add(huella)
            lista_limpia.append(a)
            
    return lista_limpia


# =========================================================================
# BLOQUE 3: CONEXIÓN ASÍNCRONA CON LA API (TOKENS Y PAGINACIÓN)
# =========================================================================

async def obtener_token():
    """Solicita el token OAuth2 básico a la API."""
    async with httpx.AsyncClient(verify=False, limits=LIMITES_RED) as client:
        try:
            credenciales = f"{ACCESS_ID}:{SECRET_KEY}"
            basic = base64.b64encode(credenciales.encode()).decode()
            headers = {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}
            r = await client.post(URL_TOKEN, headers=headers, data="grant_type=client_credentials", timeout=20)
            r.raise_for_status()
            return r.json().get("access_token")
        except Exception as e:
            logger.error(f"Error Crítico al obtener Token: {e}")
            return None


async def consultar_api_y_filtrar(context, region_objetivo, alarm_ids, node_labels=None, allowed_priorities=None):
    """Consulta la API de alarmas paginando y aplicando los filtros por Región, Gestores y Prioridades Permitidas."""
    token = context.bot_data.get('access_token')
    if not token:
        token = await obtener_token()
        context.bot_data['access_token'] = token

    if node_labels is None:
        node_labels = ["MAE_WLL"]

    ahora = datetime.now()
    alarmas_procesadas = []
    MAX_REGISTROS = 2500 
    PASO = 500

    async with httpx.AsyncClient(verify=False, limits=LIMITES_RED) as client:
        for start_index in range(0, MAX_REGISTROS, PASO):
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            
            body = {
                "type": "1", 
                "limit": str(PASO), 
                "start": str(start_index),
                "dir": "DESC", 
                "sort": "lastoccurrence",
                "filters": {
                    "alarm_id": alarm_ids, 
                    "node_label": node_labels
                }
            }

            try:
                r = await client.post(URL_ALARMAS, headers=headers, json=body, timeout=40)
                if r.status_code == 401:
                    token = await obtener_token()
                    context.bot_data['access_token'] = token
                    headers["Authorization"] = f"Bearer {token}"
                    r = await client.post(URL_ALARMAS, headers=headers, json=body, timeout=40)

                if r.status_code != 200: 
                    logger.error(f"Error de API AUTIN: Código {r.status_code}")
                    break
                
                results = r.json().get("result", {}).get("results", [])
                if not results:
                    break

                for alarma in results:
                    if alarma.get("severity") == "Uncleared":
                        prio = alarma.get("sitepriority", "N/A")
                        
                        # --- FILTRADO DE PRIORIDADES QUIRÚRGICO MEDIANTE LISTA ---
                        if allowed_priorities is not None:
                            if str(prio).upper().strip() not in allowed_priorities:
                                continue

                        s_name = alarma.get("sitename", "")
                        l_code = alarma.get("location", "")

                        if es_small_cell(s_name):
                            continue

                        if es_sitio_excluido_san_borja(s_name, l_code):
                            continue

                        # --- FILTRO COMPUESTO DE REGIONES ---
                        loc_api = str(l_code or "").upper().strip()
                        loc_api_norm = loc_api.replace("Ó", "O")
                        incluir = False
                        
                        if region_objetivo == "OTROS":
                            if not any(reg.replace("Ó", "O") in loc_api_norm for reg in REGIONES_BASE):
                                incluir = True
                        elif region_objetivo == "LIMA":
                            targets = ["REGION CENTRO NORTE", "REGION CENTRO SUR"]
                            if any(t in loc_api_norm for t in targets):
                                incluir = True
                        elif region_objetivo == "REGIONES":
                            targets = ["REGION NORTE", "REGION SUR"]
                            if any(t in loc_api_norm for t in targets):
                                incluir = True
                        else:
                            target = region_objetivo.upper().strip().replace("Ó", "O")
                            if target in loc_api_norm:
                                incluir = True

                        if incluir:
                            inicio_str = alarma.get("lastoccurrence", "N/A")
                            
                            # --- FILTRO ESTRICTO POR FECHA DE CORTE ---
                            if not es_fecha_valida(inicio_str):
                                continue

                            segundos_duracion = 0
                            tiempo_txt = "N/A"
                            
                            try:
                                dt_inicio = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S")
                                duracion = ahora - dt_inicio
                                segundos_duracion = duracion.total_seconds()
                                tiempo_txt = f"{duracion.days}d {duracion.seconds // 3600}h {(duracion.seconds // 60) % 60}m"
                            except:
                                segundos_duracion = 99999999

                            cid = extraer_cell_id(alarma.get("alertkey", ""))
                            tech_label = identificar_tecnologia(alarma.get("alarmid", ""))

                            alarmas_procesadas.append({
                                "sitename": s_name if s_name else "SIN NOMBRE",
                                "tiempo": tiempo_txt,
                                "segundos": segundos_duracion,
                                "prioridad": prio,
                                "cellid": cid,
                                "tech": tech_label,
                                "alarmname": alarma.get("alarmname", "N/A")
                            })
                
                if len(results) < PASO: 
                    break
            except Exception as e: 
                logger.error(f"Excepción durante consulta API: {e}")
                break
        
    return alarmas_procesadas


# =========================================================================
# BLOQUE 3.5: MÓDULO DE SEGURIDAD OTP Y FILTRADO POR LISTA BLANCA
# =========================================================================
VENTANA_INACTIVIDAD_MINUTOS = 15
DIRECTORIO_ACTUAL = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_LISTA_BLANCA = os.path.join(DIRECTORIO_ACTUAL, "lista_blanca.json")
memoria_seguridad = {} # Control de estados y marcas de tiempo en la RAM

def cargar_lista_blanca():
    """Lee de forma nativa la lista blanca externa para validar accesos."""
    if not os.path.exists(ARCHIVO_LISTA_BLANCA):
        with open(ARCHIVO_LISTA_BLANCA, "w") as f:
            json.dump({}, f)
        return {}
    with open(ARCHIVO_LISTA_BLANCA, "r") as f:
        try:
            return json.load(f)
        except:
            return {}

def generar_otp():
    """Genera token seguro de 9 caracteres alfanuméricos."""
    caracteres = string.ascii_uppercase + string.digits
    return ''.join(random.choice(caracteres) for _ in range(9))

def enviar_correo_otp(destino, nombre_usuario, otp_code):
    """Establece túnel TLS y envía la alerta corporativa con el código OTP."""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = destino
        msg['Subject'] = "[EXTERNO] - Solicitud de Acceso - Código OTP"

        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px;">
            <div style="background-color: #ffffff; max-width: 600px; margin: 0 auto; padding: 30px; border-radius: 8px;">
                <p style="color: #ff3300; font-weight: bold;">ALERTA ENTEL O&M - Mensaje de seguridad interna.</p>
                <p>Estimado(a) {nombre_usuario},</p>
                <p>Hemos recibido una solicitud de inicio de sesión en el Bot de O&M Sitios la cuál requiere el siguiente código OTP:</p>
                <div style="background-color: #f0f0f0; text-align: center; padding: 15px; font-size: 24px; font-weight: bold; letter-spacing: 2px; border-radius: 5px; margin: 20px 0;">
                    {otp_code}
                </div>
                <p style="font-size: 12px; color: #666;">Por favor, utilice este código con precaución. Válido por un único uso antes de expirar por inactividad.</p>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(html, 'html'))
        
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, destino, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        logger.error(f"Error crítico despachando correo OTP: {e}")
        return False

def verificar_estado_sesion(user_id):
    """Compara de forma pasiva la hora de actividad para refrescar el reloj flotante."""
    if user_id not in memoria_seguridad:
        return "NO_INICIADO"
    
    datos = memoria_seguridad[user_id]
    if datos["estado"] == "AUTORIZADO":
        ahora = datetime.now()
        if ahora - datos["ultima_actividad"] > timedelta(minutes=VENTANA_INACTIVIDAD_MINUTOS):
            datos["estado"] = "BLOQUEADO"
            return "CADUCADO"
        else:
            datos["ultima_actividad"] = ahora
            return "AUTORIZADO"
    return datos["estado"]

async def ejecutar_flujo_despacho_otp(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, info_usuario):
    """Genera, registra en RAM y envía el código OTP al destino de la lista blanca."""
    nombre_completo = info_usuario["nombre_completo"]
    correo_destino = info_usuario["correo"]
    
    otp = generar_otp()
    memoria_seguridad[user_id] = {
        "otp": otp,
        "estado": "ESPERANDO_OTP",
        "ultima_actividad": datetime.now()
    }
    
    saludo = f"🔄 ¡Hola <b>{nombre_completo}</b>!\nIdentidad verificada en Lista Blanca. Generando tu código dinámico..."
    if update.message:
        await update.message.reply_text(saludo, parse_mode='HTML')
    else:
        await update.callback_query.message.reply_text(saludo, parse_mode='HTML')
        
    exito = enviar_correo_otp(correo_destino, nombre_completo, otp)
    
    msg_final = f"📩 He enviado un código de seguridad de 9 dígitos a tu correo registrado (<code>{correo_destino}</code>).\n\nPor favor, escríbelo o pégalo aquí para ingresar:" if exito else "❌ Hubo un inconveniente al despachar el correo OTP. Contacta al administrador de O&M."
    
    if update.message:
        await update.message.reply_text(msg_final, parse_mode='HTML')
    else:
        await update.callback_query.message.reply_text(msg_final, parse_mode='HTML')

async def manejador_mensajes_seguro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Filtro middleware para interceptar y validar mensajes de texto e ingresos de tokens."""
    user_id = str(update.effective_user.id)
    texto = update.message.text.strip() if update.message.text else ""
    
    lista_blanca = cargar_lista_blanca()
    
    if user_id not in lista_blanca:
        await update.message.reply_text(
            f"❌ Acceso Denegado.\nTu Telegram ID ({user_id}) no está registrado en la lista blanca de O&M Sitios."
        )
        return

    estado = verificar_estado_sesion(user_id)
    
    if estado == "CADUCADO":
        await update.message.reply_text("⚠️ Tu sesión ha caducado por inactividad. Por seguridad, se requiere una nueva validación.")
        await ejecutar_flujo_despacho_otp(update, context, user_id, lista_blanca[user_id])
        return
        
    if estado == "ESPERANDO_OTP":
        if texto == memoria_seguridad[user_id]["otp"]:
            memoria_seguridad[user_id]["estado"] = "AUTORIZADO"
            memoria_seguridad[user_id]["ultima_actividad"] = datetime.now()
            await update.message.reply_text(
                f"✅ ¡Acceso concedido! Tu sesión estará activa mientras interactúes con el bot (Ventana: {VENTANA_INACTIVIDAD_MINUTOS} min de inactividad)."
            )
            await inicio(update, context)
        else:
            await update.message.reply_text("❌ Código incorrecto. Verifica tu bandeja e inténtalo nuevamente.")
        return
        
    if estado == "AUTORIZADO":
        await inicio(update, context)
    else:
        await ejecutar_flujo_despacho_otp(update, context, user_id, lista_blanca[user_id])

async def manejador_callback_seguro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Filtro middleware para interceptar pulsaciones de teclados Inline."""
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    lista_blanca = cargar_lista_blanca()
    if user_id not in lista_blanca:
        await query.answer("Acceso denegado.", show_alert=True)
        return
        
    estado = verificar_estado_sesion(user_id)
    
    if estado == "AUTORIZADO":
        await manejar_callback(update, context)
    elif estado == "CADUCADO":
        await query.answer("Sesión expirada por inactividad.", show_alert=True)
        await query.message.reply_text("⚠️ Tu sesión ha caducado por inactividad. Por seguridad, se requiere un nuevo código.")
        await ejecutar_flujo_despacho_otp(update, context, user_id, lista_blanca[user_id])
    else:
        await query.answer("Autenticación requerida.", show_alert=True)
        await ejecutar_flujo_despacho_otp(update, context, user_id, lista_blanca[user_id])


# =========================================================================
# BLOQUE 4: MÓDULO DE INTERFAZ DE TELEGRAM (MENÚS Y BOTONES)
# =========================================================================

async def inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Despliega el menú principal con las regiones de O&M."""
    # --- REQUERIMIENTO 3: MENÚ FORMATO 2x1x2x1x1 ---
    keyboard = [
        [InlineKeyboardButton("Region centro Norte", callback_data="reg_REGION CENTRO NORTE"), 
         InlineKeyboardButton("Region centro Sur", callback_data="reg_REGION CENTRO SUR")],
        [InlineKeyboardButton("Lima", callback_data="reg_LIMA")],
        [InlineKeyboardButton("Region Norte", callback_data="reg_REGION NORTE"), 
         InlineKeyboardButton("Region Sur", callback_data="reg_REGION SUR")],
        [InlineKeyboardButton("Regiones", callback_data="reg_REGIONES")],
        [InlineKeyboardButton("Otros", callback_data="reg_OTROS")]
    ]
    
    texto = "🛰️ <b>MENU DE ALARMAS AUTIN</b>\nSeleccione una Región Geográfica:"
    if update.message:
        await update.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update.callback_query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador centralizado de pulsaciones de botones (Lógica de flujo de navegación)."""
    query = update.callback_query
    usuario = update.effective_user.first_name
    await query.answer()
    
    if query.data.startswith("reg_"):
        region_sel = query.data.replace("reg_", "")
        context.user_data['region'] = region_sel
        
        keyboard = [
            [InlineKeyboardButton("🔥 Casos Críticos", callback_data='menu_criticos')],
            [InlineKeyboardButton("🚨 Afectación Total", callback_data='run_total')],
            [InlineKeyboardButton("⚠️ Afectación Parcial", callback_data='menu_parcial')],
            [InlineKeyboardButton("⚡ Cortes de Energía", callback_data='run_energia')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]
        await query.edit_message_text(f"📍 <b>Región:</b> {region_sel}\n¿Qué desea consultar?", 
                                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    elif query.data == "menu_parcial":
        region = context.user_data.get('region')
        keyboard = [
            [InlineKeyboardButton("📶 2G", callback_data='parcial_2G'), InlineKeyboardButton("📶 3G", callback_data='parcial_3G')],
            [InlineKeyboardButton("📶 4G", callback_data='parcial_4G'), InlineKeyboardButton("📶 5G", callback_data='parcial_5G')],
            [InlineKeyboardButton("🌐 Todas las Tecnologías", callback_data='parcial_ALL')],
            [InlineKeyboardButton("🔙 Atrás", callback_data=f"reg_{region}")]
        ]
        await query.edit_message_text(f"📍 <b>{region}</b>\nSeleccione tecnología:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    elif query.data == "menu_criticos":
        region = context.user_data.get('region')
        keyboard = [
            [InlineKeyboardButton("💥 Todos los Casos Críticos", callback_data='critico_ALL')],
            [InlineKeyboardButton("⚙️ Falla de GEE", callback_data='critico_FALLA_GEE')],
            [InlineKeyboardButton("🔋 Robo Baterías", callback_data='critico_ROBO_BATERIAS')],
            [InlineKeyboardButton("📡 Pérdida Gestión", callback_data='critico_PERDIDA_GESTION')],
            [InlineKeyboardButton("⛽ Bajo Combustible", callback_data='critico_BAJO_COMBUSTIBLE')],
            [InlineKeyboardButton("❄️ Falla A/A", callback_data='critico_FALLA_AA')],
            [InlineKeyboardButton("💻 Falla Eq. Gestión", callback_data='critico_FALLA_EQ_GESTION')],
            [InlineKeyboardButton("🚀 GEE Running", callback_data='critico_GEE_RUNNING')],
            [InlineKeyboardButton("🔙 Atrás", callback_data=f"reg_{region}")]
        ]
        await query.edit_message_text(f"📍 <b>{region}</b>\nSeleccione Categoría Crítica:", 
                                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    elif query.data.startswith("critico_"):
        region = context.user_data.get('region')
        cat_key = query.data.replace("critico_", "")
        
        keyboard = [
            [InlineKeyboardButton("📊 Prioridades P0+, P0 y P1", callback_data=f"cprio_ALL_{cat_key}")],
            [InlineKeyboardButton("📈 Prioridad P0+ y P0", callback_data=f"cprio_P0_{cat_key}")],
            [InlineKeyboardButton("📉 Prioridad P1", callback_data=f"cprio_P1_{cat_key}")],
            [InlineKeyboardButton("🔙 Atrás", callback_data="menu_criticos")]
        ]
        await query.edit_message_text(f"📍 <b>{region}</b>\nSeleccione el filtro de prioridad para el reporte:", 
                                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    elif query.data in ["run_total", "run_energia"] or query.data.startswith("parcial_") or query.data.startswith("cprio_"):
        region = context.user_data.get('region')
        is_parcial = query.data.startswith("parcial_")
        is_all_tech = query.data == "parcial_ALL"
        is_energia = query.data == "run_energia"
        is_critico = query.data.startswith("cprio_")
        is_critico_or_energia = is_critico or is_energia

        node_labels_peticion = ["MAE_WLL"]
        allowed_priorities_peticion = None

        if query.data == "run_total":
            ids, sub = ["301"], "AFECTACION TOTAL"
            
        elif is_energia:
            ids_mae = ["25622", "65050", "65059", "65071", "65122", "65163"]
            ids_neteco = ["10202", "10209", "10210", "10212", "10213", "11331", "11357", "13801", "1550", "1586", "1587", "1588", "21978", "2412100039", "2507300001", "36977", "50010", "70736", "70755", "70774", "999100224", "999100226"]
            ids = ids_mae + ids_neteco
            sub = "CORTES DE ENERGIA"
            node_labels_peticion = ["MAE_WLL", "NETECO"]
            allowed_priorities_peticion = None 
            
        elif is_all_tech:
            ids = [item for sublist in IDS_PARCIAL.values() for item in sublist]
            sub = "AFECTACION PARCIAL TOTAL"
            
        elif is_parcial:
            tech = query.data.replace("parcial_", "")
            ids, sub = IDS_PARCIAL.get(tech, []), f"AFECTACION PARCIAL {tech}"
            
        elif is_critico:
            parts = query.data.split("_")
            prio_type = parts[1]
            cat_key = "_".join(parts[2:]) 
            
            if prio_type == "ALL":
                allowed_priorities_peticion = ["P0+", "P0", "P1"]
            elif prio_type == "P0":
                allowed_priorities_peticion = ["P0+", "P0"]
            elif prio_type == "P1":
                allowed_priorities_peticion = ["P1"]

            if cat_key == "ALL":
                ids = list(set([id_alarma for cat in CATEGORIAS_CRITICAS.values() for id_alarma in cat["ids"]]))
                sub = "TODOS LOS CASOS CRITICOS"
                node_labels_peticion = ["MAE_WLL", "NETECO"]
            else:
                config_cat = CATEGORIAS_CRITICAS.get(cat_key, {})
                ids = config_cat.get("ids", [])
                sub = config_cat.get("nombre", "CASO CRITICO")
                node_labels_peticion = config_cat.get("node_label", ["MAE_WLL", "NETECO"])

        logger.info(f"Procesando: {usuario} solicita {sub} en {region}")
        await query.edit_message_text(f"🔍 Consultando {sub} en {region}...")
        
        alarmas = await consultar_api_y_filtrar(
            context, 
            region, 
            ids, 
            node_labels=node_labels_peticion, 
            allowed_priorities=allowed_priorities_peticion
        )

        if not alarmas:
            await query.edit_message_text(f"✅ No hay alarmas de <b>{sub}</b> en {region}.", parse_mode='HTML')
            return

        # --- REQUERIMIENTO 1: FILTRO DE DUPLICADOS APLICADO A TODAS LAS VISTAS ---
        alarmas = eliminar_duplicados_universal(alarmas, is_all_tech, is_parcial, is_critico_or_energia)

        alarmas.sort(key=lambda x: x['segundos'])

        cabecera = f"📊 <b>REPORTE COMPLETO: {region}</b>\n🎯 <b>{sub}</b>\n🔥 Total casos: {len(alarmas)}\n\n"
        
        if is_all_tech:
            cabecera += "<b>TEC | CID | Prio | Site_name | Tiempo</b>\n"
        elif is_parcial:
            cabecera += "<b>CID | Prio | Site_name | Tiempo</b>\n"
        elif is_critico_or_energia:
            cabecera += "<b>Prio | Alarma | Site_name | Tiempo</b>\n"
        else:
            cabecera += "<b>Prio | Site_name | Tiempo</b>\n"

        mensajes = []
        contenido_actual = cabecera

        for a in alarmas:
            prio_bold = f"<b>{a['prioridad']}</b>"
            tiempo_bold = f"<b>{a['tiempo']}</b>"
            
            # --- REQUERIMIENTO 2: LIMPIEZA DE FORMATO DEL SITE NAME SOLO PARA LA VISTA ---
            site_name_limpio = limpiar_nombre_sitio(a['sitename'])

            if is_all_tech:
                linea = f"{a['tech']} - {a['cellid']} - {prio_bold} - <code>{site_name_limpio}</code> - {tiempo_bold}\n"
            elif is_parcial:
                linea = f"{a['cellid']} - {prio_bold} - <code>{site_name_limpio}</code> - {tiempo_bold}\n"
            elif is_critico_or_energia:
                linea = f"{prio_bold} - {a['alarmname']} - <code>{site_name_limpio}</code> - {tiempo_bold}\n"
            else:
                linea = f"{prio_bold} - <code>{site_name_limpio}</code> - {tiempo_bold}\n"
            
            if len(contenido_actual) + len(linea) > 4000:
                mensajes.append(contenido_actual)
                contenido_actual = linea 
            else:
                contenido_actual += linea
        
        mensajes.append(contenido_actual)

        for i, texto_msg in enumerate(mensajes):
            if i == 0:
                await query.edit_message_text(texto_msg, parse_mode='HTML')
            else:
                await context.bot.send_message(chat_id=query.message.chat_id, text=texto_msg, parse_mode='HTML')

    elif query.data == "volver_inicio": 
        await inicio(update, context)


# =========================================================================
# BLOQUE 5: PUNTO DE ENTRADA DE EJECUCIÓN (MAIN)
# =========================================================================

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", manejador_mensajes_seguro))
    app.add_handler(MessageHandler(filters.TEXT, manejador_mensajes_seguro))
    app.add_handler(CallbackQueryHandler(manejador_callback_seguro))
    
    print("---------------------------------------")
    print("   BOT DE ALARMAS AUTIN CON SEGURIDAD OTP")
    print("---------------------------------------")
    logger.info("Bot en espera de mensajes seguros...")
    app.run_polling()