"""El cerebro en el laboratorio: «solo Hyperliquid» usa solo historia de HL, hay línea base y cartera con límites."""

import os
import random
import tempfile
import types
import unittest

from tests import comunes  # noqa: F401

import laboratorio as L
import validacion as vd
from senales import Contexto, SerieFunding
from tests.comunes import config

H = 4 * 3600


class TestEvaluarCerebro(unittest.TestCase):
    def test_solo_hl_base_y_cartera(self):
        random.seed(1)
        n = 1500
        ms, oos = [], {}
        for mon in ("AAA", "BBB"):
            p, velas = 100.0, []
            for k in range(n):
                p *= 1 + random.gauss(0, 0.01)
                velas.append({"t": k * H * 1000, "o": p, "h": p * 1.01, "l": p * .99, "c": p, "v": 10})
            ms.append(types.SimpleNamespace(moneda=mon, ctx=Contexto(velas), idx_t={v["t"] / 1000: i for i, v in enumerate(velas)},
                                            funding=SerieFunding([{"time": k * 3600_000, "fundingRate": "0.0001"}
                                                                  for k in range(n * 4)])))
            for k in range(100, n - 10, 5):
                oos.setdefault("ruptura_4h", []).append({"t": k * H, "cierre_ts": k * H + 2 * H, "r": random.gauss(0.05, 1),
                                                         "moneda": mon, "lado": "L", "sl_pct": 1})
        c = config()
        pa = vd.particionar(0, n * H, 5, 0.15)
        pa = {"pliegues": pa["pliegues"][1:], "holdout": pa["holdout"]}
        d = {"inicio_hyperliquid": {"4h": {"AAA": n * H / 2, "BBB": 0}}, "externo": {}}
        reg = vd.RegistroHoldout(os.path.join(tempfile.mkdtemp(), "h.json"))
        out = L.evaluar_cerebro("4h", d, ms, oos, {}, pa, 8 * 3600, reg, 10, c["laboratorio"], c)
        self.assertLess(out["candidatas_solo_historia_hl"], out["candidatas"])     # se excluye la historia previa a HL
        for k in ("cerebro_con_todo", "cerebro_solo_hyperliquid"):
            reglas = [x["regla"] for x in out[k]["veredicto"]["comprobaciones"]]
            self.assertTrue(any("línea base" in r for r in reglas), reglas)
            self.assertTrue(any("cartera" in r for r in reglas), reglas)
            self.assertIn("cartera", out[k])


if __name__ == "__main__":
    unittest.main()
