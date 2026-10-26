"""Carry realista: cobra el funding, paga la base y las 4 comisiones; descarta contado que no cuadra."""

import unittest

from tests import comunes

import laboratorio as lab
from senales import SerieFunding

H4 = 4 * 3600_000


class TestCarry(unittest.TestCase):
    def setUp(self):
        lab.configurar_globales(comunes.config())

    def mercado(self, n=3000):
        velas = [{"t": k * H4, "o": 100, "h": 101, "l": 99, "c": 100.0, "v": 10} for k in range(n)]
        fund = SerieFunding([{"time": k * 3600_000, "fundingRate": "0.0001"} for k in range(n * 4)])   # ≈ 88 %/año
        return lab.Mercado("BTC", "4h", velas, fund)

    def test_funding_menos_costes_y_base(self):
        m = self.mercado()
        spot = {"BTC": [{"t": k * 2 * H4, "c": 100.0} for k in range(1500)]}
        t1 = m.ctx.t[-1]
        part = {"pliegues": [(t1 * 0.4, t1 * 0.55), (t1 * 0.55, t1 * 0.7), (t1 * 0.7, t1 * 0.85)], "holdout": (t1 * 0.85, t1)}
        r = lab.carry_realista([m], spot, (0.00045, 0.00015), part, 0, {"BTC": 0})
        # Funding siempre alto: una operación que no se cierra hasta el final -> como mucho alguna; sin base (spot = perp)
        todas = r["fuera_de_muestra"]
        self.assertTrue(todas.get("n", 0) == 0 or todas["base_media_pct"] == 0)

    def test_contado_de_otra_moneda_se_descarta(self):
        m = self.mercado()
        spot = {"BTC": [{"t": k * 2 * H4, "c": 3.0} for k in range(1500)]}
        t1 = m.ctx.t[-1]
        part = {"pliegues": [(t1 * 0.4, t1 * 0.7)], "holdout": (t1 * 0.85, t1)}
        r = lab.carry_realista([m], spot, (0.00045, 0.00015), part, 0, {})
        self.assertEqual(r["fuera_de_muestra"].get("n", 0), 0)

    def test_cierra_y_cuenta_base(self):
        n = 3000
        velas = [{"t": k * H4, "o": 100, "h": 101, "l": 99, "c": 100.0, "v": 10} for k in range(n)]
        # funding alto 10 días, luego cero, repetido
        regs = [{"time": k * 3600_000, "fundingRate": "0.0001" if (k // 240) % 2 == 0 else "0"} for k in range(n * 4)]
        m = lab.Mercado("BTC", "4h", velas, SerieFunding(regs))
        spot = {"BTC": [{"t": k * 2 * H4, "c": 100.0 if (k // 30) % 2 == 0 else 99.9} for k in range(1500)]}
        t1 = m.ctx.t[-1]
        part = {"pliegues": [(t1 * 0.2, t1 * 0.5), (t1 * 0.5, t1 * 0.8)], "holdout": (t1 * 0.85, t1)}
        r = lab.carry_realista([m], spot, (0.00045, 0.00015), part, 0, {"BTC": 0})
        x = r["fuera_de_muestra"]
        self.assertGreater(x["n"], 3)
        self.assertGreater(x["funding_medio_pct"], 0)
        self.assertIn("rentab_anual_capital_pct", x)


if __name__ == "__main__":
    unittest.main()
