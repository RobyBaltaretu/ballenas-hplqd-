"""Regresión del bloque D: la simulación descuenta costes adversos y representa fills y comisiones según el tipo de
orden; precio de referencia de los stops (marca); liquidación y margen reales; impacto y exposición a BTC."""

import os
import unittest

from estrategia import SimSalida
from riesgo import exposicion_beta, impacto_pct
from tests.comunes import ConCarpeta, MercadoFalso, config, mover, nuevo_papel

TAKER, MAKER = 0.00045, 0.00015


class LibroFino(MercadoFalso):
    """Libro con varios niveles y poca profundidad (para ver impacto y fills parciales)."""

    def l2_snapshot(self, moneda):
        px = self.precios[moneda]
        bids = [{"px": str(px * (1 - 0.0001 * (k + 1))), "sz": "0.01"} for k in range(5)]
        asks = [{"px": str(px * (1 + 0.0001 * (k + 1))), "sz": "0.01"} for k in range(5)]
        return {"levels": [bids, asks]}


class Papel(ConCarpeta):
    def setUp(self):
        super().setUp()
        self.m = LibroFino()
        self.papel, self.info = nuevo_papel(self.dir, self.m)

    def ultimo_fill(self):
        return self.papel.d["fills"][-1]

    def test_ioc_recorre_el_libro_y_llena_parcial(self):
        r = self.papel.order("ETH", True, 0.08, 4000 * 1.01, {"limit": {"tif": "Ioc"}})
        hecho = float(r["response"]["data"]["statuses"][0]["filled"]["totalSz"])
        self.assertAlmostEqual(hecho, 0.05)                         # solo hay 5 niveles de 0,01
        medio = float(r["response"]["data"]["statuses"][0]["filled"]["avgPx"])
        self.assertGreater(medio, 4000 * 1.0001)                    # peor que el mejor precio: impacto
        self.assertTrue(self.ultimo_fill()["crossed"])              # taker
        self.assertAlmostEqual(float(self.ultimo_fill()["fee"]), medio * hecho * TAKER, places=6)

    def test_ioc_no_pasa_del_limite(self):
        r = self.papel.order("ETH", True, 0.05, 4000 * 1.00025, {"limit": {"tif": "Ioc"}})
        self.assertAlmostEqual(float(r["response"]["data"]["statuses"][0]["filled"]["totalSz"]), 0.02)

    def test_limite_se_llena_solo_si_se_atraviesa_y_paga_maker(self):
        self.papel.order("ETH", True, 0.01, 3990.0, {"limit": {"tif": "Alo"}})
        mover(self.papel, self.m, "ETH", 3990.0)                   # solo la toca
        self.papel.procesar()
        self.assertNotIn("ETH", self.papel.d["posiciones"])
        mover(self.papel, self.m, "ETH", 3989.0)                   # la atraviesa
        self.papel.procesar()
        self.assertIn("ETH", self.papel.d["posiciones"])
        self.assertFalse(self.ultimo_fill()["crossed"])            # maker
        self.assertAlmostEqual(float(self.ultimo_fill()["fee"]), 3990.0 * 0.01 * MAKER, places=6)

    def test_stop_se_activa_con_la_marca_no_con_el_mid(self):
        self.papel.order("ETH", True, 0.01, 4100.0, {"limit": {"tif": "Ioc"}})
        self.papel.order("ETH", False, 0.01, 3500.0, {"trigger": {"triggerPx": 3900.0, "isMarket": True, "tpsl": "sl"}},
                         reduce_only=True)
        mover(self.papel, self.m, "ETH", 3890.0, marca=3950.0)     # el mid cruza el stop, la marca no
        self.papel.procesar()
        self.assertIn("ETH", self.papel.d["posiciones"])
        mover(self.papel, self.m, "ETH", 3905.0, marca=3895.0)     # la marca cruza aunque el mid no
        self.papel.procesar()
        self.assertNotIn("ETH", self.papel.d["posiciones"])

    def test_stop_se_ejecuta_peor_que_el_trigger_contra_el_libro(self):
        self.papel.order("ETH", True, 0.03, 4100.0, {"limit": {"tif": "Ioc"}})
        self.papel.order("ETH", False, 0.03, 3500.0, {"trigger": {"triggerPx": 3900.0, "isMarket": True, "tpsl": "sl"}},
                         reduce_only=True)
        mover(self.papel, self.m, "ETH", 3880.0)                   # hueco: el precio ya está por debajo del stop
        self.papel.procesar()
        f = self.ultimo_fill()
        self.assertLess(float(f["px"]), 3880.0)                    # se vende contra los bids, peor que el mid
        self.assertLess(float(f["px"]), 3900.0)                    # y peor que el trigger
        self.assertTrue(f["crossed"])

    def test_liquidacion_estimada(self):
        self.papel.update_leverage(10, "ETH")
        self.papel.order("ETH", True, 0.01, 4100.0, {"limit": {"tif": "Ioc"}})
        pos = self.papel.user_state()["assetPositions"][0]["position"]
        liq = float(pos["liquidationPx"])
        self.assertTrue(pos["liquidacion_estimada"])
        self.assertLess(liq, 4000.0)
        mover(self.papel, self.m, "ETH", liq * 0.999)
        self.papel.procesar()
        self.assertNotIn("ETH", self.papel.d["posiciones"])
        self.assertTrue(self.papel.d["liquidaciones"])

    def test_funding_con_precio_del_oraculo(self):
        self.papel.order("ETH", True, 0.01, 4100.0, {"limit": {"tif": "Ioc"}})
        self.m.tasas["ETH"] = 0.0001
        self.m.marcas["ETH"] = 5000.0                               # marca distinta: el funding usa el oráculo
        self.papel.d["ultima_hora_funding"] -= 1
        self.papel._funding()
        pago = float(self.papel.d["funding"][-1]["delta"]["usdc"])
        self.assertAlmostEqual(pago, -0.01 * 4000.0 * 0.0001, places=8)


class Laboratorio(unittest.TestCase):
    def test_stop_simulado_sale_peor_que_su_precio(self):
        cfg = dict(config()["salida"], parcial_activo=False)
        sin = SimSalida("L", 100.0, 2.0, cfg, 10**12, TAKER, TAKER, MAKER)
        con = SimSalida("L", 100.0, 2.0, cfg, 10**12, TAKER, TAKER, MAKER, desl_mercado=0.001)
        for s in (sin, con):
            s.paso(100.5, 97.0, 97.5, 1)
        self.assertLess(con.resultado_r(), sin.resultado_r())
        self.assertLess(sin.resultado_r(), -1.0)                   # incluso sin deslizamiento, comisiones

    def test_escenarios_incluyen_funding_y_son_peores(self):
        import validacion as vd
        tr = [{"t": i, "r": 0.5, "sl_pct": 1.0, "salida": "stop-loss" if i % 2 else "objetivo final",
               "funding_r": 0.05, "deslizamiento_pct": 0.03} for i in range(40)]
        esc = vd.escenarios_estres(tr, 0.045, 0.02)
        base = sum(t["r"] for t in tr) / len(tr)
        for k, v in esc.items():
            m = sum(t["r"] for t in v["trades"]) / len(v["trades"])
            self.assertLess(m, base, k)
        self.assertIn("funding_x3", esc)


class Riesgo(unittest.TestCase):
    def test_impacto_y_libro_insuficiente(self):
        niveles = [(100.0, 1.0), (100.1, 1.0), (100.5, 1.0)]
        self.assertAlmostEqual(impacto_pct(niveles, 50.0), 0.0)
        self.assertGreater(impacto_pct(niveles, 250.0), 0.05)
        self.assertIsNone(impacto_pct(niveles, 10_000.0))

    def test_exposicion_beta_incluye_externas_y_lados(self):
        pos = [{"moneda": "ETH", "lado": "L", "notional": 100}, {"moneda": "SOL", "lado": "S", "notional": 100},
               {"moneda": "RARA", "lado": "L", "notional": 100}]
        total, sin = exposicion_beta(pos, {"ETH": 1.2, "SOL": 1.5}, 1.5)
        self.assertAlmostEqual(total, 120 - 150 + 150)
        self.assertEqual(sin, ["RARA"])

    def test_agregados_bloquean_por_beta(self):
        from riesgo import comprobar_agregados
        c = config(riesgo__exposicion_beta_btc_max_pct=100)
        nueva = {"moneda": "ETH", "lado": "L", "notional": 60, "riesgo_usd": 1}
        ext = [{"moneda": "SOL", "lado": "L", "notional": 40}]
        motivos = comprobar_agregados(nueva, [], ext, 100, c, betas={"ETH": 1.0, "SOL": 1.5})
        self.assertTrue(any("BTC" in m for m in motivos))


class LiquidacionReal(ConCarpeta):
    def gestor(self, cuenta):
        from ejecucion import Ejecutor
        from gestor import Gestor
        from ledger import Ledger
        from tests.comunes import LOG
        m = MercadoFalso()
        papel, info = nuevo_papel(self.dir, m)
        c = config(ordenes__entrada_limite=False)
        ledger = Ledger(os.path.join(self.dir, "registro"))
        self.alertas = []
        g = Gestor(c, Ejecutor(papel, info, "simulado", ledger, "simulado", LOG, c["ordenes"], esperar=lambda s: None),
                   info, "simulado", ledger, {"posiciones": {}}, {a["name"]: a for a in m.meta()["universe"]}, LOG,
                   "real", lambda t, s="info", tid=None: self.alertas.append((s, t)), lambda: None, None,
                   esperar=lambda s: None)
        g.cerrar = lambda op, causa: self.alertas.append(("cerrar", causa)) or True
        return g

    def test_liquidacion_antes_que_el_stop_cierra_y_bloquea(self):
        g = self.gestor(None)
        g.ops["ETH"] = {"trade_id": "t", "moneda": "ETH", "lado": "L", "estado": "abierta", "sl": 3800.0,
                        "estrategia": "x", "version": "1"}
        cuenta = {"posiciones": {"ETH": {"tam": 1.0, "notional": 4000.0, "liq": 3850.0, "lado": "L"}},
                  "equity": 1000, "mantenimiento": 10, "margen_usado": 100, "ts": 0}
        g.vigilar_liquidacion(cuenta)
        self.assertIn("ETH", g.riesgo_liquidacion)
        self.assertIn(("cerrar", "liquidación más cerca que el stop"), self.alertas)

    def test_liquidacion_desconocida_en_real_bloquea(self):
        g = self.gestor(None)
        cuenta = {"posiciones": {"SOL": {"tam": 1.0, "notional": 200.0, "liq": None, "lado": "S"}},
                  "equity": 1000, "mantenimiento": None, "margen_usado": None, "ts": 0}
        g.vigilar_liquidacion(cuenta)
        self.assertIn("SOL", g.riesgo_liquidacion)

    def test_margen_de_mantenimiento_alto_bloquea(self):
        g = self.gestor(None)
        g.vigilar_liquidacion({"posiciones": {}, "equity": 100, "mantenimiento": 80, "margen_usado": 90, "ts": 0})
        self.assertIn("_cuenta", g.riesgo_liquidacion)
