"""Propiedad de posiciones, protección y conciliación, con la cuenta simulada (sin red)."""

import os

from ejecucion import Ejecutor
from gestor import Gestor, conciliar, leer_cuenta
from ledger import Ledger, nuevo_trade_id
from tests.comunes import LOG, ConCarpeta, MercadoFalso, config, mover, nuevo_papel


class Base(ConCarpeta):
    def setUp(self):
        super().setUp()
        self.c = config(ordenes__entrada_limite=False)
        self.m = MercadoFalso()
        self.papel, self.info = nuevo_papel(self.dir, self.m)
        self.ledger = Ledger(os.path.join(self.dir, "registro"))
        self.estado = {"posiciones": {}}
        self.alertas = []
        self.gestor = self.nuevo_gestor()

    def nuevo_gestor(self, estado=None):
        ej = Ejecutor(self.papel, self.info, "simulado", self.ledger, "simulado", LOG, self.c["ordenes"],
                      esperar=lambda s: None)
        meta = {a["name"]: a for a in self.m.meta()["universe"]}
        return Gestor(self.c, ej, self.info, "simulado", self.ledger, estado or self.estado, meta, LOG, "simulado",
                      lambda t, s="info", tid=None: self.alertas.append((s, t)), lambda: None, None,
                      esperar=lambda s: None)

    def plan(self, moneda="ETH", lado="L", tam=0.05, sl_pct=2.0):
        return {"trade_id": nuevo_trade_id(), "moneda": moneda, "lado": lado, "tam": tam, "apalancamiento": 2,
                "sl_pct": sl_pct, "salida": dict(self.c["salida"]), "duracion_min": 600, "estrategia": "prueba_1h",
                "version": "1", "precio_ref": self.m.precios[moneda], "riesgo_usd": 4.0, "motivo": "prueba"}

    def stops(self, moneda):
        return [o for o in self.papel.d["ordenes"] if o["coin"] == moneda and o["tipo"] == "stop"]


class Apertura(Base):
    def test_abre_y_deja_stop_verificado(self):
        op = self.gestor.abrir(self.plan())
        self.assertEqual(op["estado"], "abierta")
        self.assertEqual(len(self.stops("ETH")), 1)
        self.assertTrue(self.stops("ETH")[0]["reduce_only"])
        self.assertAlmostEqual(self.stops("ETH")[0]["sz"], op["tam"])
        inf = self.gestor.conciliar(self.m.precios)
        self.assertEqual(inf["propias"], ["ETH"])
        self.assertEqual(self.gestor.sin_stop, {})

    def test_stop_rechazado_cierra_la_posicion(self):
        original = self.papel.order

        def sin_stops(name, is_buy, sz, px, tipo, reduce_only=False, cloid=None, builder=None):
            if "trigger" in tipo:
                return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "rechazado"}]}}}
            return original(name, is_buy, sz, px, tipo, reduce_only, cloid)
        self.papel.order = sin_stops
        self.assertIsNone(self.gestor.abrir(self.plan()))
        self.assertNotIn("ETH", self.papel.d["posiciones"])     # sin stop no se queda abierta
        self.assertTrue(any(s == "error" for s, _ in self.alertas))


class Propiedad(Base):
    def test_posicion_externa_no_se_toca(self):
        # Posición manual (externa) en BTC, hecha fuera del bot.
        self.papel.order("BTC", True, 0.001, 110_000.0, {"limit": {"tif": "Ioc"}})
        self.gestor.abrir(self.plan("ETH"))
        ordenes_antes = [o["oid"] for o in self.papel.d["ordenes"] if o["coin"] == "BTC"]
        inf = self.gestor.conciliar(self.m.precios)
        self.assertEqual([p["moneda"] for p in inf["externas"]], ["BTC"])
        self.assertEqual([o["oid"] for o in self.papel.d["ordenes"] if o["coin"] == "BTC"], ordenes_antes)   # sin stop puesto
        # Cerrar las del bot no toca BTC
        for op in list(self.estado["posiciones"].values()):
            self.assertTrue(self.gestor.cerrar(op, "prueba"))
        self.assertIn("BTC", self.papel.d["posiciones"])
        self.assertNotIn("ETH", self.papel.d["posiciones"])

    def test_tamano_mayor_que_el_registro_congela(self):
        self.gestor.abrir(self.plan("ETH", tam=0.05))
        self.papel.order("ETH", True, 0.05, 4100.0, {"limit": {"tif": "Ioc"}})      # alguien añade a mano
        inf = self.gestor.conciliar(self.m.precios)
        self.assertTrue(inf["tam_mayor"])
        self.assertIn("ETH", self.gestor.ej.congelados)

    def test_orden_externa_en_mercado_del_bot_congela_y_no_se_cancela(self):
        self.gestor.abrir(self.plan("ETH"))
        self.papel.order("ETH", False, 0.01, 9000.0, {"limit": {"tif": "Gtc"}})     # orden manual
        self.gestor.conciliar(self.m.precios)
        self.assertIn("ETH", self.gestor.ej.congelados)
        self.assertTrue(any(o["px"] == 9000.0 for o in self.papel.d["ordenes"] if o["tipo"] == "limite"))

    def test_conciliar_pura_clasifica(self):
        cuenta = {"posiciones": {"SOL": {"lado": "S", "tam": 1.0}}, "ordenes": []}
        inf = conciliar({}, cuenta)
        self.assertEqual(inf["externas"][0]["moneda"], "SOL")

    def test_orden_propia_huerfana_se_cancela_sin_alarma(self):
        op = self.gestor.abrir(self.plan("ETH"))
        self.assertIn(op["stop_cloid"], self.estado["cloids_propios"])
        self.estado["posiciones"].pop("ETH")                  # la operación ya no está abierta en el registro
        self.gestor.conciliar(self.m.precios)
        self.assertEqual(self.stops("ETH"), [])               # su stop reduce-only huérfano se cancela
        self.assertFalse(any("CLOID desconocido" in t for _, t in self.alertas))

    def test_orden_propia_se_reconoce_por_el_ledger(self):
        op = self.gestor.abrir(self.plan("ETH"))
        self.estado["posiciones"].pop("ETH")
        self.estado["cloids_propios"] = {}                    # registro perdido: queda el ledger
        g = self.nuevo_gestor()
        self.assertEqual(g._orden_propia(op["stop_cloid"])["trade_id"], op["trade_id"])
        g.conciliar(self.m.precios)
        self.assertEqual(self.stops("ETH"), [])

    def test_cloid_ajeno_avisa_una_vez_por_hora(self):
        self.papel.order("ETH", False, 0.01, 9000.0, {"limit": {"tif": "Gtc"}},
                         cloid=__import__("ejecucion")._objeto_cloid(__import__("ejecucion").nuevo_cloid()))
        for _ in range(3):
            self.gestor.conciliar(self.m.precios)
        self.assertEqual(sum("CLOID desconocido" in t for _, t in self.alertas), 1)


class Proteccion(Base):
    def test_stop_desaparecido_se_repone(self):
        op = self.gestor.abrir(self.plan())
        self.papel.cancel_by_cloid("ETH", op["stop_cloid"])           # el stop desaparece
        self.gestor.conciliar(self.m.precios)
        self.assertEqual(len(self.stops("ETH")), 1)
        self.assertEqual(self.gestor.sin_stop, {})
        self.assertTrue(any("stop no verificado" in t for _, t in self.alertas))

    def test_objetivo_parcial_rehace_stop_con_tamano_restante(self):
        op = self.gestor.abrir(self.plan(tam=0.05))
        self.assertTrue(op["parcial"])
        mover(self.papel, self.m, "ETH", op["tp1"] * 1.001)
        self.gestor.conciliar(self.m.precios)
        st = self.stops("ETH")
        self.assertEqual(len(st), 1)
        self.assertAlmostEqual(st[0]["sz"], self.estado["posiciones"]["ETH"]["tam"])
        self.assertLess(self.estado["posiciones"]["ETH"]["tam"], 0.05)

    def test_mover_stop_pone_el_nuevo_antes_de_quitar_el_viejo(self):
        op = self.gestor.abrir(self.plan())
        viejo = op["stop_cloid"]
        llamadas = []
        orig_order, orig_cancel = self.papel.order, self.papel.cancel_by_cloid
        self.papel.order = lambda *a, **k: (llamadas.append("nuevo"), orig_order(*a, **k))[1]
        self.papel.cancel_by_cloid = lambda *a, **k: (llamadas.append("cancelar"), orig_cancel(*a, **k))[1]
        self.assertTrue(self.gestor.reemplazar_stop(op, op["sl"] * 1.01, op["tam"], "prueba"))
        self.assertEqual(llamadas[:2], ["nuevo", "cancelar"])
        self.assertNotEqual(op["stop_cloid"], viejo)
        self.assertEqual(len(self.stops("ETH")), 1)

    def test_stop_disparado_registra_cierre_con_resultado(self):
        op = self.gestor.abrir(self.plan())
        mover(self.papel, self.m, "ETH", op["sl"] * 0.999)
        self.gestor.conciliar(self.m.precios)
        self.assertNotIn("ETH", self.estado["posiciones"])
        ops, _ = self.ledger.operaciones()
        o = ops[op["trade_id"]]
        self.assertEqual(o["estado"], "cerrada")
        self.assertLess(o["pnl_neto"], 0)
        self.assertGreater(o["comisiones"], 0)
        self.assertAlmostEqual(o["r_neto"], o["pnl_neto"] / o["riesgo_inicial_usd"], places=6)


class Reinicio(Base):
    def test_reinicio_reconcilia_con_el_registro(self):
        op = self.gestor.abrir(self.plan())
        g2 = self.nuevo_gestor(self.estado)                 # proceso nuevo, mismo estado guardado
        inf = g2.conciliar(self.m.precios)
        self.assertEqual(inf["propias"], ["ETH"])
        self.assertEqual(len(self.stops("ETH")), 1)          # no duplica el stop
        self.assertEqual(self.estado["posiciones"]["ETH"]["trade_id"], op["trade_id"])

    def test_entrada_a_medias_se_reconstruye_desde_la_cuenta(self):
        p = self.plan()
        # Simula caída del proceso tras enviar la entrada y antes de proteger: la orden se ejecutó pero el
        # registro quedó en 'enviada', sin stop.
        self.papel.order("ETH", True, p["tam"], 4100.0, {"limit": {"tif": "Ioc"}}, cloid="0x" + "33" * 16)
        self.estado["posiciones"]["ETH"] = {
            "trade_id": p["trade_id"], "origen": "simulado", "moneda": "ETH", "lado": "L", "estrategia": "prueba_1h",
            "estado": "enviada", "tam": 0.0, "tam_inicial": 0.0, "entrada": None, "precio_ref": p["precio_ref"], "sl": None,
            "sl_pct": 2.0, "salida": p["salida"], "duracion_min": 600, "abierta_ts": 0, "tam_plan": p["tam"],
            "ordenes": {"0x" + "33" * 16: {"rol": "entrada", "oid": None, "estado": "desconocida", "sz": p["tam"]}},
            "sobrantes": []}
        self.gestor.conciliar(self.m.precios)
        op = self.estado["posiciones"]["ETH"]
        self.assertEqual(op["estado"], "abierta")
        self.assertEqual(len(self.stops("ETH")), 1)


class LecturaCuenta(Base):
    def test_lee_liquidacion_y_margen_si_existen(self):
        self.gestor.abrir(self.plan())
        cta = leer_cuenta(self.info, "simulado")
        self.assertIn("ETH", cta["posiciones"])
        self.assertGreater(cta["equity"], 0)


class ReservaPorMoneda(Base):
    def test_segunda_entrada_misma_moneda_se_rechaza_sin_ordenes(self):
        op1 = self.gestor.abrir(self.plan("ETH"))
        self.assertEqual(op1["estado"], "abierta")
        n_ordenes = len(self.papel.d["ordenes"])
        tam = self.papel.d["posiciones"]["ETH"]["szi"]
        self.assertIsNone(self.gestor.abrir(self.plan("ETH")))
        self.assertEqual(self.estado["posiciones"]["ETH"]["trade_id"], op1["trade_id"])   # no se sobrescribe
        self.assertEqual(len(self.papel.d["ordenes"]), n_ordenes)                           # nada enviado
        self.assertEqual(self.papel.d["posiciones"]["ETH"]["szi"], tam)

    def test_posicion_ajena_en_la_cuenta_bloquea_la_entrada(self):
        self.papel.order("SOL", True, 1.0, 1000.0, {"limit": {"tif": "Ioc"}})
        self.assertIsNone(self.gestor.abrir(self.plan("SOL", tam=1.0)))
        self.assertNotIn("SOL", self.estado["posiciones"])
