# Poner el bot en un VPS (24/7, sin depender de tu portátil)

> **Oracle Cloud gratis:** sigue `GUIA_ORACLE.md`, que incluye copias a Cloudflare R2, aviso de caída y paso de los datos del PC. Esta guía sirve para cualquier otro VPS con Ubuntu; los scripts son los mismos.

Un VPS es un ordenador pequeño en la nube que está siempre encendido. Para este bot basta el más barato: 1–2 CPU y 2 GB de RAM, con Ubuntu 24.04. Suelen costar entre **4 y 6 €/mes** en proveedores europeos como Hetzner, OVH o Contabo. Comprueba el precio actual en su web.

> Elige un centro de datos en Europa. Hyperliquid no da servicio a residentes de algunos países, como EE. UU. Revisa sus condiciones.

## 1. Contratar el VPS

1. Crea la cuenta en el proveedor y un servidor con **Ubuntu 24.04**.
2. Copia la **IP** que te dan y la contraseña de `root`. Mejor aún, añade una clave SSH si el proveedor lo permite.

## 2. Subir el bot (desde tu portátil)

En Windows, abre **PowerShell**. En Mac o Linux, la **Terminal**. Ponte en la carpeta donde está `bot-ballenas` y ejecuta:

En Windows, doble clic en `herramientas\preparar-migracion.bat`: crea `bot-migracion.tar.gz` en el Escritorio con el
código y los datos, **sin `.env`**. Súbelo:

```
scp bot-migracion.tar.gz root@TU_IP:/root/
ssh root@TU_IP "tar -xzf bot-migracion.tar.gz"
```

**No subas la carpeta entera con `scp -r`**: arrastraría tu `.env` con las claves. Las claves se escriben
directamente en el servidor: el instalador te las pide (no se muestran al escribir) y las guarda en
`/opt/bot-ballenas/.env` con permisos 600 (solo las lee el usuario del bot).

## 3. Instalar (en el VPS)

```
ssh root@TU_IP
cd /root/bot-ballenas
bash despliegue/instalar_vps.sh
```

El instalador hace lo siguiente:
- Instala Python.
- Activa las actualizaciones de seguridad automáticas y un cortafuegos que solo deja entrar por SSH.
- Crea un usuario sin privilegios para el bot.
- Copia el bot a `/opt/bot-ballenas` y lo deja como servicio que **arranca solo y se reinicia si falla**.

## 4. Claves, preentrenamiento y arranque

```
nano /opt/bot-ballenas/.env                  # pega tus claves; guarda con Ctrl+O, Enter, Ctrl+X
cd /opt/bot-ballenas
sudo -u botballenas .venv/bin/python laboratorio.py --guardar-memoria   # recomendado: elige y valida las estrategias
systemctl start bot-ballenas bot-ballenas-panel
journalctl -u bot-ballenas -f                # ver lo que hace (Ctrl+C para salir)
```

> Por defecto el bot arranca en **modo simulado** (`"modo": "simulado"` en `config.json`). El modo real exige subcuenta dedicada, habilitación explícita en `config.json` y en `.env` y estrategias aprobadas con `promocion.py` (ver la sección «Pasar a real» del LEEME). Si falta algo, el servicio no arranca y `journalctl -u bot-ballenas` dice por qué. También puedes usar `"modo": "shadow"` para observar sin órdenes.

## 5. Ver el panel desde tu portátil

El panel solo escucha dentro del VPS, así que nadie de fuera puede verlo. Para abrirlo desde tu portátil, crea un túnel SSH:

```
ssh -L 8080:localhost:8080 root@TU_IP
```

Deja esa ventana abierta y entra en **http://localhost:8080** desde el navegador.

Si configuraste Telegram, te llegará "🤖 Bot en marcha". A partir de ahí puedes controlarlo desde el móvil con /estado, /pausa, /reanudar, /cerrarbot CONFIRMAR (solo las posiciones del bot) e /informe.

## Uso diario

| Para… | Comando |
|---|---|
| Ver lo que hace | `journalctl -u bot-ballenas -f` |
| Parar | `systemctl stop bot-ballenas` |
| Arrancar | `systemctl start bot-ballenas` |
| Reiniciar (tras cambiar `config.json`) | `systemctl restart bot-ballenas` |
| Reiniciar el panel | `systemctl restart bot-ballenas-panel` |
| Ver lo aprendido | `cd /opt/bot-ballenas && .venv/bin/python informe.py` |

**Para actualizar a una versión nueva del bot**, en Windows doble clic en `herramientas\preparar-actualizacion.bat` (paquete SOLO con código: sin `.env`, sin `config.json`, sin datos), súbelo con `scp`, descomprímelo y ejecuta `sudo bash despliegue/actualizar_vps.sh` desde esa carpeta. Se conservan las claves, el estado y la memoria. Después: `sudo -u botballenas /opt/bot-ballenas/.venv/bin/python /opt/bot-ballenas/comprobar.py`.

Si el bot se apaga por **pérdida máxima total** o por un **error de configuración**, el servicio no lo vuelve a arrancar solo. Revisa qué ha pasado antes de arrancarlo a mano.

## Seguridad

- La API wallet **no puede retirar fondos**. Aunque alguien entrase en el VPS, no podría sacar tu dinero, aunque sí operar con él. Si sospechas algo, revócala en app.hyperliquid.xyz/API.
- Usa una contraseña de root larga o, mejor, claves SSH.
- No subas `.env` a ningún sitio público.
