#!/bin/bash

# ═══════════════════════════════════════════════════════════════════════════════
# TikTok Live Comment Reader - Backend Installation Script (Linux)
# Instala todas las dependencias de una sola vez
# 
# Uso:
#   chmod +x instalar_backend.sh
#   ./instalar_backend.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -e  # Salir si hay error

echo "╔═══════════════════════════════════════════════════════════════════════════╗"
echo "║  TikTok Live Reader - Backend Installation                               ║"
echo "║  Sistema: Linux                                                           ║"
echo "╚═══════════════════════════════════════════════════════════════════════════╝"
echo ""

# Verificar si está corriendo como root (recomendado para instalar paquetes del sistema)
if [ "$EUID" -ne 0 ]; then 
   echo "⚠️  Este script necesita permisos de sudo para instalar paquetes del sistema."
   echo "Ejecuta con: sudo ./instalar_backend.sh"
   echo ""
   read -p "¿Continuar sin sudo? (s/n): " -n 1 -r
   echo
   if [[ ! $REPLY =~ ^[Ss]$ ]]; then
      exit 1
   fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Paso 1: Actualizar índices de paquetes
# ─────────────────────────────────────────────────────────────────────────────

echo "📦 [1/5] Actualizando índices de paquetes..."
apt-get update -qq 2>/dev/null || echo "⚠️  apt-get update falló (puedes ignorar si usas otro gestor)"
echo "✓ Hecho"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Paso 2: Instalar dependencias del sistema
# ─────────────────────────────────────────────────────────────────────────────

echo "📦 [2/5] Instalando dependencias del sistema..."
echo "   - python3-pip"
echo "   - python3-dev"
echo "   - ffmpeg"
echo ""

PAQUETES="python3-pip python3-dev ffmpeg"

for paquete in $PAQUETES; do
    if dpkg -l | grep -q "^ii  $paquete"; then
        echo "   ✓ $paquete ya instalado"
    else
        echo "   → Instalando $paquete..."
        apt-get install -y "$paquete" -qq 2>/dev/null || echo "   ⚠️  Error instalando $paquete"
    fi
done
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Paso 3: Actualizar pip
# ─────────────────────────────────────────────────────────────────────────────

echo "📦 [3/5] Actualizando pip, setuptools y wheel..."
python3 -m pip install --upgrade pip setuptools wheel -q 2>/dev/null || echo "⚠️  Error actualizando pip"
echo "✓ Hecho"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Paso 4: Instalar dependencias Python
# ─────────────────────────────────────────────────────────────────────────────

echo "📦 [4/5] Instalando librerías Python..."
echo "   - TikTokLive"
echo "   - edge-tts"
echo "   - flask"
echo "   - flask-cors"
echo "   - flask-socketio"
echo "   - python-socketio"
echo "   - aiohttp"
echo ""

PAQUETES_PYTHON=(
    "TikTokLive"
    "edge-tts"
    "flask"
    "flask-cors"
    "flask-socketio"
    "python-socketio"
    "aiohttp"
)

for paquete in "${PAQUETES_PYTHON[@]}"; do
    echo "   → Instalando $paquete..."
    python3 -m pip install "$paquete" -q 2>/dev/null || echo "   ⚠️  Error instalando $paquete"
done
echo "✓ Hecho"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Paso 5: Verificar instalación
# ─────────────────────────────────────────────────────────────────────────────

echo "📦 [5/5] Verificando instalación..."
echo ""

python3 << 'PYTHON_CHECK'
import sys

librerías = ['TikTokLive', 'edge_tts', 'flask', 'flask_cors', 'flask_socketio', 'socketio', 'aiohttp']
fallos = []

for lib in librerías:
    try:
        __import__(lib)
        print(f"   ✓ {lib}")
    except ImportError:
        print(f"   ✗ {lib} - FALLO")
        falsos.append(lib)

if fallos:
    print(f"\n⚠️  Algunas librerías fallaron: {', '.join(fallos)}")
    print("Intenta instalarlas manualmente:")
    for lib in fallos:
        print(f"   python3 -m pip install {lib}")
else:
    print("\n✅ Todas las dependencias instaladas correctamente")
PYTHON_CHECK

echo ""
echo "╔═══════════════════════════════════════════════════════════════════════════╗"
echo "║  ✅ Instalación completada                                               ║"
echo "╚═══════════════════════════════════════════════════════════════════════════╝"
echo ""
echo "Próximos pasos:"
echo ""
echo "1. Copia el archivo 'backend_servidor.py' a esta carpeta"
echo ""
echo "2. Ejecuta el servidor:"
echo "   python3 backend_servidor.py"
echo ""
echo "3. Deberías ver:"
echo "   ╔════════════════════════════════════════════╗"
echo "   ║  TikTok Live Reader - Backend             ║"
echo "   ║  Corre en: http://0.0.0.0:5000            ║"
echo "   ╚════════════════════════════════════════════╝"
echo ""
