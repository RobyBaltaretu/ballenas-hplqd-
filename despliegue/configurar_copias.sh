#!/usr/bin/env bash
# Configura la copia diaria de los datos del bot a Cloudflare R2 y la prueba.
# Antes, en Cloudflare: R2 -> crea un cubo (p. ej. bot-ballenas-copias) y un token de API de R2 con permiso
# "Object Read & Write" SOLO para ese cubo. Te da: Access Key ID, Secret Access Key y tu Account ID.
# Uso:  sudo bash /opt/bot-ballenas/despliegue/configurar_copias.sh
set -euo pipefail
DESTINO="${DESTINO:-/opt/bot-ballenas}"
USUARIO="${USUARIO:-botballenas}"
CONF="$DESTINO/.copia.env"

read -r -p "Account ID de Cloudflare: " cuenta < /dev/tty
read -r -p "Nombre del cubo de R2 [bot-ballenas-copias]: " cubo < /dev/tty
cubo="${cubo:-bot-ballenas-copias}"
read -r -p "Access Key ID: " clave_id < /dev/tty
read -r -s -p "Secret Access Key (no se muestra): " clave < /dev/tty; echo
for v in "$cuenta" "$cubo" "$clave_id" "$clave"; do
  case "$v" in *[[:space:]\'\"\$\`]*|"") echo "Valor vacío o con caracteres no válidos."; exit 1;; esac
done
umask 077
cat > "$CONF" <<EOF
R2_CUENTA='$cuenta'
R2_CUBO='$cubo'
R2_CLAVE_ID='$clave_id'
R2_CLAVE_SECRETA='$clave'
EOF
chown "$USUARIO:$USUARIO" "$CONF"
chmod 600 "$CONF"

echo ">> Haciendo la primera copia (puede tardar si ya hay muchos datos)..."
if sudo -u "$USUARIO" bash "$DESTINO/despliegue/copia_seguridad.sh"; then
  systemctl enable --now bot-ballenas-copia.timer >/dev/null 2>&1 || true
  echo "Copias configuradas. Se harán cada día a las 03:30 UTC y se guardan 14 días de versiones."
else
  echo "La copia ha fallado: revisa el Account ID, el nombre del cubo y que el token tenga permiso de escritura."
  exit 1
fi
