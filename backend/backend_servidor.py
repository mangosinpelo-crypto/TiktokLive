
import asyncio
import re
import tempfile
import os
from collections import deque
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent
import edge_tts
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import threading
import base64
import logging
import queue
import time

# ─── Logging ───────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-8s: %(message)s',
    datefmt='%d/%m/%Y %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('backend.log')
    ]
)
logger = logging.getLogger(__name__)

# ─── Voces disponibles ──────
VOCES_DISPONIBLES = [
    {"id": "es-MX-DaliaNeural",    "nombre": "Dalia",    "pais": "México 🇲🇽",    "genero": "F"},
    {"id": "es-MX-JorgeNeural",    "nombre": "Jorge",    "pais": "México 🇲🇽",    "genero": "M"},
    {"id": "es-ES-ElviraNeural",   "nombre": "Elvira",   "pais": "España 🇪🇸",    "genero": "F"},
    {"id": "es-ES-AlvaroNeural",   "nombre": "Álvaro",   "pais": "España 🇪🇸",    "genero": "M"},
    {"id": "es-AR-ElenaNeural",    "nombre": "Elena",    "pais": "Argentina 🇦🇷",  "genero": "F"},
    {"id": "es-AR-TomasNeural",    "nombre": "Tomás",    "pais": "Argentina 🇦🇷",  "genero": "M"},
    {"id": "es-CO-SalomeNeural",   "nombre": "Salomé",   "pais": "Colombia 🇨🇴",   "genero": "F"},
    {"id": "es-CO-GonzaloNeural",  "nombre": "Gonzalo",  "pais": "Colombia 🇨🇴",   "genero": "M"},
    {"id": "es-US-PalomaNeural",   "nombre": "Paloma",   "pais": "US Latino 🇺🇸",  "genero": "F"},
    {"id": "es-US-AlonsoNeural",   "nombre": "Alonso",   "pais": "US Latino 🇺🇸",  "genero": "M"},
    {"id": "es-CL-CatalinaNeural", "nombre": "Catalina", "pais": "Chile 🇨🇱",      "genero": "F"},
    {"id": "es-VE-PaolaNeural",    "nombre": "Paola",    "pais": "Venezuela 🇻🇪",  "genero": "F"},
]

# ─── Configuración ──────
VOZ        = "es-MX-DaliaNeural"
VELOCIDAD  = "+30%"
VOLUMEN    = "+0%"
MAX_COLA   = 50
MAX_CHARS  = 150
PUERTO     = 5080
HOST       = "0.0.0.0"

LISTA_NEGRA_USUARIOS = set() 
LISTA_NEGRA_PALABRAS = set()

# ─── Flask + SocketIO ──────
app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', logger=False, engineio_logger=False)

app_stream = Flask(__name__ + '_stream')
CORS(app_stream)
PUERTO_STREAM = 5081

# ─── Estado global ──────
cola_comentarios    = deque(maxlen=MAX_COLA)
cola_lock           = threading.Lock()

frontend_libre      = threading.Event()
frontend_libre.set()

streams_activos     = set()

cliente_tiktok      = None
tiktok_thread       = None
live_activo         = False
detener_flag        = False
usuario_actual      = None
clientes_conectados = set()
tiempo_conexion     = 0.0

# ─── Utilidades ───────

def limpiar_texto(texto: str) -> str:
    texto = re.sub(r'http\S+', '', texto)
    texto = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', texto)  # control chars
    return ' '.join(texto.split()).strip()

def en_lista_negra(usuario: str, texto: str) -> bool:
    if usuario.lower() in {u.lower() for u in LISTA_NEGRA_USUARIOS}:
        return True
    return any(p in texto.lower() for p in LISTA_NEGRA_PALABRAS)

def texto_a_voz_sync(texto: str) -> bytes:
    async def _generar():
        communicate = edge_tts.Communicate(texto, voice=VOZ, rate=VELOCIDAD, volume=VOLUMEN)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        await communicate.save(tmp.name)
        with open(tmp.name, 'rb') as f:
            audio_bytes = f.read()
        os.unlink(tmp.name)
        return audio_bytes

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_generar())
    finally:
        loop.close()

# ─── Procesador de cola ───────────────────────────────────────────────────────

def procesador_cola():

    logger.info("✅ Procesador de cola iniciado")

    while True:
        while True:
            with cola_lock:
                if cola_comentarios:
                    usuario, texto = cola_comentarios.popleft()
                    break
            threading.Event().wait(0.2)

        if detener_flag:
            continue

        frase = f"{usuario} dice: {texto}"
        logger.info(f"🔊 Generando voz ({VOZ}): {frase}")

        try:
            audio_bytes  = texto_a_voz_sync(frase)
            audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"Error generando voz: {e}", exc_info=True)
            continue

        if detener_flag:
            continue

        logger.info("⏳ Esperando que el frontend termine de reproducir...")
        
        while not frontend_libre.is_set():
            if detener_flag:
                break
            frontend_libre.wait(0.5)
            
        if detener_flag:
            continue

        frontend_libre.clear()
        logger.info(f"📤 Enviando audio ({len(audio_bytes)} bytes) a {len(clientes_conectados)} clientes")
        socketio.emit('reproducir_audio', {
            'usuario': usuario,
            'texto':   texto,
            'audio':   audio_base64,
        })

# ─── Cliente TikTok ──────

def crear_cliente_tiktok(unique_id: str):
    global live_activo, usuario_actual

    unique_id_con_arroba = '@' + unique_id.lstrip('@')
    logger.info(f"Creando cliente TikTok para: '{unique_id_con_arroba}'")

    client = TikTokLiveClient(unique_id=unique_id_con_arroba)
    usuario_actual = unique_id_con_arroba

    @client.on(ConnectEvent)
    async def al_conectar(event: ConnectEvent):
        global live_activo, tiempo_conexion
        live_activo = True
        tiempo_conexion = time.time()
        logger.info(f"✅ ConnectEvent disparado para {unique_id_con_arroba}")
        socketio.emit('live_conectado', {
            'usuario': unique_id_con_arroba,
            'mensaje': f'Conectado al live de {unique_id_con_arroba}'
        })

    @client.on(DisconnectEvent)
    async def al_desconectar(event: DisconnectEvent):
        global live_activo
        live_activo = False
        logger.info(f"❌ DisconnectEvent para {unique_id_con_arroba}")
        socketio.emit('live_desconectado', {
            'usuario': unique_id_con_arroba,
            'mensaje': 'Live terminado o conexión perdida'
        })

    @client.on(CommentEvent)
    async def al_comentar(event: CommentEvent):
        try:
            if detener_flag:
                return

            user_info = event.user_info
            usuario_comentario = (
                getattr(user_info, 'nick_name', None)
                or getattr(user_info, 'nickname', None)
                or getattr(user_info, 'unique_id', None)
                or getattr(user_info, 'username', None)
                or "Anónimo"
            )
            texto = event.comment or ""

            logger.info(f"💬 {usuario_comentario}: {texto}")

            if en_lista_negra(usuario_comentario, texto):
                logger.debug(f"Ignorado (lista negra): {usuario_comentario}")
                return

            texto_limpio = limpiar_texto(texto)
            if not texto_limpio:
                logger.debug(f"Ignorado (vacío): {usuario_comentario}")
                return

            if len(texto_limpio) > MAX_CHARS:
                texto_limpio = texto_limpio[:MAX_CHARS] + "..."

            with cola_lock:
                cola_comentarios.append((usuario_comentario, texto_limpio))
            
                # Si entraron de golpe al conectar (historial), solo conservar los 2 últimos
                if time.time() - tiempo_conexion < 2.0:
                    while len(cola_comentarios) > 2:
                        cola_comentarios.popleft()

            socketio.emit('nuevo_comentario', {
                'usuario': usuario_comentario,
                'texto':   texto_limpio,
            })

        except Exception as e:
            logger.error(f"Error procesando comentario: {e}", exc_info=True)

    return client

# ─── Rutas HTTP ─────

@app_stream.route('/api/stream', methods=['GET'])
def audio_stream():
    silencio_b64 = "SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tAwAAAAAAAAAAAAAAAAAAAAAAASW5mbwAAAA8AAAAoAAAQ9gAQEBYWHR0dIyMpKSkvLzU1NTs7QUFBSEhOTk5UVFpaWmBgZmZmbGxycnJ5eX9/f4WFi4uLkZGXl5ednaSkpKqqsLCwtra8vLzCwsjIyM7O1dXV29vh4eHn5+3t7fPz+fn5//8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAV8AAAAAAAAEPYp+BPjAAAAAAD/+xDEAAPAAAGkAAAAIAAANIAAAARMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMQpg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVV//sQxFMDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVX/+xDEfIPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMSmA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVV//sQxM+DwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVQ=="
    silencio_bytes = base64.b64decode(silencio_b64)

    def generate_audio():
        q = queue.Queue(maxsize=15)
        streams_activos.add(q)
        logger.info(f"🎧 Nuevo cliente HTTP stream (Total: {len(streams_activos)})")

        try:
            while True:
                try:
                    audio_bytes = q.get(timeout=1.0)
                    yield audio_bytes
                except queue.Empty:
                    yield silencio_bytes
        except GeneratorExit:
            logger.info("❌ Cliente HTTP stream desconectado")
        finally:
            streams_activos.discard(q)

    resp = Response(generate_audio(), mimetype="audio/mpeg")
    resp.headers['Cache-Control'] = 'no-cache, no-store'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Transfer-Encoding'] = 'chunked'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/api/status', methods=['GET'])
def status():
    return jsonify({
        'live_activo':         live_activo,
        'usuario':             usuario_actual,
        'comentarios_en_cola': len(cola_comentarios),
        'clientes_conectados': len(clientes_conectados),
        'voz_actual':          VOZ,
    })

@app.route('/api/voces', methods=['GET'])
def get_voces():
    return jsonify({'voces': VOCES_DISPONIBLES, 'voz_actual': VOZ})

@app.route('/api/config', methods=['POST'])
def set_config():
    global VOZ, VELOCIDAD
    data = request.json or {}

    if 'voz' in data:
        voz_ids = {v['id'] for v in VOCES_DISPONIBLES}
        if data['voz'] not in voz_ids:
            return jsonify({'error': 'Voz no válida'}), 400
        VOZ = data['voz']
        logger.info(f"🎙️ Voz cambiada a: {VOZ}")

    if 'velocidad' in data:
        VELOCIDAD = data['velocidad']
        logger.info(f"⚡ Velocidad cambiada a: {VELOCIDAD}")

    return jsonify({'exito': True, 'voz': VOZ, 'velocidad': VELOCIDAD})

@app.route('/api/silenciar', methods=['POST'])
def silenciar_usuario():
    data = request.json or {}
    usuario = data.get('usuario', '').strip().lower()
    if not usuario:
        return jsonify({'error': 'Usuario requerido'}), 400
    LISTA_NEGRA_USUARIOS.add(usuario)
    logger.info(f"🔇 Usuario silenciado: {usuario}")
    return jsonify({'exito': True, 'silenciados': list(LISTA_NEGRA_USUARIOS)})

@app.route('/api/silenciar', methods=['DELETE'])
def quitar_silencio():
    data = request.json or {}
    usuario = data.get('usuario', '').strip().lower()
    LISTA_NEGRA_USUARIOS.discard(usuario)
    logger.info(f"🔊 Usuario des-silenciado: {usuario}")
    return jsonify({'exito': True, 'silenciados': list(LISTA_NEGRA_USUARIOS)})

@app.route('/api/silenciados', methods=['GET'])
def get_silenciados():
    return jsonify({'silenciados': list(LISTA_NEGRA_USUARIOS)})

@app.route('/api/iniciar', methods=['POST'])
def iniciar_live():
    global cliente_tiktok, tiktok_thread, detener_flag

    logger.info(f"POST /api/iniciar desde {request.remote_addr}")

    try:
        data    = request.json or {}
        usuario = data.get('usuario', '').strip().lstrip('@')

        logger.info(f"Usuario: '{usuario}'")

        if not usuario:
            return jsonify({'error': 'Usuario requerido'}), 400

        if cliente_tiktok:
            return jsonify({'error': 'Ya hay un live activo, detén el actual primero'}), 400

        detener_flag = False

        cliente_tiktok = crear_cliente_tiktok(usuario)

        def iniciar_tiktok():
            global cliente_tiktok, live_activo
            try:
                logger.info(f"Iniciando TikTokLive con client.run()...")
                cliente_tiktok.run(process_connect_events=False)
            except Exception as e:
                logger.error(f"Error en cliente TikTok: {e}", exc_info=True)
                err_str = str(e)
                if 'UserNotFoundError' in err_str:
                    msg = f'Usuario no encontrado o no está en vivo: @{usuario}'
                elif 'LiveNotFoundError' in err_str or 'LiveEndedError' in err_str:
                    msg = f'@{usuario} no está en vivo ahora'
                else:
                    msg = f'Error de conexión: {err_str[:120]}'
                socketio.emit('live_desconectado', {'usuario': usuario, 'mensaje': msg})
            finally:
                cliente_tiktok = None
                live_activo = False
                frontend_libre.set()
                logger.info("Estado reiniciado, listo para nuevo intento")

        tiktok_thread = threading.Thread(target=iniciar_tiktok, daemon=True)
        tiktok_thread.start()

        return jsonify({'exito': True, 'mensaje': f'Conectando a @{usuario}...'})

    except Exception as e:
        logger.error(f"Error en /api/iniciar: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/detener', methods=['POST'])
def detener_live():
    global cliente_tiktok, live_activo, detener_flag

    logger.info(f"POST /api/detener desde {request.remote_addr}")

    detener_flag = True

    with cola_lock:
        cola_comentarios.clear()

    if cliente_tiktok:
        try:
            loop = getattr(cliente_tiktok, '_asyncio_loop', None)
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(cliente_tiktok.disconnect(), loop)
        except Exception as e:
            logger.error(f"Error al detener cliente: {e}")

    cliente_tiktok = None
    live_activo = False
    frontend_libre.set()

    logger.info("✅ Live detenido correctamente")
    socketio.emit('live_desconectado')
    return jsonify({'exito': True, 'mensaje': 'Live detenido'})

# ─── WebSocket ─────

@socketio.on('connect')
def handle_connect():
    logger.info(f"📱 CLIENTE CONECTADO: {request.sid} desde {request.remote_addr}")
    clientes_conectados.add(request.sid)
    emit('estado', {
        'live_activo': live_activo,
        'usuario':     usuario_actual,
    })

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"📱 CLIENTE DESCONECTADO: {request.sid}")
    clientes_conectados.discard(request.sid)
    if not clientes_conectados:
        frontend_libre.set()

@socketio.on('audio_terminado')
def handle_audio_terminado():
    logger.info(f"✅ audio_terminado recibido de {request.sid}")
    frontend_libre.set()

# ─── Main ──────

if __name__ == '__main__':
    logger.info("""
╔════════════════════════════════════════════╗
║  TikTok Live Reader - Backend             ║
║  Puerto: 5080  (SocketIO)                 ║
║  Puerto: 5081  (Audio Stream)             ║
╚════════════════════════════════════════════╝
    """)

    procesador_thread = threading.Thread(target=procesador_cola, daemon=True)
    procesador_thread.start()

    def run_stream_server():
        from werkzeug.serving import make_server
        srv = make_server(HOST, PUERTO_STREAM, app_stream, threaded=True)
        logger.info(f"🎧 Servidor de audio stream en puerto {PUERTO_STREAM}")
        srv.serve_forever()

    stream_thread = threading.Thread(target=run_stream_server, daemon=True)
    stream_thread.start()

    socketio.run(app, host=HOST, port=PUERTO, debug=False, allow_unsafe_werkzeug=True)