"""
TikTok Live Comment Reader - Backend para app móvil (Con Logs detallados)
Corre en servidor Linux 24/7

Instalación:
    pip install TikTokLive edge-tts flask flask-cors flask-socketio python-socketio aiohttp

Uso:
    python backend_servidor_con_logs.py
"""

import asyncio
import json
import re
import tempfile
import os
from collections import deque
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent
import edge_tts
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import threading
import base64
import ssl
import logging
from datetime import datetime

# ─── Configuración de Logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)-8s: %(message)s',
    datefmt='%d/%m/%Y %H:%M:%S',
    handlers=[
        logging.StreamHandler(),  # Mostrar en terminal
        logging.FileHandler('backend.log')  # Guardar en archivo
    ]
)
logger = logging.getLogger(__name__)

# ─── Configuración ────────────────────────────────────────────────────────────

VOZ = "es-MX-DaliaNeural"
VELOCIDAD = "+30%"
VOLUMEN = "+0%"
MAX_COLA = 30
MAX_CHARS = 150

LISTA_NEGRA_USUARIOS = set()
LISTA_NEGRA_PALABRAS = set()

# Puerto del servidor
PUERTO = 5080
HOST = "0.0.0.0"  # Accesible desde cualquier IP

# ─── Configuración SSL/HTTPS ──────────────────────────────────────────────────
CERT_FILE = None
KEY_FILE  = None

# Auto-detectar certificados
if not CERT_FILE or not KEY_FILE or not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
    logger.warning("⚠️  Certificados auto-firmados no encontrados")
    logger.warning(f"   Buscados en: {CERT_FILE} y {KEY_FILE}")
    CERT_FILE = None
    KEY_FILE = None
else:
    logger.info("✅ Certificados auto-firmados detectados")

# ─── Flask + SocketIO ─────────────────────────────────────────────────────────

logger.info("Inicializando Flask y SocketIO...")
app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", logger=True, engineio_logger=True)

logger.info("✅ Flask y SocketIO inicializados")

# Estado global
cola_comentarios = deque(maxlen=MAX_COLA)
cliente_tiktok = None
live_activo = False
usuario_actual = None
clientes_conectados = set()

# ─── Utilidades ───────────────────────────────────────────────────────────────

def limpiar_texto(texto: str) -> str:
    texto = re.sub(r'[^\x00-\x7F\u00C0-\u024F\u00A0-\u024F]+', '', texto)
    texto = re.sub(r'http\S+', '', texto)
    texto = ' '.join(texto.split())
    return texto.strip()

def esta_en_lista_negra(usuario: str, texto: str) -> bool:
    if usuario.lower() in LISTA_NEGRA_USUARIOS:
        return True
    texto_lower = texto.lower()
    return any(p in texto_lower for p in LISTA_NEGRA_PALABRAS)

async def texto_a_voz(texto: str) -> bytes:
    """Convierte texto a audio MP3 y devuelve los bytes."""
    logger.debug(f"Generando voz para: '{texto}'")
    communicate = edge_tts.Communicate(
        texto,
        voice=VOZ,
        rate=VELOCIDAD,
        volume=VOLUMEN
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    await communicate.save(tmp.name)
    
    with open(tmp.name, 'rb') as f:
        audio_bytes = f.read()
    
    os.unlink(tmp.name)
    logger.debug(f"✓ Voz generada ({len(audio_bytes)} bytes)")
    return audio_bytes

async def procesador_cola():
    """Procesa la cola de comentarios."""
    logger.info("Procesador de cola iniciado")
    while True:
        try:
            if cola_comentarios:
                usuario, texto = cola_comentarios.popleft()
                frase = f"{usuario} dice: {texto}"
                
                logger.info(f"🔊 Reproduciendo: {frase}")
                
                # Generar audio
                audio_bytes = await texto_a_voz(frase)
                audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
                
                # Enviar a todos los clientes conectados
                logger.debug(f"Enviando audio a {len(clientes_conectados)} clientes")
                socketio.emit('reproducir_audio', {
                    'usuario': usuario,
                    'texto': texto,
                    'audio': audio_base64,
                    'timestamp': int(asyncio.get_event_loop().time() * 1000)
                }, broadcast=True)
            else:
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"⚠️  Error procesando cola: {e}", exc_info=True)
            await asyncio.sleep(1)

def crear_cliente_tiktok(usuario: str):
    """Crea cliente TikTok para un usuario específico."""
    global cliente_tiktok, live_activo, usuario_actual
    
    logger.info(f"Creando cliente TikTok para: @{usuario}")
    client = TikTokLiveClient(unique_id=usuario)
    usuario_actual = usuario

    @client.on(ConnectEvent)
    async def al_conectar(event: ConnectEvent):
        global live_activo
        live_activo = True
        logger.info(f"✅ Conectado al live de @{usuario}")
        socketio.emit('live_conectado', {
            'usuario': usuario,
            'mensaje': f'Conectado al live de @{usuario}'
        }, broadcast=True)

    @client.on(DisconnectEvent)
    async def al_desconectar(event: DisconnectEvent):
        global live_activo
        live_activo = False
        logger.info(f"❌ Live de @{usuario} terminado")
        socketio.emit('live_desconectado', {
            'usuario': usuario,
            'mensaje': 'Live terminado o conexión perdida'
        }, broadcast=True)

    @client.on(CommentEvent)
    async def al_comentar(event: CommentEvent):
        usuario_comentario = event.user.nickname or event.user.unique_id
        texto = event.comment

        if esta_en_lista_negra(usuario_comentario, texto):
            logger.debug(f"Comentario ignorado (lista negra): {usuario_comentario}")
            return

        texto_limpio = limpiar_texto(texto)
        if not texto_limpio:
            logger.debug(f"Comentario ignorado (vacío después de limpiar): {usuario_comentario}")
            return

        if len(texto_limpio) > MAX_CHARS:
            texto_limpio = texto_limpio[:MAX_CHARS] + "..."

        logger.info(f"💬 {usuario_comentario}: {texto}")
        cola_comentarios.append((usuario_comentario, texto_limpio))
        
        # Notificar a clientes que hay un nuevo comentario
        socketio.emit('nuevo_comentario', {
            'usuario': usuario_comentario,
            'texto': texto_limpio,
            'timestamp': int(asyncio.get_event_loop().time() * 1000)
        }, broadcast=True)

    return client

# ─── Routes HTTP ──────────────────────────────────────────────────────────────

@app.route('/api/status', methods=['GET'])
def status():
    """Devuelve el estado actual del servidor."""
    logger.debug(f"GET /api/status desde {request.remote_addr}")
    return jsonify({
        'live_activo': live_activo,
        'usuario': usuario_actual,
        'comentarios_en_cola': len(cola_comentarios),
        'clientes_conectados': len(clientes_conectados)
    })

@app.route('/api/iniciar', methods=['POST'])
def iniciar_live():
    """Inicia la conexión a un live de TikTok."""
    global cliente_tiktok
    
    logger.info(f"POST /api/iniciar desde {request.remote_addr}")
    
    try:
        data = request.json
        usuario = data.get('usuario', '').strip()
        
        logger.debug(f"Usuario solicitado: {usuario}")
        
        if not usuario:
            logger.warning("Usuario vacío")
            return jsonify({'error': 'Usuario requerido'}), 400
        
        if not usuario.startswith('@'):
            usuario = '@' + usuario
        
        if cliente_tiktok:
            logger.warning(f"Ya hay un live activo: {usuario_actual}")
            return jsonify({'error': 'Ya hay un live activo'}), 400
        
        logger.info(f"🔄 Intentando conectar a {usuario}...")
        cliente_tiktok = crear_cliente_tiktok(usuario)
        
        # Iniciar en thread separado
        def iniciar_tiktok():
            try:
                logger.info(f"Iniciando cliente TikTok en thread...")
                asyncio.run(cliente_tiktok.start())
            except Exception as e:
                logger.error(f"Error en cliente TikTok: {e}", exc_info=True)
        
        threading.Thread(target=iniciar_tiktok, daemon=True).start()
        
        logger.info(f"✓ Cliente TikTok iniciado para {usuario}")
        return jsonify({
            'exito': True,
            'mensaje': f'Conectando a @{usuario}...'
        })
    except Exception as e:
        logger.error(f"Error en /api/iniciar: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/detener', methods=['POST'])
def detener_live():
    """Detiene la conexión actual."""
    global cliente_tiktok, live_activo
    
    logger.info(f"POST /api/detener desde {request.remote_addr}")
    
    if cliente_tiktok:
        try:
            logger.info("Deteniendo cliente TikTok...")
            cliente_tiktok.disconnect()
        except Exception as e:
            logger.error(f"Error deteniendo cliente: {e}")
        cliente_tiktok = None
        live_activo = False
    
    return jsonify({'exito': True, 'mensaje': 'Live detenido'})

@app.route('/api/comentarios', methods=['GET'])
def obtener_comentarios():
    """Devuelve los últimos comentarios."""
    logger.debug(f"GET /api/comentarios desde {request.remote_addr}")
    return jsonify({
        'comentarios': list(cola_comentarios)
    })

# ─── WebSocket Events ─────────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    logger.info(f"📱 CLIENTE CONECTADO: {request.sid} desde {request.remote_addr}")
    clientes_conectados.add(request.sid)
    logger.info(f"   Total clientes: {len(clientes_conectados)}")
    
    # Enviar estado actual
    logger.debug(f"Enviando estado inicial al cliente {request.sid}")
    emit('estado', {
        'live_activo': live_activo,
        'usuario': usuario_actual,
        'comentarios_en_cola': len(cola_comentarios)
    })

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"📱 CLIENTE DESCONECTADO: {request.sid}")
    clientes_conectados.discard(request.sid)
    logger.info(f"   Total clientes: {len(clientes_conectados)}")

@socketio.on('ping')
def handle_ping():
    """Keep-alive desde el cliente."""
    logger.debug(f"📍 PING desde {request.sid}")
    emit('pong')

# ─── Main ─────────────────────────────────────────────────────────────────────

def correr_procesador():
    """Corre el procesador de cola en un event loop separado."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(procesador_cola())

if __name__ == '__main__':
    protocolo = "HTTPS" if (CERT_FILE and KEY_FILE) else "HTTP"
    url = f"https://0.0.0.0:{PUERTO}" if (CERT_FILE and KEY_FILE) else f"http://0.0.0.0:{PUERTO}"
    
    logger.info(f"""
╔════════════════════════════════════════════╗
║  TikTok Live Reader - Backend             ║
║  Protocolo: {protocolo:<28}║
║  Puerto: {PUERTO:<36}║
║  URL: {url:<37}║
╚════════════════════════════════════════════╝
    """)
    
    # Iniciar procesador de cola en thread
    logger.info("Iniciando procesador de cola...")
    threading.Thread(target=correr_procesador, daemon=True).start()
    
    # Configurar SSL si está disponible
    ssl_context = None
    if CERT_FILE and KEY_FILE:
        try:
            logger.info("Cargando certificados SSL...")
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
            logger.info("✅ SSL/HTTPS activado")
        except Exception as e:
            logger.error(f"⚠️  Error cargando certificados SSL: {e}")
            logger.warning("   Continuando con HTTP...")
            ssl_context = None
    
    logger.info(f"Iniciando servidor en {protocolo}...")
    logger.info("=" * 50)
    
    # Iniciar servidor
    socketio.run(app, host=HOST, port=PUERTO, debug=False, 
                 allow_unsafe_werkzeug=True, ssl_context=ssl_context)