"""
Comprobaciones de despliegue (Windows y Linux). No conecta con Hyperliquid, no opera y no muestra secretos.

    python comprobar.py            (en el servidor: cd /opt/bot-ballenas && sudo -u botballenas .venv/bin/python comprobar.py)

Revisa:
  1. configuración válida (la misma validación que al arrancar) y modo;
  2. una sola instancia (el bloqueo del sistema operativo lo tiene el bot en marcha, o nadie);
  3. permisos: .env solo legible por su dueño (Linux) y que .env no esté en los paquetes;
  4. copias de seguridad recientes (si están configuradas);
  5. salud: latido del bot, estado global y latido del panel (proceso aparte);
  6. recuperación tras reinicio: servicios habilitados y con reinicio automático (Linux con systemd).
Devuelve código 1 si falla algo CRÍTICO.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
RESULTADOS = []


def anotar(ok, texto, critico=True):
    """ok: True (comprobado bien), False (mal) o None (DESCONOCIDO: no se pudo comprobar; nunca cuenta como bien)."""
    RESULTADOS.append((ok, critico, texto))
    marca = "?" if ok is None else "✓" if ok else ("✗" if critico else "!")
    print(f"  {marca} {texto}")


def leer(nombre):
    try:
        with open(os.path.join(AQUI, nombre), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def comprobar_config():
    import configuracion as conf
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(AQUI, ".env"))
    except ImportError:
        pass
    try:
        c, _ = conf.migrar(conf.cargar(os.path.join(AQUI, "config.json")))
        c = conf.validar(c, entorno=dict(os.environ))
    except Exception as e:  # noqa: BLE001
        from observabilidad import ocultar_secretos
        anotar(False, f"configuración NO válida: {ocultar_secretos(str(e))[:300]}")
        return None
    anotar(True, f"configuración válida · modo {c['modo'].upper()}")
    if c["modo"] == "real":
        anotar(True, "AVISO: modo REAL configurado (revisa la promoción y los límites del piloto)", critico=False)
    return c


def comprobar_instancia(c):
    from instancia import BloqueoInstancia, InstanciaDuplicada
    clave = (os.environ.get("HL_DIRECCION_CUENTA", "").strip().lower() if c and c["modo"] == "real"
             else f"{(c or {}).get('modo', 'simulado')}:{AQUI}")
    try:
        BloqueoInstancia(AQUI, clave).tomar().soltar()
        anotar(True, "ningún bot en marcha con esta carpeta/cuenta en ESTE equipo (el bloqueo está libre)", critico=False)
        return False
    except InstanciaDuplicada:
        anotar(True, "hay exactamente un bot en marcha con esta carpeta/cuenta (bloqueo tomado)")
        return True


def comprobar_permisos():
    env = os.path.join(AQUI, ".env")
    if not os.path.exists(env):
        anotar(True, ".env no existe (en simulado/shadow no hace falta)", critico=False)
    elif os.name != "nt":
        modo = stat.S_IMODE(os.stat(env).st_mode)
        anotar(not (modo & 0o077), f".env con permisos {oct(modo)} (debe ser 600: solo su dueño)")
    else:
        anotar(True, ".env presente (en Windows, guárdalo solo en tu usuario)", critico=False)
    paquetes = [f for f in os.listdir(AQUI) if f.endswith((".zip", ".tar.gz", ".tgz"))]
    for p in paquetes:
        ruta = os.path.join(AQUI, p)
        try:
            if p.endswith(".zip"):
                import zipfile
                nombres = zipfile.ZipFile(ruta).namelist()
            else:
                import tarfile
                with tarfile.open(ruta) as t:
                    nombres = t.getnames()
            anotar(not any(os.path.basename(n) == ".env" for n in nombres), f"el paquete {p} no contiene .env")
        except Exception:  # noqa: BLE001
            anotar(False, f"no pude revisar el paquete {p}", critico=False)


def comprobar_copias():
    if not os.path.exists(os.path.join(AQUI, ".copia.env")):
        anotar(False, "copias de seguridad NO configuradas (servidor: despliegue/configurar_copias.sh)", critico=False)
        return
    try:
        with open(os.path.join(AQUI, ".ultima_copia"), encoding="utf-8") as fh:
            ultima = fh.read().strip()
        from datetime import datetime
        edad = (time.time() - datetime.fromisoformat(ultima.replace("Z", "+00:00")).timestamp()) / 3600
        anotar(edad < 30, f"última copia de seguridad hace {edad:.1f} h (debe ser < 30 h)")
    except (OSError, ValueError):
        anotar(False, "configuradas, pero no consta ninguna copia hecha")


def comprobar_salud(en_marcha):
    est = leer("estado_sistema.json") or {}
    lat = est.get("latido_ts")
    if not lat:
        if en_marcha:
            anotar(False, "el bot está en marcha pero no ha escrito su estado de salud")
        else:
            anotar(None, "salud del bot DESCONOCIDA: aún no ha escrito su estado (¿no ha arrancado nunca aquí?)",
                   critico=False)
    else:
        edad = time.time() - lat
        anotar(edad < 180 or not en_marcha, f"latido del bot hace {edad:.0f} s", critico=en_marcha)
        g = est.get("estado_global") or (est.get("proceso") or {}).get("estado")
        anotar(g == "correcto", f"estado global del bot: {g}"
               + (f" (sin datos: {', '.join(est.get('obligatorios_sin_datos') or [])})" if est.get("obligatorios_sin_datos") else ""),
               critico=g == "fallido")
    pl = leer("panel_latido.json") or {}
    if pl.get("ts"):
        anotar(time.time() - pl["ts"] < 120, f"latido del panel hace {time.time() - pl['ts']:.0f} s", critico=False)
    else:
        anotar(False, "el panel no ha dado señales (¿arrancado?)", critico=False)


def comprobar_servicios():
    if os.name == "nt" or not shutil.which("systemctl") or not os.path.exists("/etc/systemd/system/bot-ballenas.service"):
        anotar(True, "servicios de systemd no instalados en este equipo (Windows: el arranque tras reinicio lo da "
                     "la tarea programada de activar-arranque-automatico.bat)", critico=False)
        return
    for svc in ("bot-ballenas", "bot-ballenas-panel"):
        habil = subprocess.run(["systemctl", "is-enabled", svc], capture_output=True, text=True).stdout.strip()
        activo = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True).stdout.strip()
        anotar(habil == "enabled", f"{svc}: habilitado al arrancar ({habil})")
        anotar(activo == "active", f"{svc}: en marcha ({activo})", critico=svc == "bot-ballenas")
    for t in ("bot-ballenas-copia.timer", "bot-ballenas-laboratorio.timer", "bot-ballenas-investigacion.timer"):
        habil = subprocess.run(["systemctl", "is-enabled", t], capture_output=True, text=True).stdout.strip()
        anotar(habil == "enabled", f"{t}: {habil}", critico=False)
    unidad = "/etc/systemd/system/bot-ballenas.service"
    if os.path.exists(unidad):
        with open(unidad, encoding="utf-8") as fh:
            anotar("Restart=" in fh.read(), "el servicio del bot se reinicia solo si falla")


def main():
    sys.path.insert(0, AQUI)
    try:
        from compat import consola_utf8
        consola_utf8()
    except ImportError:
        pass
    print("Comprobaciones de despliegue (no conecta ni opera):")
    c = comprobar_config()
    en_marcha = comprobar_instancia(c)
    comprobar_permisos()
    comprobar_copias()
    comprobar_salud(en_marcha)
    comprobar_servicios()
    criticos = [t for ok, cr, t in RESULTADOS if ok is False and cr]
    avisos = sum(1 for ok, cr, _ in RESULTADOS if ok is False and not cr)
    desconocidos = sum(1 for ok, _, _ in RESULTADOS if ok is None)
    if criticos:
        titulo = f"{len(criticos)} PROBLEMA(S) CRÍTICO(S)"
    elif avisos or desconocidos:
        titulo = "SIN PROBLEMAS CRÍTICOS (pero no todo está confirmado)"
    else:
        titulo = "TODO CORRECTO"
    print(f"\n{titulo} · {avisos} aviso(s) · {desconocidos} desconocido(s).")
    return 1 if criticos else 0


if __name__ == "__main__":
    sys.exit(main())
