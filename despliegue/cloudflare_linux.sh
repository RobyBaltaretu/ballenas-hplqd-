#!/usr/bin/env bash
# Conecta este servidor a tu Cloudflare Tunnel para ver el panel en tu dominio con inicio de sesión (Cloudflare Access).
# Antes, en Cloudflare Zero Trust -> Networks -> Tunnels: abre tu túnel (o crea uno), elige "Debian" y la arquitectura
# del servidor (arm64 en Oracle ARM) y copia el comando o el token (texto largo que empieza por eyJ).
# Uso:  sudo bash /opt/bot-ballenas/despliegue/cloudflare_linux.sh
set -euo pipefail
if ! command -v cloudflared >/dev/null 2>&1; then
  case "$(uname -m)" in
    aarch64|arm64) arq=arm64 ;;
    x86_64) arq=amd64 ;;
    *) echo "Arquitectura no soportada: $(uname -m)"; exit 1 ;;
  esac
  echo ">> Instalando cloudflared ($arq) desde la página oficial de Cloudflare..."
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/cloudflared.deb" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arq.deb"
  dpkg -i "$tmp/cloudflared.deb" >/dev/null
  rm -rf "$tmp"
fi
echo "cloudflared: $(cloudflared --version 2>/dev/null | head -1)"
read -r -s -p "Pega el comando o el token del túnel (no se muestra): " entrada < /dev/tty; echo
token="$(printf '%s' "$entrada" | tr ' ' '\n' | grep -E '^eyJ' | head -1 || true)"
[ -n "$token" ] || { echo "No encuentro el token (empieza por eyJ)."; exit 1; }
cloudflared service uninstall >/dev/null 2>&1 || true
cloudflared service install "$token"
sleep 5
if systemctl is-active --quiet cloudflared; then
  echo "Túnel conectado. En Cloudflare debería salir 'Healthy'."
  echo "La ruta pública debe apuntar a  http://localhost:8080  (el panel solo escucha dentro del servidor)."
  echo "Si el túnel lo usaba antes tu PC: desconéctalo allí (herramientas\\desconectar-cloudflare.bat)."
else
  echo "El servicio cloudflared no está activo. Mira: journalctl -u cloudflared -n 50"
fi
