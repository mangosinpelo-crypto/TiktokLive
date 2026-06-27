"""
TikTok Live Comment Reader - Backend para app móvil
Corre en servidor Linux 24/7

Instalación:
    pip install TikTokLive edge-tts flask flask-cors flask-socketio python-socketio aiohttp

Uso:
    python backend_servidor.py
"""

import asyncio
import json
import re
import tempfile
import os
from collections import deque
from TikTokLive import 
from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent
import edge_tts
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import threading
import base64

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

# ─── Flask + SocketIO ─────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

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
    return audio_bytes

async def procesador_cola():
    """Procesa la cola de comentarios."""
    while True:
        try:
            if cola_comentarios:
                usuario, texto = cola_comentarios.popleft()
                frase = f"{usuario} dice: {texto}"
                
                print(f"🔊 {frase}")
                
                # Generar audio
                audio_bytes = await texto_a_voz(frase)
                audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
                
                # Enviar a todos los clientes conectados
                socketio.emit('reproducir_audio', {
                    'usuario': usuario,
                    'texto': texto,
                    'audio': audio_base64,
                    'timestamp': int(asyncio.get_event_loop().time() * 1000)
                }, broadcast=True)
            else:
                await asyncio.sleep(0.2)
        except Exception as e:
            print(f"⚠️  Error procesando cola: {e}")
            await asyncio.sleep(1)

def crear_cliente_tiktok(usuario: str):
    """Crea cliente TikTok para un usuario específico."""
    global cliente_tiktok, live_activo, usuario_actual
    
    client = TikTokLiveClient(unique_id=usuario)
    usuario_actual = usuario

    @client.on(ConnectEvent)
    async def al_conectar(event: ConnectEvent):
        global live_activo
        live_activo = True
        print(f"✅ Conectado al live de @{usuario}")
        socketio.emit('live_conectado', {
            'usuario': usuario,
            'mensaje': f'Conectado al live de @{usuario}'
        }, broadcast=True)

    @client.on(DisconnectEvent)
    async def al_desconectar(event: DisconnectEvent):
        global live_activo
        live_activo = False
        print(f"❌ Live de @{usuario} terminado")
        socketio.emit('live_desconectado', {
            'usuario': usuario,
            'mensaje': 'Live terminado o conexión perdida'
        }, broadcast=True)

    @client.on(CommentEvent)
    async def al_comentar(event: CommentEvent):
        usuario_comentario = event.user.nickname or event.user.unique_id
        texto = event.comment

        if esta_en_lista_negra(usuario_comentario, texto):
            return

        texto_limpio = limpiar_texto(texto)
        if not texto_limpio:
            return

        if len(texto_limpio) > MAX_CHARS:
            texto_limpio = texto_limpio[:MAX_CHARS] + "..."

        print(f"💬 {usuario_comentario}: {texto}")
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
    
    data = request.json
    usuario = data.get('usuario', '').strip()
    
    if not usuario:
        return jsonify({'error': 'Usuario requerido'}), 400
    
    if not usuario.startswith('@'):
        usuario = '@' + usuario
    
    try:
        if cliente_tiktok:
            return jsonify({'error': 'Ya hay un live activo'}), 400
        
        cliente_tiktok = crear_cliente_tiktok(usuario)
        
        # Iniciar en thread separado
        threading.Thread(
            target=lambda: asyncio.run(cliente_tiktok.start()),
            daemon=True
        ).start()
        
        return jsonify({
            'exito': True,
            'mensaje': f'Conectando a @{usuario}...'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/detener', methods=['POST'])
def detener_live():
    """Detiene la conexión actual."""
    global cliente_tiktok, live_activo
    
    if cliente_tiktok:
        try:
            cliente_tiktok.disconnect()
        except:
            pass
        cliente_tiktok = None
        live_activo = False
    
    return jsonify({'exito': True, 'mensaje': 'Live detenido'})

@app.route('/api/comentarios', methods=['GET'])
def obtener_comentarios():
    """Devuelve los últimos comentarios."""
    return jsonify({
        'comentarios': list(cola_comentarios)
    })

# ─── WebSocket Events ─────────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    print(f"📱 Cliente conectado: {request.sid}")
    clientes_conectados.add(request.sid)
    
    # Enviar estado actual
    emit('estado', {
        'live_activo': live_activo,
        'usuario': usuario_actual,
        'comentarios_en_cola': len(cola_comentarios)
    })

@socketio.on('disconnect')
def handle_disconnect():
    print(f"📱 Cliente desconectado: {request.sid}")
    clientes_conectados.discard(request.sid)

@socketio.on('ping')
def handle_ping():
    """Keep-alive desde el cliente."""
    emit('pong')

# ─── Main ─────────────────────────────────────────────────────────────────────

def correr_procesador():
    """Corre el procesador de cola en un event loop separado."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(procesador_cola())

if __name__ == '__main__':
    print("""
    ╔════════════════════════════════════════════╗
    ║  TikTok Live Reader - Backend             ║
    ║  Corre en: http://0.0.0.0:{}              ║
    ╚════════════════════════════════════════════╝
    """.format(PUERTO))
    
    # Iniciar procesador de cola en thread
    threading.Thread(target=correr_procesador, daemon=True).start()
    
    # Iniciar servidor
    socketio.run(app, host=HOST, port=PUERTO, debug=False, allow_unsafe_werkzeug=True)
