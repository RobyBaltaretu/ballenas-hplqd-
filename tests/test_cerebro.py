"""Cerebro (un modelo con todos los datos) y vista de mercado unificada. Sin red."""

import random
import unittest

from tests import comunes  # noqa: F401

import cerebro as cb
import mercado_unificado as mu
from senales import Contexto


def estad(ts):
    rs = [t["r"] for t in ts]
    return {"n": len(rs), "r_medio": round(sum(rs) / len(rs), 4) if rs else None}


def candidatas(n=1200, seed=3):
    """El flujo externo anticipa el resultado; la tendencia no. Un buen modelo debe usar el flujo."""
    rng = random.Random(seed)
    out = []
    for k in range(n):
        flujo = rng.uniform(-1, 1)
        x = {r: None for r in mu.RASGOS}
        x.update(tendencia=rng.gauss(0, 1), flujo1=flujo, volatilidad=rng.uniform(0.5, 2))
        r = 0.8 * flujo - 0.05 + rng.gauss(0, 0.6)
        out.append({"t": k * 3600, "cierre_ts": k * 3600 + 7200, "r": r, "estrategia": "e" + str(k % 3), "rasgos": x})
    return out


class TestCerebro(unittest.TestCase):
    def test_aprende_el_rasgo_que_aporta_sin_mirar_el_futuro(self):
        cand = candidatas()
        part = {"pliegues": [(400 * 3600, 600 * 3600), (600 * 3600, 800 * 3600), (800 * 3600, 1000 * 3600)],
                "holdout": (1000 * 3600, 1200 * 3600)}
        comp = cb.comparar(cand, part, 3600, estad)
        todas = comp["todas_las_candidatas"]["fuera_de_muestra"]["r_medio"]
        con = comp["cerebro_con_todo"]["fuera_de_muestra"]["r_medio"]
        solo_hl = comp["cerebro_solo_hyperliquid"]["fuera_de_muestra"]
        self.assertGreater(con, todas + 0.2)
        self.assertGreater(comp["cerebro_con_todo"]["pesos"]["flujo1"], 0.2)
        self.assertLess((solo_hl["r_medio"] or 0), con)
        # Cada elegida se decidió con un modelo entrenado solo con operaciones cerradas antes de su tramo.
        for tr in comp["cerebro_con_todo"]["tramos"]:
            self.assertLessEqual(tr["entreno"], sum(1 for c in cand if c["cierre_ts"] < tr["desde"] - 3600))

    def test_sin_historia_no_decide(self):
        sel, tramos = cb.walk_forward(candidatas(30), [(0, 10 ** 9)], 0)
        self.assertEqual(sel, [])
        self.assertIn("sin historia", tramos[0]["nota"])

    def test_dato_ausente_es_neutro(self):
        m = cb.Modelo(["a"], []).entrenar([({"a": float(i % 7)}, None, float(i % 7) * 0.1) for i in range(100)])
        self.assertTrue(abs(m.predecir({"a": None}, None) - m.predecir({"a": 3.0}, None)) < 0.15)


class TestRasgos(unittest.TestCase):
    def test_rasgos_con_signo_y_sin_futuro(self):
        H = 3600_000
        velas = [{"t": k * H, "o": 100 + k, "h": 101 + k, "l": 99 + k, "c": 100.5 + k, "v": 10} for k in range(120)]
        ctx = Contexto(velas)
        ext = mu.SerieExterna([{"t": k * H, "c": 100.5 + k, "v": 10.0, "tb": 8.0} for k in range(120)])
        fh = mu.FundingHorario([{"time": k * H, "fundingRate": 0.0001} for k in range(120)])
        fe = mu.FundingHorario([{"time": k * H, "fundingRate": 0.00005} for k in range(120)])
        largo = mu.rasgos(ctx, 100, "L", fh, ext, fe)
        corto = mu.rasgos(ctx, 100, "S", fh, ext, fe)
        self.assertGreater(largo["tendencia"], 0)
        self.assertAlmostEqual(largo["flujo1"], 0.6)
        self.assertAlmostEqual(corto["flujo1"], -0.6)
        self.assertLess(largo["funding8h"], 0)            # el largo paga funding positivo
        self.assertLess(largo["dif_funding8h"], 0)        # y aquí es más caro que fuera
        self.assertAlmostEqual(largo["base_pct"], 0.0)
        # Cambiar velas FUTURAS no cambia los rasgos de la vela 100.
        velas2 = velas[:101] + [dict(v, c=1.0) for v in velas[101:]]
        self.assertEqual(mu.rasgos(Contexto(velas2), 100, "L", fh, ext, fe), largo)

    def test_sin_datos_externos_son_none(self):
        H = 3600_000
        ctx = Contexto([{"t": k * H, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1} for k in range(80)])
        r = mu.rasgos(ctx, 70, "L")
        self.assertTrue(all(r[k] is None for k in mu.RASGOS_EXT))


if __name__ == "__main__":
    unittest.main()
