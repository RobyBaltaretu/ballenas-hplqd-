"""
Una sola instancia del bot por carpeta y por cuenta.

Dos bots operando la misma cuenta se pisarían: cada uno vería las órdenes del otro como "externas", duplicaría
entradas y stops y la contabilidad dejaría de cuadrar. Por eso, al arrancar, el bot toma un BLOQUEO DEL SISTEMA
OPERATIVO sobre un archivo:
  - en la carpeta del bot (bot.lock), y
  - en una carpeta común del usuario (~/.bot-ballenas/bloqueos/cuenta-<hash>.lock), con un hash de la cuenta,
    para que dos copias del bot en carpetas distintas del MISMO equipo tampoco operen la misma cuenta.

El bloqueo lo libera el sistema operativo cuando el proceso termina (también si se cuelga o se corta la luz),
así que nunca queda "pegado": no hace falta borrar nada a mano tras una caída. Funciona en Linux/macOS (fcntl)
y en Windows (msvcrt).

Lo que NO puede evitar: dos equipos distintos (p. ej. tu PC y el servidor) con la misma cuenta. Para eso:
  - nunca arranques los dos a la vez (la guía de migración lo dice en cada paso);
  - en real, al arrancar, el bot se niega si hay órdenes o posiciones que no puede atribuirse, y en marcha
    avisa si aparece una orden con CLOID desconocido (síntoma típico de otra instancia).
"""

import hashlib
import json
import os
import socket
import time


class InstanciaDuplicada(RuntimeError):
    pass


class _Bloqueo:
    def __init__(self, ruta):
        self.ruta = ruta
        self.fh = None

    def tomar(self):
        os.makedirs(os.path.dirname(self.ruta) or ".", exist_ok=True)
        fh = open(self.ruta, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            try:
                with open(self.ruta + ".info", encoding="utf-8") as i:
                    dueño = i.read().strip()
            except OSError:
                dueño = ""
            raise InstanciaDuplicada(dueño or "otro proceso") from None
        self.fh = fh
        try:      # quién lo tiene (informativo; el bloqueo real es el del sistema operativo)
            with open(self.ruta + ".info", "w", encoding="utf-8") as i:
                i.write(json.dumps({"pid": os.getpid(), "equipo": socket.gethostname(), "desde": int(time.time())}))
        except OSError:
            pass
        return self

    def soltar(self):
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


def ruta_comun(cuenta, base=None):
    base = base or os.path.join(os.path.expanduser("~"), ".bot-ballenas", "bloqueos")
    h = hashlib.sha256(str(cuenta or "simulado").lower().encode()).hexdigest()[:16]
    return os.path.join(base, f"cuenta-{h}.lock")


class BloqueoInstancia:
    """Toma los dos bloqueos o lanza InstanciaDuplicada (sin dejar ninguno tomado)."""

    def __init__(self, carpeta, cuenta, base_comun=None):
        self.locales = [_Bloqueo(os.path.join(carpeta, "bot.lock")), _Bloqueo(ruta_comun(cuenta, base_comun))]
        self.tomados = []

    def tomar(self):
        try:
            for b in self.locales:
                self.tomados.append(b.tomar())
        except InstanciaDuplicada as e:
            self.soltar()
            raise InstanciaDuplicada(f"ya hay un bot en marcha con esta carpeta o esta cuenta ({e})") from None
        return self

    def soltar(self):
        for b in reversed(self.tomados):
            b.soltar()
        self.tomados = []

    def __enter__(self):
        return self.tomar()

    def __exit__(self, *exc):
        self.soltar()
