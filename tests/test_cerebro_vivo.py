"""El cerebro en vivo: decide con el modelo del laboratorio, con los mismos rasgos, y se abstiene ante cualquier duda."""

import json
import os
import shutil
import tempfile
import time
import unittest

from tests import comunes  # noqa: F401

import cerebro as cb
import cerebro_vivo as cv
import mercado_unificado as mu
from senales import Contexto, SerieFunding

H = 3600_000


def velas(n=120):
    return [{"t": k * H, "o": 100 + k, "h": 101 + k, "l": 99 + k, "c": 100.5 + k, "v": 10} for k in range(n)]


class MotorFalso:
    def __init__(self):
        self.contextos = {("ETH", "1h"): (0, Contexto(velas()))}
        self.funding_hist = {"ETH": (0, SerieFunding([{"time": k * H, "fundingRate": "0.0001"} for k in range(120)]), [])}


class ExternoFalso:
    def __init__(self, fresco=True):
        self.fresco = fresco

    def series_cerebro(self, moneda, iv, max_antiguedad_seg=1200, ahora=None):
        if not self.fresco:
            return None, None
        return ([{"t": k * H, "c": 100.5 + k, "v": 10.0, "tb": 8.0} for k in range(108, 120)],
                [{"time": k * H, "fundingRate": 0.00005} for k in range(100, 120)])


def modelo(sesgo):
    """Modelo válido con todo el peso en el flujo comprador externo."""
    filas = [({**dict.fromkeys(mu.RASGOS), "flujo1": f / 10, "tendencia": 0.0}, "ruptura_1h", sesgo + f / 10)
             for f in range(-10, 11)] * 5
    return cb.Modelo(mu.RASGOS, {"ruptura_1h"}).entrenar(filas)


class TestCerebroVivo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ruta = os.path.join(self.dir, "cerebro_modelo.json")
        self.cand = {"moneda": "ETH", "lado": "L", "intervalo": "1h", "estrategia": "ruptura_1h"}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def guardar(self, m, **extra):
        with open(self.ruta, "w", encoding="utf-8") as fh:
            json.dump({"version": "cerebro-x", "huella": "h", "modelos": {"1h": dict(m.a_dict(), **extra)}}, fh)

    def test_sin_modelo_se_abstiene(self):
        c = cv.CerebroVivo({}, self.ruta)
        d = c.decidir(self.cand, MotorFalso(), ExternoFalso())
        self.assertFalse(d["abrir"])
        self.assertIn("pasa el laboratorio", d["motivo"])
        self.assertEqual(c.contadores["abstiene"], 1)

    def test_modelo_corrupto_se_abstiene(self):
        with open(self.ruta, "w", encoding="utf-8") as fh:
            fh.write("{roto")
        c = cv.CerebroVivo({}, self.ruta)
        self.assertFalse(c.decidir(self.cand, MotorFalso(), ExternoFalso())["abrir"])
        self.assertIn("inválido", c.error)
        m = modelo(0.2)
        d = m.a_dict()
        d["w"] = d["w"][:-1]                                  # longitud que no cuadra
        with open(self.ruta, "w", encoding="utf-8") as fh:
            json.dump({"modelos": {"1h": d}}, fh)
        os.utime(self.ruta, (time.time() + 5, time.time() + 5))
        c.recargar()
        self.assertEqual(c.modelos, {})

    def test_decide_con_los_mismos_rasgos_que_el_laboratorio(self):
        self.guardar(modelo(0.0))
        c = cv.CerebroVivo({}, self.ruta)
        d = c.decidir(self.cand, MotorFalso(), ExternoFalso())
        ctx = MotorFalso().contextos[("ETH", "1h")][1]
        lab = mu.rasgos(ctx, len(ctx) - 1, "L", MotorFalso().funding_hist["ETH"][1],
                        mu.SerieExterna(ExternoFalso().series_cerebro("ETH", "1h")[0]),
                        mu.FundingHorario(ExternoFalso().series_cerebro("ETH", "1h")[1]))
        self.assertEqual(d["rasgos"], lab)                    # paridad vivo = laboratorio
        self.assertTrue(d["abrir"])                           # flujo comprador fuerte -> resultado esperado alto
        corto = c.decidir(dict(self.cand, lado="S"), MotorFalso(), ExternoFalso())
        self.assertFalse(corto["abrir"])                      # el mismo flujo va en contra del corto

    def test_externos_viejos_son_sin_dato(self):
        self.guardar(modelo(0.0))
        c = cv.CerebroVivo({}, self.ruta)
        d = c.decidir(self.cand, MotorFalso(), ExternoFalso(fresco=False))
        self.assertIn("flujo1", d["faltan"])
        self.assertIn("sin dato", d["motivo"])

    def test_rasgos_de_otra_version_se_rechazan(self):
        m = modelo(0.2)
        with open(self.ruta, "w", encoding="utf-8") as fh:
            json.dump({"modelos": {"1h": dict(m.a_dict(), rasgos=list(mu.RASGOS)[:-1])}}, fh)
        c = cv.CerebroVivo({}, self.ruta)
        self.assertFalse(c.decidir(self.cand, MotorFalso(), ExternoFalso())["abrir"])
        self.assertIn("otros rasgos", c.error)

    def test_error_interno_se_abstiene(self):
        self.guardar(modelo(0.2))
        c = cv.CerebroVivo({}, self.ruta)

        class MotorRoto:
            contextos = None
        d = c.decidir(self.cand, MotorRoto(), ExternoFalso())
        self.assertFalse(d["abrir"])
        self.assertIn("error del cerebro", d["motivo"])

    def test_acepta_modelo_solo_hyperliquid(self):
        filas = [({**dict.fromkeys(mu.RASGOS), "funding8h": f / 10}, "ruptura_1h", f / 10) for f in range(-10, 11)] * 5
        m = cb.Modelo(mu.RASGOS_HL, {"ruptura_1h"}).entrenar(filas)
        self.guardar(m, variante="cerebro_solo_hyperliquid")
        c = cv.CerebroVivo({}, self.ruta)
        self.assertIsNone(c.error)
        d = c.decidir(self.cand, MotorFalso(), ExternoFalso(fresco=False))
        self.assertIsNotNone(d["prediccion"])                 # decide sin depender de Binance/Bybit
        self.assertEqual(c.resumen()["modelos"]["1h"]["variante"], "cerebro_solo_hyperliquid")


if __name__ == "__main__":
    unittest.main()
