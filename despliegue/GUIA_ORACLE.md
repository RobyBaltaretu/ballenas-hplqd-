# Pasar el bot a Oracle Cloud (gratis, 24/7)

Al terminar tendrás:
- El bot y el panel funcionando en un servidor de Oracle en Madrid, día y noche, **sin depender de tu PC**.
- El panel en tu dirección de Cloudflare (con tu email), desde el móvil o cualquier sitio.
- Una **copia diaria** de todo lo que aprende el bot en Cloudflare R2.
- Un **aviso por Telegram** si el servidor o el bot dejan de funcionar (Healthchecks.io).
- El **departamento de investigación** cada noche, el **laboratorio semanal** y un **recordatorio mensual** para entrar en Oracle, automáticos.

Tiempo: 1–2 horas la primera vez. Tú haces las cuentas y pegas las claves; las claves **nunca** salen del servidor.

---

## 1. Crear la cuenta de Oracle

1. Entra en https://www.oracle.com/es/cloud/free/ → **Comienza gratis**.
2. Rellena tus datos. Como **región de inicio (Home Region)** elige **Spain Central (Madrid)**. No se puede cambiar después y las máquinas gratuitas solo se crean ahí.
3. Te pedirá una tarjeta para verificar que eres una persona. Con lo gratuito ("Always Free") no cobra.

## 2. Crear la máquina

En la consola de Oracle: menú ☰ → **Compute → Instances → Create instance**.

| Campo | Valor |
|---|---|
| Name | `bot-ballenas` |
| Image | **Canonical Ubuntu 24.04** (la normal, no "Minimal") |
| Shape | **Change shape → Ampere → VM.Standard.A1.Flex**: **1 OCPU** y **2 GB** de memoria. Debe poner *Always Free-eligible* |
| Networking | Deja que cree una red nueva (*Create new virtual cloud network*) con subred pública y **Assign a public IPv4 address** |
| Add SSH keys | **Generate a key pair for me** → **Save private key** |
| Boot volume | Lo que venga por defecto (unos 50 GB) |

**Guarda la clave privada (`.key`) en una carpeta tuya, por ejemplo `Documentos\claves-oracle`, NO dentro de la carpeta del bot** (así nunca acaba en un paquete). Sin ella no podrás entrar al servidor.

Pulsa **Create**. Si sale *Out of capacity*, prueba otro *Availability domain* o vuelve a intentarlo más tarde (a veces tarda unos días en haber hueco).

Cuando esté en verde (*Running*), copia la **Public IP address**.

> ¿Por qué 2 GB y no los 12 GB gratuitos? Oracle puede recuperar una máquina gratuita si durante 7 días su procesador, su red **y** su memoria están por debajo del 20 %. Con 2 GB, el uso normal del bot ya supera ese 20 % de memoria. El resto de lo gratuito queda libre.

## 3. Conectarte desde tu PC

Abre **PowerShell** y ejecuta (cambia la ruta y la IP):

```
icacls "C:\Users\rober\Documents\claves-oracle\ssh-key.key" /inheritance:r /grant:r "$($env:USERNAME):R"
ssh -i "C:\Users\rober\Documents\claves-oracle\ssh-key.key" ubuntu@IP_DEL_SERVIDOR
```

La primera vez pregunta si confías en el servidor: escribe `yes`. Ya estás dentro (el texto empieza por `ubuntu@bot-ballenas`). Para salir: `exit`.

## 4. Subir el bot con todo lo que ha aprendido

En tu PC:
1. **Cierra la ventana del bot** (el bot del PC debe quedar parado).
2. Doble clic en `herramientas\preparar-migracion.bat`. Crea `bot-migracion.tar.gz` en el Escritorio con el código, tu `config.json` y todos los datos (cuenta simulada, memoria, registro, datos de mercado y archivo). **No incluye tu `.env`.**
3. En PowerShell:
   ```
   scp -i "C:\Users\rober\Documents\claves-oracle\ssh-key.key" "$env:USERPROFILE\Desktop\bot-migracion.tar.gz" ubuntu@IP_DEL_SERVIDOR:~
   ```

En el servidor (`ssh ...` como en el paso 3):
```
tar -xzf bot-migracion.tar.gz
cd bot-ballenas
sudo bash despliegue/instalar_vps.sh
```

El instalador tarda unos minutos y te pregunta:
- **Token del bot de Telegram** y **chat_id**: los mismos que en tu `.env` del PC (ábrelo con el Bloc de notas para copiarlos).
- **URL de Healthchecks**: si aún no la tienes, deja en blanco y la añades en el paso 5.
- **Contraseña del panel**: opcional (Cloudflare ya pide tu email).

## 5. Aviso si algo se cae (Healthchecks.io)

1. Crea una cuenta gratis en https://healthchecks.io.
2. **Add Check**: nombre `Bot ballenas`, **Period 10 minutes**, **Grace 10 minutes**.
3. En **Integrations**, añade **Telegram** (te guía para conectar tu chat) o deja el email.
4. Copia la **Ping URL** (`https://hc-ping.com/...`).
5. En el servidor:
   ```
   sudo nano /opt/bot-ballenas/.env
   ```
   Pega la URL en `HEALTHCHECK_URL=` y guarda (Ctrl+O, Enter, Ctrl+X). Si el bot ya estaba en marcha: `sudo systemctl restart bot-ballenas`.

El bot manda una señal cada 5 minutos. Si deja de llegar (bot parado, servidor caído, sin red), Healthchecks te avisa.

## 6. Copia diaria a Cloudflare R2

En el panel de Cloudflare:
1. **R2 Object Storage** → actívalo. El plan gratuito incluye 10 GB; Cloudflare puede pedirte una forma de pago para activarlo aunque no cobre dentro de ese límite.
2. **Create bucket**: `bot-ballenas-copias`.
3. **Manage API tokens → Create API token**: permiso **Object Read & Write**, aplicado **solo al cubo** `bot-ballenas-copias`. Copia el **Access Key ID** y el **Secret Access Key** (solo se ve una vez) y tu **Account ID** (aparece en la página de R2).

En el servidor:
```
sudo bash /opt/bot-ballenas/despliegue/configurar_copias.sh
```
Pega los datos. Hace la primera copia y deja programadas las siguientes (cada día a las 03:30 UTC, con 14 días de versiones). Si una copia falla, te llega un aviso por Telegram.

## 7. El panel por Cloudflare Tunnel

En Cloudflare **Zero Trust → Networks → Tunnels**, abre el túnel que ya usas en el PC → **Configure**. En *Install and run connectors* elige **Debian** y **arm64** y copia el comando (lleva un texto largo que empieza por `eyJ`).

En el servidor:
```
sudo bash /opt/bot-ballenas/despliegue/cloudflare_linux.sh
```
Pega el comando. La ruta pública del túnel sigue apuntando a `http://localhost:8080`.

En tu PC, doble clic en `herramientas\desconectar-cloudflare.bat` para quitar el conector del PC (si no, Cloudflare repartiría las visitas entre el PC y el servidor).

> Desde fuera, el panel es de **solo lectura**: puedes ver todo, pero los botones no actúan. Para controlarlo usa Telegram (`/pausa`, `/reanudar`, `/cerrarbot CONFIRMAR`). Si quieres botones también por el túnel, pon `"comandos_remotos": true` en el bloque `panel` de `config.json` (Cloudflare ya exige tu email).

## 8. Arrancar

En el servidor:
```
sudo systemctl start bot-ballenas bot-ballenas-panel
journalctl -u bot-ballenas -f
```
Verás al bot arrancar (Ctrl+C para dejar de mirar; el bot sigue). Te llegará "🤖 Bot en marcha" por Telegram y el panel se verá en tu dirección de Cloudflare.

En tu PC, para que no vuelva a arrancar allí:
- `herramientas\desactivar-arranque-automatico.bat`
- `herramientas\desactivar-laboratorio-semanal.bat` y `herramientas\desactivar-investigacion-nocturna.bat` (ya se pasan en el servidor)

## 9. La primera semana

- En Oracle: **Instances → bot-ballenas → Metrics**. La **memoria** debería estar por encima del 20 %. Si no, dímelo y lo ajustamos.
- Revisa que llegan las copias: en R2 deben aparecer carpetas nuevas cada día.

## Uso diario

| Para… | Comando (en el servidor) |
|---|---|
| Ver lo que hace | `journalctl -u bot-ballenas -f` |
| Parar / arrancar / reiniciar | `sudo systemctl stop/start/restart bot-ballenas` |
| Estado de las tareas programadas | `systemctl list-timers 'bot-ballenas*'` |
| Hacer una copia ahora | `sudo -u botballenas bash /opt/bot-ballenas/despliegue/copia_seguridad.sh` |
| Última ronda de investigación | `journalctl -u bot-ballenas-investigacion -n 50` |
| Comprobar el despliegue (config, una sola instancia, permisos, copias, salud, servicios) | `cd /opt/bot-ballenas && sudo -u botballenas .venv/bin/python comprobar.py` |
| Ver lo aprendido | `cd /opt/bot-ballenas && sudo -u botballenas .venv/bin/python informe.py` |

**Actualizar a una versión nueva:** en el PC, doble clic en `herramientas\preparar-actualizacion.bat` (crea `bot-actualizacion.tar.gz` SOLO con código: sin `.env`, sin `config.json`, sin datos; si por error contuviera un `.env`, lo borra). Súbelo con `scp` como en el paso 4 y en el servidor: `tar -xzf bot-actualizacion.tar.gz && cd bot-ballenas && sudo bash despliegue/actualizar_vps.sh`. Conserva `.env`, `config.json` y todos los datos, y no arranca si la configuración no es válida. **Nunca subas la carpeta del PC entera** (llevaría tu `.env`).

**Si Oracle recuperara la máquina** (o quieres cambiar de servidor): crea otra (pasos 2–4, con el paquete de código), ejecuta `configurar_copias.sh` con los mismos datos de R2 y después `sudo bash /opt/bot-ballenas/despliegue/restaurar.sh`. Recupera todo lo de la última copia.

**Reinicios:** el servidor instala solo las actualizaciones de seguridad y, si hace falta, se reinicia a las 04:00 UTC. El bot se apaga ordenadamente y vuelve a arrancar solo, sin que nadie inicie sesión.

## Seguridad

- Entrar al servidor solo es posible con tu clave `.key`. No la compartas.
- El panel escucha solo en `127.0.0.1` y se ve por el túnel cifrado de Cloudflare (HTTPS). No uses la contraseña Basic del panel por HTTP directo: viajaría sin cifrar.
- El servidor no tiene ningún puerto abierto salvo SSH; el panel solo se ve por el túnel de Cloudflare.
- `.env` y los datos de R2 solo los puede leer el usuario del bot. Nunca van en las copias ni en los paquetes.
- En simulado no hacen falta claves de Hyperliquid. Si algún día pasas a real, usa una API wallet (no puede retirar fondos) y una subcuenta dedicada (ver LEEME).
