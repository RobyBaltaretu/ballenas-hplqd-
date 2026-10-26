#!/usr/bin/env bash
# Actualiza SOLO el código del bot en el servidor. Conserva .env, config.json y todos los datos
# (estado, cuenta simulada, memoria, registro, datos de mercado y archivo).
# Uso (en el servidor, dentro de la carpeta con la versión NUEVA):   sudo bash despliegue/actualizar_vps.sh
set -euo pipefail
ORIGEN="$(cd "$(dirname "$0")/.." && pwd)"
DESTINO="${DESTINO:-/opt/bot-ballenas}"
USUARIO="${USUARIO:-botballenas}"
PRUEBA="${PRUEBA:-0}"
sistema() { if [ "$PRUEBA" = "1" ]; then echo "   (prueba) $*"; else "$@"; fi; }

[ -f "$DESTINO/bot.py" ] || { echo "No hay bot instalado en $DESTINO: usa despliegue/instalar_vps.sh"; exit 1; }
if [ "$PRUEBA" != "1" ] && [ "$(id -u)" -ne 0 ]; then echo "Ejecútalo con sudo."; exit 1; fi

echo ">> Parando el bot (apagado ordenado: conserva los stops)..."
sistema systemctl stop bot-ballenas bot-ballenas-panel || true

echo ">> Copiando solo el código..."
rsync -a --prune-empty-dirs \
  --exclude '.venv/' --exclude '__pycache__/' --exclude 'datos_*/' --exclude 'registro/' --exclude 'logs/' \
  --include '*/' --include '*.py' --include '*.html' --include '*.md' --include '*.txt' --include '*.sh' \
  --include '*.service' --include '*.timer' --include '*.bat' --include '*.ps1' --include '.env.example' \
  --exclude '*' "$ORIGEN"/ "$DESTINO"/

echo ">> Dependencias..."
if [ "$PRUEBA" != "1" ]; then "$DESTINO/.venv/bin/pip" install -q -r "$DESTINO/requirements.txt"; fi
if [ "$PRUEBA" != "1" ]; then chown -R "$USUARIO:$USUARIO" "$DESTINO"; fi

echo ">> Comprobando (y si hace falta migrando, con copia de seguridad) la configuración..."
if [ "$PRUEBA" = "1" ]; then
  (cd "$DESTINO" && .venv/bin/python bot.py --validar-config)
else
  (cd "$DESTINO" && sudo -u "$USUARIO" .venv/bin/python bot.py --validar-config) || {
    echo "La configuración no es válida. El bot NO se ha arrancado. Corrige config.json y ejecuta:"
    echo "  sudo systemctl start bot-ballenas bot-ballenas-panel"; exit 1; }
fi

if [ "$PRUEBA" != "1" ]; then
  for u in "$DESTINO"/despliegue/bot-ballenas*.service "$DESTINO"/despliegue/bot-ballenas*.timer; do
    cp "$u" /etc/systemd/system/
  done
  systemctl daemon-reload
  systemctl enable bot-ballenas bot-ballenas-panel >/dev/null
  systemctl enable --now bot-ballenas-investigacion.timer bot-ballenas-laboratorio.timer bot-ballenas-recordatorio.timer bot-ballenas-copia.timer >/dev/null
fi
sistema systemctl start bot-ballenas bot-ballenas-panel
echo "Actualizado y en marcha. Ver lo que hace: journalctl -u bot-ballenas -f"
if [ "$PRUEBA" != "1" ]; then sleep 20; (cd "$DESTINO" && sudo -u "$USUARIO" .venv/bin/python comprobar.py) || true; fi
