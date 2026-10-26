"""Contexto de Binance/Bybit en vivo: solo lectura, nunca frena ni rompe el bot. Sin red: lectores falsos."""

import os
import shutil
import tempfile
import unittest

from tests import comunes  # noqa: F401

import contexto_externo as cx
import historia_externa as hx


class LectorFalso:
    def __init__(self, nombre, datos=None, falla=False):
        self.nombre, self.datos, self.falla = nombre, datos or {}, falla
        self.pedidos = None

    def leer(self, simbolos):
        self.pedidos = simbolos
        if self.falla:
            raise ConnectionError("bloqueado en tu país")
        return {s: dict(v) for s, v in self.datos.items() if s in simbolos}


class TestContexto(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_calcula_diferencias_y_archiva(self):
        b = LectorFalso("binance", {"BTCUSDT": {"precio": 100.1, "funding_h_pct": 0.00125, "vol24h_usd": 9e9, "oi_usd": 5e9},
                                    "LITUSDT": {"precio": 3.0, "funding_h_pct": 0.001, "vol24h_usd": 1e6, "oi_usd": 1e6}})
        y = LectorFalso("bybit", {"BTCUSDT": {"precio": 99.9, "funding_h_pct": 0.00075, "vol24h_usd": 1e9, "oi_usd": 1e9}})
        c = cx.ContextoExterno({}, self.dir, lectores=[b, y])
        c.actualizar(["BTC", "LIT"], {"BTC": 100.0, "LIT": 1.5}, {"BTC": 0.0015}, {"BTC": 1e9}, ahora=1000.0,
                     en_segundo_plano=False)
        r = c.resumen()
        self.assertEqual([x["m"] for x in r["monedas"]], ["BTC"])           # LIT: otro token (precio ×2) -> fuera
        btc = r["monedas"][0]
        self.assertAlmostEqual(btc["funding_externo_h_pct"], 0.001)
        self.assertAlmostEqual(btc["dif_funding_h_pct"], 0.0005)
        self.assertEqual(btc["oi_externo_usd"], 6e9)
        self.assertAlmostEqual(btc["cuota_hl_volumen_pct"], 9.09, places=2)
        self.assertAlmostEqual(btc["fuentes"]["binance"]["dif_precio_pct"], 0.1)
        self.assertIn("LIT", r["no_coinciden"])
        self.assertEqual(r["fuentes"]["binance"]["estado"], "correcto")
        self.assertEqual(r["dias_archivados"], 1)
        self.assertEqual(len(cx.leer_archivo(self.dir)), 1)

    def test_cambio_oi_en_una_hora_y_ritmo(self):
        datos = {"BTCUSDT": {"precio": 100.0, "funding_h_pct": 0.001, "vol24h_usd": 1e9, "oi_usd": 1e9}}
        b = LectorFalso("binance", datos)
        c = cx.ContextoExterno({"cada_min": 5}, self.dir, lectores=[b])
        self.assertTrue(c.actualizar(["BTC"], {"BTC": 100.0}, {}, ahora=0.0, en_segundo_plano=False))
        self.assertFalse(c.actualizar(["BTC"], {"BTC": 100.0}, {}, ahora=60.0, en_segundo_plano=False))  # aún no toca
        datos["BTCUSDT"]["oi_usd"] = 1.1e9
        c.actualizar(["BTC"], {"BTC": 100.0}, {}, ahora=3600.0, en_segundo_plano=False)
        self.assertAlmostEqual(c.resumen()["monedas"][0]["oi_externo_cambio_1h_pct"], 10.0)

    def test_una_fuente_caida_no_rompe_nada(self):
        b = LectorFalso("binance", falla=True)
        y = LectorFalso("bybit", {"ETHUSDT": {"precio": 10.0, "funding_h_pct": 0.0, "vol24h_usd": 1.0, "oi_usd": None}})
        c = cx.ContextoExterno({}, self.dir, lectores=[b, y])
        c.actualizar(["ETH"], {"ETH": 10.0}, {}, ahora=1.0, en_segundo_plano=False)
        r = c.resumen()
        self.assertEqual(r["fuentes"]["binance"]["estado"], "fallido")
        self.assertEqual(r["monedas"][0]["m"], "ETH")
        self.assertIsNone(r["monedas"][0]["oi_externo_usd"])

    def test_desactivado(self):
        b = LectorFalso("binance")
        c = cx.ContextoExterno({"activo": False}, self.dir, lectores=[b])
        self.assertFalse(c.actualizar(["BTC"], {"BTC": 1.0}, {}, ahora=1.0, en_segundo_plano=False))
        self.assertIsNone(b.pedidos)
        self.assertFalse(os.listdir(self.dir))

    def test_lectores_http_binance_y_bybit(self):
        def get(url, p):
            if url.endswith("/premiumIndex"):
                return [{"symbol": "BTCUSDT", "markPrice": "100", "lastFundingRate": "0.0004"}]
            if url.endswith("/ticker/24hr"):
                return [{"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "5000"}]
            if url.endswith("/fundingInfo"):
                return [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}]
            if url.endswith("/openInterest"):
                return {"openInterest": "3"}
            if url.endswith("/instruments-info"):
                return {"result": {"list": [{"symbol": "BTCUSDT", "fundingInterval": 480}]}}
            if url.endswith("/tickers"):
                return {"result": {"list": [{"symbol": "BTCUSDT", "markPrice": "101", "fundingRate": "0.0008",
                                             "turnover24h": "7", "openInterestValue": "9"}]}}
            raise AssertionError(url)
        cli = hx.Cliente(get=get, dormir=lambda s: None)
        b = cx.LectorBinance(cli).leer(["BTCUSDT", "XUSDT"])
        self.assertEqual(b, {"BTCUSDT": {"precio": 100.0, "funding_h_pct": 0.01, "vol24h_usd": 5000.0, "oi_usd": 300.0}})
        y = cx.LectorBybit(cli).leer(["BTCUSDT"])
        self.assertEqual(y["BTCUSDT"]["funding_h_pct"], 0.01)
        self.assertEqual(y["BTCUSDT"]["oi_usd"], 9.0)


if __name__ == "__main__":
    unittest.main()


class TestSoloVelasCerradas(unittest.TestCase):
    def test_la_vela_en_curso_no_se_usa(self):
        import threading
        ce = cx.ContextoExterno.__new__(cx.ContextoExterno)
        ce.bloqueo = threading.Lock()
        H = 3600_000
        ahora = 10 * 3600 + 1800                                  # 10:30
        ce.recientes = {"ETH": {"ts": ahora, "1h": [{"t": k * H, "c": 1.0, "v": 1.0, "tb": 0.5} for k in range(8, 11)],
                                "funding": [{"time": 9 * H, "fundingRate": 0.0001}, {"time": 11 * H, "fundingRate": 0.0001}]}}
        velas, fund = ce.series_cerebro("ETH", "1h", ahora=ahora)
        self.assertEqual([v["t"] for v in velas], [8 * H, 9 * H])  # la de las 10:00 sigue abierta
