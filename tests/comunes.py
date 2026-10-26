"""Utilidades de prueba: mercado falso, configuración válida y carpetas temporales. NUNCA hay red ni claves."""

import copy
import logging
import os
import shutil
import sys
import tempfile
import unittest

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if AQUI not in sys.path:
    sys.path.insert(0, AQUI)

import configuracion as conf  # noqa: E402

LOG = logging.getLogger("pruebas")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def config(**cambios):
    """Configuración por defecto, validada, en modo simulado (sin entorno: no hay claves)."""
    c = copy.deepcopy(conf.DEFECTOS)
    c["modo"] = "simulado"
    for ruta, valor in cambios.items():
        d = c
        partes = ruta.split("__")
        for p in partes[:-1]:
            d = d[p]
        d[partes[-1]] = valor
    return conf.validar(c, entorno={})


class MercadoFalso:
    """Precios y libro controlados por la prueba. Imita lo poco de hyperliquid.info.Info que usa CuentaPapel."""

    def __init__(self, precios=None):
        self.precios = dict(precios or {"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0})
        self.tasas = {}
        self.marcas = {}                    # marca distinta del mid (para probar divergencias)
        self.llamadas_mutantes = 0          # si algo intentara operar a través de Info, se vería aquí

    def all_mids(self):
        return {k: str(v) for k, v in self.precios.items()}

    def l2_snapshot(self, moneda):
        px = self.precios[moneda]
        return {"levels": [[{"px": str(px * 0.9999), "sz": "1000"}], [{"px": str(px * 1.0001), "sz": "1000"}]]}

    def meta(self):
        return {"universe": [{"name": m, "szDecimals": 5 if m == "BTC" else 3, "maxLeverage": 20} for m in self.precios]}

    def meta_and_asset_ctxs(self):
        return self.meta(), [{"funding": str(self.tasas.get(m, 0.0)), "markPx": str(self.marcas.get(m, p)),
                              "oraclePx": str(p)} for m, p in self.precios.items()]


def nuevo_papel(carpeta, mercado, saldo=1000.0):
    from papel import CuentaPapel, InfoConPapel
    cuenta = CuentaPapel(mercado, os.path.join(carpeta, "papel.json"), saldo, 0.00015, 0.00045, LOG)
    return cuenta, InfoConPapel(mercado, cuenta, "simulado")


def mover(cuenta, mercado, moneda, px, marca=None):
    """Mueve el precio (mid y, salvo que se diga otra cosa, también la marca)."""
    mercado.precios[moneda] = px
    if marca is not None:
        mercado.marcas[moneda] = marca
    else:
        mercado.marcas.pop(moneda, None)
    cuenta._mids_cache = (0.0, {})
    cuenta._ctx_cache = (0.0, {})


class ConCarpeta(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bot-prueba-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def evidencia_para_apta(reg, clave, huella, n=30, r=0.3):
    """Evidencia que cumple TODOS los requisitos de 'apta_revision' (laboratorio superado reciente + 30
    operaciones paper válidas repartidas en más de 14 días). Solo para pruebas."""
    import time
    reg.registrar_evidencia(clave, "laboratorio", huella, "lab-1", {"estado": "supera"})
    for i in range(n):
        reg.registrar_evidencia(clave, "paper", huella, f"t{i}", {"r_neto": r + (0.2 if i % 2 else -0.2),
                                                                  "r_bruto": r + 0.25, "pnl_neto": 1.0})
    a = reg.d["estrategias"][clave]["acumulado"][f"paper:{huella}"]
    a["primera_ts"] = time.time() - 20 * 86400
