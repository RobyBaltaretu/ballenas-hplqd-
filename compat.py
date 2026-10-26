"""
Detalles para que el bot funcione igual en Windows, macOS y Linux.
"""

import os
import subprocess
import sys
import time


def reemplazar(origen, destino, intentos=20):
    """os.replace con reintentos: en Windows falla si otro programa (p. ej. el panel)
    tiene el archivo abierto en ese mismo instante."""
    for i in range(intentos):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if i == intentos - 1:
                raise
            time.sleep(0.1)


def consola_utf8():
    """Evita errores al mostrar acentos, emojis o símbolos en la consola de Windows."""
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def evitar_suspension(log=None):
    """Impide que el ordenador se duerma por inactividad mientras el bot está en marcha.
    No evita que se duerma si cierras la tapa del portátil o se queda sin batería."""
    try:
        if sys.platform == "win32":
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        elif sys.platform == "darwin":
            subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])
        else:
            return
        if log:
            log.info("El ordenador no se dormirá mientras el bot esté en marcha.")
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("No pude evitar que el ordenador se duerma (%s).", e)
