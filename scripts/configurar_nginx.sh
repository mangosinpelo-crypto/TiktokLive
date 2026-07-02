
set -e

PUERTO_FLASK=5080 
PUERTO_NGINX=8443      
IP=$(hostname -I | awk '{print $1}')

echo "╔══════════════════════════════════════════════════╗"
echo "║  Configurando Nginx - TikTok Live Reader        ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ─── 1. Instalar Nginx ────────────────────────────────
echo "[1/5] Instalando Nginx..."
apt-get update -qq
apt-get install -y nginx openssl -qq
echo "✅ Nginx instalado"
echo ""

# ─── 2. Generar certificado auto-firmado ─────────────
echo "[2/5] Generando certificado SSL auto-firmado..."
mkdir -p /etc/nginx/ssl
openssl req -x509 -newkey rsa:4096 -nodes \
  -out /etc/nginx/ssl/servidor.crt \
  -keyout /etc/nginx/ssl/servidor.key \
  -days 3650 \
  -subj "/C=US/ST=State/L=City/O=TikTokLive/CN=${IP}" \
  2>/dev/null
echo "✅ Certificado generado (válido 10 años)"
echo ""

# ─── 3. Configurar Nginx ──────────────────────────────
echo "[3/5] Configurando Nginx..."
cat > /etc/nginx/sites-available/tiktok-live << NGINX
server {
    listen ${PUERTO_NGINX} ssl;
    server_name _;

    ssl_certificate     /etc/nginx/ssl/servidor.crt;
    ssl_certificate_key /etc/nginx/ssl/servidor.key;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Aumentar timeout para WebSocket
    proxy_read_timeout 3600;
    proxy_send_timeout 3600;

    # Proxy HTTP normal
    location /api/ {
        proxy_pass http://127.0.0.1:${PUERTO_FLASK};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }

    # Proxy WebSocket (Socket.IO)
    location /socket.io/ {
        proxy_pass http://127.0.0.1:${PUERTO_FLASK};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_cache_bypass \$http_upgrade;
    }

    # Servir app.html directamente
    location / {
        root /var/www/tiktok-live;
        index app.html;
        try_files \$uri \$uri/ /app.html;
    }
}
NGINX

# Activar el sitio
ln -sf /etc/nginx/sites-available/tiktok-live /etc/nginx/sites-enabled/tiktok-live

# Desactivar el sitio default si existe
rm -f /etc/nginx/sites-enabled/default

echo "✅ Nginx configurado"
echo ""

# ─── 4. Crear carpeta para la web app ────────────────
echo "[4/5] Creando carpeta para la web app..."
mkdir -p /var/www/tiktok-live
echo "✅ Carpeta creada en /var/www/tiktok-live"
echo ""

# ─── 5. Abrir puerto y reiniciar Nginx ───────────────
echo "[5/5] Abriendo puerto ${PUERTO_NGINX} y reiniciando Nginx..."
ufw allow ${PUERTO_NGINX}/tcp 2>/dev/null || true
nginx -t && systemctl restart nginx && systemctl enable nginx
echo "✅ Nginx activo"
echo ""

# ─── Ahora actualizar el backend para HTTP puro ──────
echo "────────────────────────────────────────────────"
echo "⚠️  IMPORTANTE: El backend Flask debe correr en"
echo "   HTTP puro (sin SSL) en el puerto ${PUERTO_FLASK}."
echo ""
echo "   Abre backend_servidor.py y asegúrate que"
echo "   estas líneas estén así:"
echo ""
echo "   PUERTO = ${PUERTO_FLASK}"
echo "   CERT_FILE = None"
echo "   KEY_FILE = None"
echo "────────────────────────────────────────────────"
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  ✅ Configuración completada                    ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  URL de la app:    https://${IP}:${PUERTO_NGINX}"
echo "  URL del backend:  https://${IP}:${PUERTO_NGINX}/api/status"
echo ""
echo "  Pasos finales:"
echo "  1. Edita backend_servidor.py → CERT_FILE = None, KEY_FILE = None"
echo "  2. Copia app.html a /var/www/tiktok-live/"
echo "  3. Corre: python3 backend_servidor.py"
echo "  4. Abre en el navegador: https://${IP}:${PUERTO_NGINX}"
echo "  5. Acepta el certificado una sola vez → listo para siempre"
echo ""
