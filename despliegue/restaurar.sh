#!/usr/bin/env bash
# Restaura los DATOS del bot desde la última copia en Cloudflare R2 (por ejemplo, en una máquina nueva).
# Requisitos: bot instalado (instalar_vps.sh) y copias configuradas (configurar_copias.sh) con los mismos datos de R2.
# Uso:  sudo bash /opt/bot-ballenas/despliegue/restaurar.sh
set -euo pipefail
DESTINO="${DESTINO:-/opt/bot-ballenas}"
USUARIO="${USUARIO:-botballenas}"
CONF="$DESTINO/.copia.env"
[ -f "$CONF" ] || { echo "Primero: sudo bash $DESTINO/despliegue/configurar_copias.sh"; exit 1; }
# shellcheck disable=SC1090
. "$CONF"
export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_CLAVE_ID" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_CLAVE_SECRETA"
export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_CUENTA}.r2.cloudflarestorage.com"
ORIGEN="r2:${R2_CUBO}/bot-ballenas/actual"

echo "Contenido de la última copia:"
rclone size "$ORIGEN" || { echo "No encuentro la copia en R2."; exit 1; }
read -r -p "Esto SUSTITUYE los datos actuales del bot por los de la copia. ¿Seguir? escribe RESTAURAR: " r < /dev/tty
[ "$r" = "RESTAURAR" ] || { echo "Cancelado."; exit 1; }

systemctl stop bot-ballenas bot-ballenas-panel || true
APARTE="$DESTINO/datos_antes_de_restaurar_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$APARTE"
for f in estado.json papel.json aprendizaje.json promociones.json config.json; do
  [ -f "$DESTINO/$f" ] && cp -a "$DESTINO/$f" "$APARTE/" || true
done
rclone copy "$ORIGEN" "$DESTINO" --transfers 4 --retries 3 -q
chown -R "$USUARIO:$USUARIO" "$DESTINO"
echo "Restaurado. Lo que había antes se ha guardado en $APARTE."
echo "Arranca el bot:  sudo systemctl start bot-ballenas bot-ballenas-panel"
