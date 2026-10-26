import os
import unittest

from ejecucion import Ejecutor, EjecutorShadow, ErrorShadow, interpretar_respuesta, normalizar_consulta
from ledger import Ledger
from tests.comunes import LOG, ConCarpeta, MercadoFalso, nuevo_papel

CFG = {"max_consultas_estado": 3}


class ExchangeTimeout:
    """Envía la orden al papel pero 'pierde' la respuesta (como un timeout de red)."""

    def __init__(self, papel):
        self.papel, self.ordenes = papel, 0

    def order(self, *a, **k):
        self.ordenes += 1
        self.papel.order(*a, **k)
        raise TimeoutError("timeout leyendo la respuesta")

    def update_leverage(self, *a, **k):
        return {"status": "ok"}


class CancelSinEfecto:
    def __init__(self, papel):
        self.papel = papel

    def order(self, *a, **k):
        return self.papel.order(*a, **k)

    def cancel_by_cloid(self, *a, **k):
        raise ConnectionError("sin conexión")

    def cancel(self, *a, **k):
        raise ConnectionError("sin conexión")


class InfoCaida:
    def query_order_by_cloid(self, *a):
        raise ConnectionError("sin conexión")

    query_order_by_oid = query_order_by_cloid


class Ordenes(ConCarpeta):
    def setUp(self):
        super().setUp()
        self.m = MercadoFalso()
        self.papel, self.info = nuevo_papel(self.dir, self.m)
        self.ledger = Ledger(os.path.join(self.dir, "registro"))

    def ej(self, ex=None, info=None, umbral=5):
        return Ejecutor(ex or self.papel, info or self.info, "simulado", self.ledger, "simulado", LOG, CFG,
                        umbral, esperar=lambda s: None)

    def test_respuesta_ambigua_consulta_y_no_reenvia(self):
        ex = ExchangeTimeout(self.papel)
        e = self.ej(ex)
        r = e.enviar("t1", "entrada", "ETH", True, 0.01, 4100.0, {"limit": {"tif": "Gtc"}})
        self.assertEqual(ex.ordenes, 1)                      # una sola vez
        self.assertEqual(r["estado"], "completada")          # resuelto consultando por CLOID
        self.assertGreaterEqual(e.llamadas["consulta"], 1)
        self.assertEqual(len([f for f in self.papel.d["fills"] if f["cloid"] == r["cloid"]]), 1)

    def test_ambigua_sin_poder_consultar_congela(self):
        ex = ExchangeTimeout(self.papel)
        e = self.ej(ex, InfoCaida())
        r = e.enviar("t1", "entrada", "ETH", True, 0.01, 4100.0, {"limit": {"tif": "Gtc"}})
        self.assertEqual(r["estado"], "desconocida")
        self.assertIn("ETH", e.congelados)
        self.assertEqual(ex.ordenes, 1)

    def test_cloid_duplicado_rechazado(self):
        cl = "0x" + "11" * 16
        a = self.papel.order("ETH", True, 0.01, 3000.0, {"limit": {"tif": "Gtc"}}, cloid=cl)
        b = self.papel.order("ETH", True, 0.01, 3000.0, {"limit": {"tif": "Gtc"}}, cloid=cl)
        self.assertEqual(interpretar_respuesta(a)[0], "abierta")
        self.assertEqual(interpretar_respuesta(b)[0], "rechazada")
        self.assertEqual(len(self.papel.d["ordenes"]), 1)

    def test_cancelacion_verificada(self):
        e = self.ej()
        r = e.enviar("t1", "tp", "ETH", False, 0.01, 5000.0, {"limit": {"tif": "Gtc"}})
        self.assertEqual(r["estado"], "abierta")
        self.assertEqual(e.cancelar("t1", "tp", "ETH", r["oid"], r["cloid"]), "cancelada")

    def test_cancelacion_ambigua_congela(self):
        e = self.ej(CancelSinEfecto(self.papel))
        r = e.enviar("t1", "tp", "ETH", False, 0.01, 5000.0, {"limit": {"tif": "Gtc"}})
        estado = e.cancelar("t1", "tp", "ETH", r["oid"], r["cloid"])
        self.assertEqual(estado, "abierta")         # sigue abierta: NO se da por cancelada
        e2 = self.ej(CancelSinEfecto(self.papel), InfoCaida())
        self.assertEqual(e2.cancelar("t1", "tp", "ETH", r["oid"], r["cloid"]), "desconocida")
        self.assertIn("ETH", e2.congelados)

    def test_degradado_tras_errores_seguidos(self):
        e = self.ej(info=InfoCaida(), umbral=3)
        for _ in range(3):
            e.consultar(cloid="0x" + "22" * 16)
        self.assertIsNotNone(e.degradado)

    def test_todo_queda_en_el_ledger(self):
        e = self.ej()
        e.enviar("t9", "entrada", "ETH", True, 0.01, 4100.0, {"limit": {"tif": "Ioc"}})
        tipos = [ev["tipo"] for ev in self.ledger.leer()[0] if ev["trade_id"] == "t9"]
        self.assertEqual(tipos[0], "orden_enviada")         # registrada ANTES de enviar
        self.assertIn("orden_estado", tipos)


class Shadow(unittest.TestCase):
    def test_shadow_no_puede_operar(self):
        e = EjecutorShadow(LOG)
        self.assertFalse(e.puede_operar)
        for llamada in (lambda: e.enviar("t", "entrada", "BTC", True, 1, 1, {}), lambda: e.cancelar("t", "x", "BTC"),
                        lambda: e.apalancamiento("BTC", 2), lambda: e.fills(), lambda: e.consultar(cloid="x")):
            with self.assertRaises(ErrorShadow):
                llamada()
        self.assertEqual(len(e.intentos), 5)
        self.assertFalse(hasattr(e, "ex"))


class Normalizar(unittest.TestCase):
    def test_parcial(self):
        n = normalizar_consulta({"status": "order", "order": {"status": "open", "order": {"origSz": "2", "sz": "0.5", "oid": 1}}})
        self.assertEqual(n["estado"], "parcial")
        self.assertAlmostEqual(n["ejecutado"], 1.5)

    def test_desconocida(self):
        self.assertIsNone(normalizar_consulta("basura"))
        self.assertEqual(normalizar_consulta({"status": "unknownOid"})["estado"], "no_consta")
