import unittest
from datetime import datetime, timezone

from riesgo import (actualizar_ventanas, bloqueos_duros, comprobar_agregados, dimensionar, distancia_liquidacion_pct,
                    limites_efectivos, suelo_emergencia)
from tests.comunes import config


class Dimensionar(unittest.TestCase):
    def setUp(self):
        self.c = config()

    def test_riesgo_con_costes_no_supera_lo_permitido(self):
        d = dimensionar(1000, 1000, 100.0, 2.0, 3, 20, self.c, spread_pct=0.02)
        self.assertTrue(d["ok"], d)
        permitido = 1000 * self.c["riesgo"]["riesgo_por_operacion_pct"] / 100
        self.assertLessEqual(d["riesgo_usd"], permitido + 1e-9)
        self.assertGreater(d["costes_estimados_usd"], 0)     # incluye comisiones ida y vuelta + spread + desliz.

    def test_tamano_minimo_excede_riesgo(self):
        d = dimensionar(20, 20, 100_000.0, 5.0, 5, 40, self.c)
        self.assertFalse(d["ok"])
        self.assertIn("tamaño mínimo excede riesgo", d["motivo"])

    def test_redondeo_hacia_abajo(self):
        d = dimensionar(1000, 1000, 3.0, 1.0, 0, 20, self.c)
        self.assertTrue(d["ok"])
        self.assertEqual(d["tam"], int(d["tam"]))

    def test_factor_solo_reduce(self):
        a = dimensionar(1000, 1000, 100.0, 2.0, 3, 20, self.c, factor=1.0)
        b = dimensionar(1000, 1000, 100.0, 2.0, 3, 20, self.c, factor=5.0)
        c = dimensionar(1000, 1000, 100.0, 2.0, 3, 20, self.c, factor=0.5)
        self.assertEqual(a["tam"], b["tam"])
        self.assertLess(c["tam"], a["tam"])

    def test_stop_fuera_de_limites(self):
        self.assertFalse(dimensionar(1000, 1000, 100.0, 50.0, 3, 20, self.c)["ok"])

    def test_liquidacion_lejos_del_stop(self):
        d = dimensionar(1000, 1000, 100.0, 2.0, 3, 20, self.c)
        self.assertGreaterEqual(d["distancia_liquidacion_pct"],
                                2.0 * self.c["riesgo"]["distancia_liquidacion_min_multiplo_stop"])
        self.assertGreater(distancia_liquidacion_pct(2, 20), distancia_liquidacion_pct(10, 20))

    def test_datos_desconocidos(self):
        self.assertFalse(dimensionar(None, 0, 100.0, 2.0, 3, 20, self.c)["ok"])


class Agregados(unittest.TestCase):
    def setUp(self):
        self.c = config()

    def test_max_posiciones(self):
        n = self.c["riesgo"]["max_posiciones_abiertas"]
        abiertas = [{"moneda": f"M{i}", "lado": "L" if i % 2 else "S", "notional": 10, "riesgo_usd": 0.1} for i in range(n)]
        m = comprobar_agregados({"moneda": "X", "lado": "L", "notional": 10, "riesgo_usd": 0.1}, abiertas, [], 1000, self.c)
        self.assertTrue(any("máximo de posiciones" in x for x in m))

    def test_externas_cuentan_como_exposicion(self):
        externas = [{"moneda": "BTC", "lado": "L", "notional": 1500}]
        m = comprobar_agregados({"moneda": "BTC", "lado": "L", "notional": 100, "riesgo_usd": 1}, [], externas, 1000, self.c)
        self.assertTrue(any("exposición" in x for x in m))

    def test_riesgo_abierto(self):
        abiertas = [{"moneda": "A", "lado": "L", "notional": 10, "riesgo_usd": 39}]
        m = comprobar_agregados({"moneda": "B", "lado": "S", "notional": 10, "riesgo_usd": 5}, abiertas, [], 1000, self.c)
        self.assertTrue(any("riesgo abierto" in x for x in m))


class Limites(unittest.TestCase):
    def test_ventanas_utc_y_bloqueo_diario(self):
        c = config()
        e = {}
        lunes = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()     # lunes 00:00 UTC
        actualizar_ventanas(e, 1000, lunes + 3600)
        self.assertEqual(e["dia"], "2026-10-19")
        self.assertEqual(bloqueos_duros(e, 1000, c, ahora=lunes + 3600), [])
        caida = 1000 * (1 - c["riesgo"]["perdida_diaria_max_pct"] / 100)
        self.assertTrue(any("pérdida del día" in x for x in bloqueos_duros(e, caida, c, ahora=lunes + 7200)))
        self.assertTrue(actualizar_ventanas(e, caida, lunes + 86400 + 60))      # nuevo día UTC
        self.assertFalse(any("pérdida del día" in x for x in bloqueos_duros(e, caida, c, ahora=lunes + 86400 + 60)))

    def test_ops_por_dia(self):
        c = config()
        e = {"ops_hoy": c["riesgo"]["max_operaciones_dia"]}
        self.assertTrue(any("operaciones del día" in x for x in bloqueos_duros(e, 1000, c)))

    def test_suelo_emergencia(self):
        c = config()
        self.assertTrue(suelo_emergencia({"saldo_inicial": 100}, 100 * (1 - c["riesgo"]["perdida_max_total_pct"] / 100), c))
        self.assertFalse(suelo_emergencia({"saldo_inicial": 100}, 90, c))

    def test_piloto_real_mas_prudente(self):
        c = config()
        c["modo"] = "real"
        lim = limites_efectivos(c)
        self.assertLessEqual(lim["max_posiciones_abiertas"], c["piloto_real"]["max_posiciones_abiertas"])
        self.assertLessEqual(lim["riesgo_por_operacion_pct"], c["piloto_real"]["riesgo_por_operacion_pct"])


class FiltroCostes(unittest.TestCase):
    def test_stop_corto_se_descarta(self):
        from riesgo import coste_esperado_pct, costes_excesivos
        c = config()
        coste = coste_esperado_pct(c)
        self.assertIsNotNone(costes_excesivos(c, coste / 0.25))          # costes = 25 % del riesgo
        self.assertIsNone(costes_excesivos(c, coste / 0.10))             # costes = 10 % del riesgo
        c["riesgo"]["coste_max_fraccion_riesgo"] = 0
        self.assertIsNone(costes_excesivos(c, 0.01))                     # 0 = desactivado

    def test_entrada_limite_abarata(self):
        from riesgo import coste_esperado_pct
        c = config()
        con_limite = coste_esperado_pct(c)
        c["ordenes"]["entrada_limite"] = False
        self.assertGreater(coste_esperado_pct(c), con_limite)


class TestTopePiloto(unittest.TestCase):
    def test_perdida_max_usd_del_piloto_cierra_en_real(self):
        from riesgo import suelo_emergencia
        cfg = {"modo": "real", "riesgo": {"perdida_max_total_pct": 50}, "piloto_real": {"perdida_max_usd": 10}}
        e = {"saldo_inicial": 232.0}
        self.assertFalse(suelo_emergencia(e, 223.0, cfg))
        self.assertTrue(suelo_emergencia(e, 222.0, cfg))          # 10 USD de pérdida (incluye lo no realizado)
        self.assertFalse(suelo_emergencia(e, 222.0, dict(cfg, modo="simulado")))


class TestFundingEnTamano(unittest.TestCase):
    def test_funding_adverso_reduce_el_tamano(self):
        from riesgo import funding_adverso_frac
        from tests.comunes import config
        from riesgo import dimensionar
        c = config()
        f = funding_adverso_frac(0.01, 1800)                      # 0,01 %/h en contra durante 30 h, estrés ×2
        self.assertAlmostEqual(f, 0.006)
        self.assertGreater(funding_adverso_frac(None, 600), 0)     # nunca cero: mínimo básico de Hyperliquid
        a = dimensionar(1000, 1000, 100, 2.0, 2, 10, c)
        b = dimensionar(1000, 1000, 100, 2.0, 2, 10, c, funding_frac=f)
        self.assertLess(b["notional"], a["notional"])
