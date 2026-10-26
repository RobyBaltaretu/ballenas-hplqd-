"""Historia ampliada (Binance/Bybit) para el laboratorio. Sin red: las fuentes son falsas o con 'get' inyectado."""

import os
import shutil
import tempfile
import unittest

from tests import comunes  # noqa: F401  (añade la carpeta del bot al path)

import historia_externa as hx
import validacion as vd

H = hx.HORA_MS
H4 = 4 * H


def vela(t, c, v=10.0):
    return {"t": t, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": v}


class FuenteFalsa:
    def __init__(self, nombre, simbolos, precio=lambda t: 100.0, vol=100.0, funding=None, falla=False):
        self.nombre, self._sims, self.precio, self.vol, self._f, self.falla = nombre, set(simbolos), precio, vol, funding, falla
        self.peticiones = 0

    def simbolos(self):
        if self.falla:
            raise ConnectionError("bloqueado")
        return self._sims

    def velas(self, simbolo, iv, desde, hasta):
        self.peticiones += 1
        paso = hx.MIN_IV[iv] * 60_000
        t0 = (desde + paso - 1) // paso * paso
        return [vela(t, self.precio(t), self.vol) for t in range(t0, hasta + 1, paso)]

    def funding(self, simbolo, desde, hasta):
        return [x for x in (self._f or []) if desde <= x["time"] <= hasta]


class TestUtilidades(unittest.TestCase):
    def test_simbolos(self):
        self.assertEqual(hx.simbolos_candidatos("BTC"), ["BTCUSDT"])
        self.assertEqual(hx.simbolos_candidatos("kPEPE"), ["1000PEPEUSDT", "KPEPEUSDT"])

    def test_solape_acepta_y_escala_volumen(self):
        hl = [vela(i * H4, 100.0, 5.0) for i in range(200)]
        ext = [vela(i * H4, 100.2, 50.0) for i in range(200)]
        ok, info = hx.comprobar_solape(hl, ext, 100, 1.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(info["factor_volumen"], 0.1)

    def test_solape_rechaza_otro_token_o_escala(self):
        hl = [vela(i * H4, 100.0) for i in range(200)]
        ok, info = hx.comprobar_solape(hl, [vela(i * H4, 0.1) for i in range(200)], 100, 1.0)   # p. ej. ×1000
        self.assertFalse(ok)
        self.assertIn("no coincide", info["motivo"])
        ok, info = hx.comprobar_solape(hl, [vela(i * H4, 100.0) for i in range(50)], 100, 1.0)
        self.assertFalse(ok)
        self.assertIn("en común", info["motivo"])

    def test_anteponer_solo_antes_de_hyperliquid(self):
        hl = [vela(t, 100.0, 1.0) for t in range(10 * H4, 20 * H4, H4)]
        ext = [vela(t, 999.0, 10.0) for t in range(0, 20 * H4, H4)]
        juntas, n = hx.anteponer(hl, ext, 0.5, "binance")
        self.assertEqual(n, 10)
        self.assertEqual(len(juntas), 20)
        self.assertTrue(all(v["c"] == 100.0 for v in juntas[10:]))          # donde hay Hyperliquid, manda
        self.assertTrue(all(v["fuente"] == "binance" and v["v"] == 5.0 for v in juntas[:10]))
        self.assertNotIn("fuente", juntas[10])

    def test_funding_horario_reparte_la_tasa(self):
        regs = [{"time": 8 * H, "fundingRate": 0.0008}, {"time": 16 * H, "fundingRate": 0.0004},
                {"time": 20 * H, "fundingRate": 0.0004}]                     # el último cada 4 h
        out = hx.funding_horario(regs)
        self.assertEqual(len(out), 20)
        self.assertAlmostEqual(sum(r["fundingRate"] for r in out), 0.0016)
        self.assertAlmostEqual(out[-1]["fundingRate"], 0.0001)
        self.assertEqual(len(hx.funding_horario(regs, hasta_ms=10 * H)), 9)   # horas 1..9


class TestFuentesHttp(unittest.TestCase):
    def test_binance_pagina_velas_y_funding(self):
        llamadas = []

        def get(url, p):
            llamadas.append((url, dict(p)))
            if url.endswith("/klines"):
                ini = p["startTime"]
                n = min(p["limit"], (p["endTime"] - ini) // H + 1)
                return [[ini + i * H, "1", "2", "0.5", "1.5", "10"] for i in range(n)]
            if url.endswith("/fundingRate"):
                ini = p["startTime"]
                n = min(1000, (p["endTime"] - ini) // (8 * H) + 1)
                return [{"fundingTime": ini + i * 8 * H, "fundingRate": "0.0001"} for i in range(n)]
            return {"symbols": [{"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT"}]}
        b = hx.Binance(hx.Cliente(get=get, dormir=lambda s: None))
        self.assertEqual(b.simbolos(), {"BTCUSDT"})
        velas = b.velas("BTCUSDT", "1h", 0, 3000 * H)
        self.assertEqual(len(velas), 3001)
        self.assertEqual([v["t"] for v in velas], sorted({v["t"] for v in velas}))
        self.assertEqual(len(b.funding("BTCUSDT", 0, 1500 * 8 * H)), 1501)

    def test_bybit_pagina_hacia_atras(self):
        def get(url, p):
            if url.endswith("/kline"):
                fin, ini = p["end"], p["start"]
                paso = int(p["interval"]) * 60_000
                ultimo = fin // paso * paso
                ts = [t for t in range(ultimo, ini - 1, -paso)][: p["limit"]]
                return {"result": {"list": [[str(t), "1", "2", "0.5", "1.5", "10", "15"] for t in ts]}}
            return {"result": {"list": []}}
        y = hx.Bybit(hx.Cliente(get=get, dormir=lambda s: None))
        velas = y.velas("BTCUSDT", "4h", 0, 2500 * H4)
        self.assertEqual(len(velas), 2501)
        self.assertEqual(velas[0]["t"], 0)
        self.assertEqual(velas[-1]["t"], 2500 * H4)

    def test_cliente_reintenta_tras_limite(self):
        estado = {"n": 0}
        esperas = []

        def get(url, p):
            estado["n"] += 1
            if estado["n"] < 3:
                raise hx.ErrorLimite(429)
            return {"ok": True}
        c = hx.Cliente(get=get, dormir=esperas.append)
        self.assertEqual(c.get("u", {}), {"ok": True})
        self.assertEqual(len(esperas), 2)


class TestAmpliador(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _hl(self, desde, hasta, paso, precio=100.0):
        return [vela(t, precio, 1.0) for t in range(desde, hasta, paso)]

    def test_usa_la_segunda_fuente_si_la_primera_no_coincide(self):
        fin = 3000 * H4
        hl4 = self._hl(2000 * H4, fin, H4)
        mala = FuenteFalsa("binance", {"LITUSDT"}, precio=lambda t: 3.0)          # otro token con el mismo ticker
        buena = FuenteFalsa("bybit", {"LITUSDT"}, precio=lambda t: 100.0, vol=10.0,
                            funding=[{"time": t, "fundingRate": 0.0008} for t in range(0, fin, 8 * H)])
        a = hx.Ampliador({"anos_4h": 1, "anos_1h": 1}, [mala, buena], fin, carpeta=self.dir, log=lambda *x: None)
        a.cfg["anos_4h"] = (fin - 1000 * H4) / (365.25 * 86400_000)                # pide desde la vela 1000
        juntas = a.velas("LIT", "4h", hl4)
        self.assertEqual(len(juntas), 2000)
        self.assertEqual(a.informe["LIT"]["fuente"], "bybit")
        self.assertEqual(a.informe["LIT"]["4h"]["velas_externas"], 1000)
        self.assertAlmostEqual(a.informe["LIT"]["4h"]["factor_volumen"], 0.1)
        self.assertTrue(any("binance" in r and "no coincide" in r for r in a.informe["LIT"]["rechazos"]))
        regs = a.funding("LIT", [{"time": 2000 * H4, "fundingRate": "0.0000125"}])
        self.assertGreater(len(regs), 1)
        self.assertTrue(all(r["time"] < 2000 * H4 for r in regs[:-1]))
        self.assertAlmostEqual(regs[0]["fundingRate"], 0.0001)
        # La segunda vez sale de la caché de disco: no vuelve a descargar.
        antes = buena.peticiones
        b = hx.Ampliador(a.cfg, [buena], fin, carpeta=self.dir)
        b.velas("LIT", "4h", hl4)
        self.assertEqual(buena.peticiones, antes)

    def test_sin_red_o_sin_simbolo_devuelve_hyperliquid_tal_cual(self):
        fin = 3000 * H4
        hl4 = self._hl(2000 * H4, fin, H4)
        a = hx.Ampliador({"anos_4h": 6}, [FuenteFalsa("binance", set(), falla=True),
                                          FuenteFalsa("bybit", {"OTRAUSDT"})], fin, carpeta=self.dir)
        self.assertEqual(a.velas("LIT", "4h", hl4), hl4)
        self.assertIsNone(a.informe["LIT"]["fuente"])
        self.assertTrue(any("no responde" in r for r in a.informe["LIT"]["rechazos"]))
        self.assertEqual(a.funding("LIT", [{"time": 1}]), [{"time": 1}])

    def test_hyperliquid_ya_cubre_el_periodo(self):
        fin = 3000 * H4
        hl4 = self._hl(0, fin, H4)
        f = FuenteFalsa("binance", {"BTCUSDT"})
        a = hx.Ampliador({"anos_4h": 0.1}, [f], fin, carpeta=self.dir)
        self.assertEqual(a.velas("BTC", "4h", hl4), hl4)
        self.assertEqual(f.peticiones, 0)
        self.assertFalse(os.listdir(self.dir))


class TestUniversoConMinimo(unittest.TestCase):
    def test_exige_volumen_minimo_en_el_momento(self):
        dia = 86400
        vols = {"A": [(t * dia, 100.0) for t in range(60)], "B": [(t * dia, 1.0) for t in range(60)]}
        sin = vd.universo_en_el_tiempo(vols, 2, 30 * dia)
        con = vd.universo_en_el_tiempo(vols, 2, 30 * dia, vol_min_ventana=50 * 30)
        self.assertTrue(sin("B", 45 * dia))
        self.assertFalse(con("B", 45 * dia))
        self.assertTrue(con("A", 45 * dia))


if __name__ == "__main__":
    unittest.main()
