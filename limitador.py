"""
Límite de peticiones a Hyperliquid: 1200 de "peso" por minuto y por IP. Si se supera, responde 429
y la petición falla. Esto envuelve la función post de Info para que todas las consultas del bot
(velas, libro, posiciones, ballenas...) respeten un presupuesto común, esperando lo justo si hace
falta, y reintentando con calma si aun así llega un 429.
"""

import threading
import time
from collections import deque

PRESUPUESTO_MINUTO = 900          # por debajo de 1200 para dejar margen al websocket y a otros programas

PESOS = {"l2Book": 2, "allMids": 2, "clearinghouseState": 2, "orderStatus": 2, "spotClearinghouseState": 2,
         "exchangeStatus": 2, "userRole": 60}


def peso(cuerpo):
    tipo = cuerpo.get("type") if isinstance(cuerpo, dict) else None
    if tipo == "candleSnapshot":
        return 20 + 6          # 20 + 1 por cada 60 velas (el bot pide ~300)
    return PESOS.get(tipo, 20)


class Limitador:
    def __init__(self, presupuesto=PRESUPUESTO_MINUTO, log=None):
        self.presupuesto, self.log = presupuesto, log
        self.historial = deque()
        self.bloqueo = threading.Lock()
        self.esperas = 0
        self.reintentos_429 = 0

    def reservar(self, p):
        while True:
            with self.bloqueo:
                ahora = time.time()
                while self.historial and ahora - self.historial[0][0] >= 60:
                    self.historial.popleft()
                usado = sum(x[1] for x in self.historial)
                if usado + p <= self.presupuesto:
                    self.historial.append((ahora, p))
                    return
                espera = 60 - (ahora - self.historial[0][0]) + 0.05
            self.esperas += 1
            time.sleep(min(max(espera, 0.1), 5))

    def envolver(self, info):
        """Sustituye info.post por una versión que respeta el presupuesto y reintenta los 429."""
        original = info.post

        def post(ruta, cuerpo=None, *a, **k):
            self.reservar(peso(cuerpo or {}))
            for intento in range(4):
                try:
                    return original(ruta, cuerpo, *a, **k)
                except Exception as e:
                    if "429" not in str(e)[:20] or intento == 3:
                        raise
                    self.reintentos_429 += 1
                    if self.log:
                        self.log.warning("Hyperliquid pide calma (429): espero %d s y reintento.", 5 * (intento + 1))
                    time.sleep(5 * (intento + 1))
                    self.reservar(peso(cuerpo or {}))
        info.post = post
        return info
