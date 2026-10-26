"""La simulación repartida entre núcleos da EXACTAMENTE lo mismo que en serie."""

import math
import unittest

from tests import comunes

import laboratorio as lab


def mercado(nombre, fase):
    H = 3600_000 * 4
    velas = []
    for k in range(900):
        p = 100 * (1 + 0.2 * math.sin(k / 37 + fase) + 0.05 * math.sin(k / 5))
        velas.append({"t": k * H, "o": p, "h": p * 1.01, "l": p * 0.99, "c": p * (1.002 if k % 3 else 0.998), "v": 10})
    return lab.Mercado(nombre, "4h", velas, None)


class TestParalelo(unittest.TestCase):
    def test_igual_que_en_serie(self):
        c = comunes.config()
        lab.configurar_globales(c)
        ms = [mercado("AAA", 0.0), mercado("BBB", 1.3)]
        fees = (0.00045, 0.00015)
        nombres = ["ruptura", "retroceso", "reversion"]
        serie = lab.simular_todos(nombres, ms, c, fees, True, 0, None, procesos=1)
        paralelo = lab.simular_todos(nombres, ms, c, fees, True, 0, None, procesos=2)
        self.assertEqual(serie, paralelo)
        self.assertTrue(any(tr for _, combos in serie.values() for _, tr in combos))


if __name__ == "__main__":
    unittest.main()
