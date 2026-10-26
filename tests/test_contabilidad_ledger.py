import json
import os
import unittest

from contabilidad import atribuir_funding, resultado
from ledger import ErrorLedger, Ledger
from tests.comunes import ConCarpeta


def pago(t, coin, usdc, szi, tipo="funding"):
    return {"time": t, "delta": {"type": tipo, "coin": coin, "usdc": str(usdc), "szi": str(szi), "fundingRate": "0.0001"}}


class Funding(unittest.TestCase):
    IV = [{"trade_id": "a", "moneda": "ETH", "lado": "L", "desde_ms": 1000, "hasta_ms": 5000, "tam": 1.0}]

    def test_atribuye_solo_si_cuadra(self):
        por, sin = atribuir_funding([pago(2000, "ETH", -0.1, 1.0), pago(3000, "ETH", -0.2, 2.0),
                                     pago(6000, "ETH", -0.1, 1.0), pago(2500, "BTC", 0.3, 0.1)], self.IV)
        self.assertEqual([p["usd"] for p in por["a"]], [-0.1])
        self.assertEqual(len(sin), 3)       # tamaño distinto, fuera de intervalo, otra moneda

    def test_depositos_y_transferencias_no_son_funding(self):
        por, sin = atribuir_funding([pago(2000, "ETH", 500, 1.0, tipo="deposit"),
                                     pago(2100, "ETH", -50, 1.0, tipo="accountClassTransfer")], self.IV)
        self.assertEqual(por, {})
        self.assertEqual(sin, [])

    def test_lado_contrario_no_se_atribuye(self):
        por, sin = atribuir_funding([pago(2000, "ETH", 0.1, -1.0)], self.IV)
        self.assertEqual(por, {})
        self.assertEqual(len(sin), 1)


class Resultado(unittest.TestCase):
    def test_neto_y_r(self):
        ent = [{"px": "100", "sz": "1", "fee": "0.05"}]
        sal = [{"px": "110", "sz": "1", "fee": "0.05", "closedPnl": "10"}]
        r = resultado(ent, sal, [{"usd": -0.4}], "L", 95.0, 99.0)
        self.assertAlmostEqual(r["pnl_bruto"], 10)
        self.assertAlmostEqual(r["comisiones"], 0.1)
        self.assertAlmostEqual(r["pnl_neto"], 10 - 0.1 - 0.4)
        self.assertAlmostEqual(r["riesgo_inicial_usd"], 5)
        self.assertAlmostEqual(r["r_bruto"], 2.0)
        self.assertAlmostEqual(r["r_neto"], (10 - 0.5) / 5)
        self.assertAlmostEqual(r["deslizamiento_entrada_pct"], (100 - 99) / 99 * 100)

    def test_sin_fills_de_salida_es_desconocido(self):
        r = resultado([{"px": "100", "sz": "1", "fee": "0"}], [], [], "L", 95.0)
        self.assertIsNone(r["pnl_neto"])
        self.assertIsNone(r["r_neto"])


class LedgerPruebas(ConCarpeta):
    def test_escribe_lee_y_reconstruye(self):
        lg = Ledger(os.path.join(self.dir, "r"))
        lg.registrar("senal", "t1", "simulado", moneda="ETH", lado="L", estrategia="x", precio=100.0)
        lg.registrar("decision", "t1", "simulado", aprobada=True, motivos=[])
        lg.registrar("fill", "t1", "simulado", rol="entrada", px=100.0, sz=1.0, comision=0.05)
        lg.registrar("proteccion", "t1", "simulado", clase="stop", accion="verificado", trigger=95.0)
        lg.registrar("fill", "t1", "simulado", rol="salida", px=110.0, sz=1.0, comision=0.05, pnl_cerrado=10.0)
        lg.registrar("funding", "t1", "simulado", usd=-0.4)
        lg.registrar("cierre", "t1", "simulado", causa="objetivo")
        ops, problemas = lg.operaciones()
        o = ops["t1"]
        self.assertEqual(problemas, [])
        self.assertEqual(o["estado"], "cerrada")
        self.assertAlmostEqual(o["pnl_neto"], 10 - 0.1 - 0.4)
        self.assertAlmostEqual(o["r_neto"], (10 - 0.5) / 5)

    def test_linea_corrupta_no_rompe(self):
        lg = Ledger(os.path.join(self.dir, "r"))
        lg.registrar("senal", "t1", "simulado", moneda="ETH")
        with open(lg.ruta, "a", encoding="utf-8") as fh:
            fh.write('{"tipo": "fill", "trade_id": "t1", ')     # escritura cortada (corte de luz)
            fh.write("\n")
        lg.registrar("cierre", "t1", "simulado", causa="x")
        eventos, problemas = lg.leer()
        self.assertEqual(len(eventos), 2)
        self.assertEqual(len(problemas), 1)

    def test_rotacion_conserva_todo(self):
        lg = Ledger(os.path.join(self.dir, "r"), max_bytes=400)
        for i in range(30):
            lg.registrar("senal", f"t{i}", "simulado", moneda="ETH", motivo="x" * 20)
        self.assertGreater(len(lg.archivos()), 1)
        self.assertEqual(len(lg.leer()[0]), 30)

    def test_tipo_y_campos_reservados(self):
        lg = Ledger(os.path.join(self.dir, "r"))
        with self.assertRaises(ErrorLedger):
            lg.registrar("inventado", "t1", "simulado")
        with self.assertRaises(ErrorLedger):
            lg.registrar("senal", "t1", "marciano")

    def test_version_y_utc(self):
        lg = Ledger(os.path.join(self.dir, "r"))
        ev = lg.registrar("senal", "t1", "real", moneda="ETH")
        self.assertEqual(ev["v"], 1)
        self.assertTrue(ev["ts"].endswith("Z"))
        with open(lg.ruta, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["origen"], "real")
