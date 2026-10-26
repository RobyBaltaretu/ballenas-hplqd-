#!/usr/bin/env bash
# Copia de seguridad de los DATOS del bot a Cloudflare R2 (incremental). Nunca copia .env ni el código.
#   - r2:<cubo>/bot-ballenas/actual          lo último (lo que se restaura)
#   - r2:<cubo>/bot-ballenas/anteriores/...  versiones anteriores de lo que cambia o se borra (se guardan 14 días)
# La lanza cada día el temporizador bot-ballenas-copia.timer. A mano:  sudo -u botballenas bash despliegue/copia_seguridad.sh
set -euo pipefail
DESTINO="${DESTINO:-/opt/bot-ballenas}"
CONF="$DESTINO/.copia.env"
PY="$DESTINO/.venv/bin/python"

if [ ! -f "$CONF" ]; then
  echo "Copias no configuradas (falta $CONF). Ejecuta: sudo bash $DESTINO/despliegue/configurar_copias.sh"
  exit 0
fi
# shellcheck disable=SC1090
. "$CONF"
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_CLAVE_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_CLAVE_SECRETA"
export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_CUENTA}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
RAIZ="${RAIZ_PRUEBA:-r2:${R2_CUBO}/bot-ballenas}"   # RAIZ_PRUEBA: solo para las pruebas
FECHA="$(date -u +%Y-%m-%dT%H%M%SZ)"

FILTRO="$(mktemp)"
trap 'rm -f "$FILTRO"' EXIT
cat > "$FILTRO" <<'EOF'
- .env
- .copia.env
- .venv/**
- __pycache__/**
- datos_backtest/**
- *.tmp
- *.lock
- *.lock.info
- panel_latido.json
+ config.json
+ config.json.*.bak
+ estado*.json
+ papel.json
+ aprendizaje*.json
+ promociones.json
+ estrategia_elegida.json
+ laboratorio_holdout.json
+ laboratorio_variantes.json
+ config_copias/**
+ laboratorio_validacion.csv
+ ballenas.json
+ investigacion.json
+ incidentes.json
+ registro/**
+ datos_mercado/**
+ datos_archivo/**
- **
EOF

if rclone sync "$DESTINO" "$RAIZ/actual" --filter-from "$FILTRO" --backup-dir "$RAIZ/anteriores/$FECHA" \
     --fast-list --transfers 4 --retries 3 --low-level-retries 10 --stats-one-line -q; then
  rclone delete "$RAIZ/anteriores" --min-age 14d -q 2>/dev/null || true      # aún no existe la primera vez
  rclone rmdirs "$RAIZ/anteriores" --leave-root -q 2>/dev/null || true
  date -u +%Y-%m-%dT%H:%M:%SZ > "$DESTINO/.ultima_copia"
  echo "Copia hecha: $FECHA"
else
  "$PY" "$DESTINO/despliegue/avisos.py" "⚠️ La copia de seguridad diaria del bot a Cloudflare R2 ha FALLADO. Revisa: journalctl -u bot-ballenas-copia" || true
  exit 1
fi
