import unittest

from senales import MODULOS, Contexto, barras_max, detectar, usa_intervalo

H4 = 4 * 3600 * 1000


def velas(n, paso):
    out, px = [], 100.0
    for i in range(n):
        px *= 1 + paso + (0.002 if i % 2 else -0.002)
        out.append({"t": i * H4, "o": str(px), "h": str(px * 1.004), "l": str(px * 0.996), "c": str(px), "v": "1"})
    return out


class Momentum(unittest.TestCase):
    P = {"lookback": 63, "rebalanceo": 21, "umbral_atr": 3.0, "eficiencia_min": 0.1, "sl_atr": 3.0}

    def senal_en_rebalanceo(self, ctx):
        i = max(k for k in range(len(ctx)) if int(ctx.t[k] // 14400) % 21 == 0)
        return detectar("momentum", ctx, i, self.P)

    def test_tendencia_clara_da_senal_a_favor(self):
        self.assertEqual(self.senal_en_rebalanceo(Contexto(velas(300, 0.004)))["lado"], "L")
        self.assertEqual(self.senal_en_rebalanceo(Contexto(velas(300, -0.004)))["lado"], "S")

    def test_lateral_no_da_senal(self):
        self.assertIsNone(self.senal_en_rebalanceo(Contexto(velas(300, 0.0))))

    def test_solo_fuera_del_rebalanceo_no_hay_senal(self):
        ctx = Contexto(velas(300, 0.004))
        i = max(k for k in range(len(ctx)) if int(ctx.t[k] // 14400) % 21 == 5)
        self.assertIsNone(detectar("momentum", ctx, i, self.P))

    def test_solo_4h_y_dura_hasta_el_rebalanceo(self):
        self.assertTrue(usa_intervalo("momentum", "4h"))
        self.assertFalse(usa_intervalo("momentum", "1h"))
        self.assertEqual(barras_max("momentum", self.P), 20)
        self.assertTrue(MODULOS["momentum"]["salida"]["sin_objetivo"])
