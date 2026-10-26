"""Regresión del ciclo de órdenes (bloque A): carrera al cancelar la entrada límite, respuestas ambiguas, stops
duplicados, tamaño protegido = tamaño real, reduce-only e idempotencia. Solo cuenta simulada y mocks."""

import os

from ejecucion import Ejecutor, cambiar_estado, transicion_valida
from gestor import Gestor
from ledger import Ledger, nuevo_trade_id
from tests.comunes import LOG, ConCarpeta, MercadoFalso, config, mover, nuevo_papel


class Base(ConCarpeta):
    entrada_limite = True

    def setUp(self):
        super().setUp()
        self.c = config(ordenes__entrada_limite=self.entrada_limite, ordenes__espera_limite_seg=2,
                        ordenes__reserva_a_mercado=True)
        self.m = MercadoFalso()
        self.papel, self.info = nuevo_papel(self.dir, self.m)
        self.ledger = Ledger(os.path.join(self.dir, "registro"))
        self.estado = {"posiciones": {}}
        self.alertas = []
        self.enviadas = []                      # (tipo, sz) de cada order() que llega a la cuenta
        original = self.papel.order

        def espia(name, is_buy, sz, px, tipo, reduce_only=False, cloid=None, builder=None):
            self.enviadas.append(("trigger" if "trigger" in tipo else tipo["limit"]["tif"], float(sz)))
            return original(name, is_buy, sz, px, tipo, reduce_only, cloid)
        self.papel.order = espia
        self.gestor = self.nuevo_gestor()

    def nuevo_gestor(self):
        ej = Ejecutor(self.papel, self.info, "simulado", self.ledger, "simulado", LOG, self.c["ordenes"],
                      esperar=lambda s: None)
        meta = {a["name"]: a for a in self.m.meta()["universe"]}
        return Gestor(self.c, ej, self.info, "simulado", self.ledger, self.estado, meta, LOG, "simulado",
                      lambda t, s="info", tid=None: self.alertas.append((s, t)), lambda: None, None,
                      esperar=lambda s: None)

    def plan(self, moneda="ETH", tam=0.05):
        return {"trade_id": nuevo_trade_id(), "moneda": moneda, "lado": "L", "tam": tam, "apalancamiento": 2,
                "sl_pct": 2.0, "salida": dict(self.c["salida"], sin_objetivo=True), "duracion_min": 600,
                "estrategia": "prueba_1h", "version": "1", "precio_ref": self.m.precios[moneda], "riesgo_usd": 4.0,
                "motivo": "prueba"}

    def stops(self, moneda="ETH"):
        return [o for o in self.papel.d["ordenes"] if o["coin"] == moneda and o["tipo"] == "stop"]

    def tifs(self):
        return [t for t, _ in self.enviadas]


class CarreraCancelacion(Base):
    def test_entrada_que_sigue_abierta_tras_cancelar_no_recibe_ioc(self):
        # La cancelación "responde" pero la orden sigue viva en el exchange.
        self.papel.cancel_by_cloid = lambda *a, **k: {"status": "ok"}
        self.papel.cancel = lambda *a, **k: {"status": "ok"}
        op = self.gestor.abrir(self.plan())
        self.assertNotIn("Ioc", self.tifs())                     # nunca se completa a mercado
        self.assertIn("ETH", self.gestor.ej.congelados)
        self.assertEqual(op["estado"], "desconocido")
        self.assertIn("entrada_incierta", op)
        self.assertEqual(self.stops(), [])                       # no hay posición: nada que proteger aún

    def test_parcial_o_desconocida_tras_cancelar_no_recibe_ioc(self):
        for estado in ("parcial", "desconocida"):
            self.setUp()
            self.gestor.ej.cancelar_confirmada = lambda *a, e=estado, **k: {"estado": e, "ejecutado": None,
                                                                               "px_medio": None, "confirmado": False}
            self.gestor.abrir(self.plan())
            self.assertNotIn("Ioc", self.tifs(), estado)
            self.assertIn("ETH", self.gestor.ej.congelados, estado)

    def test_entrada_incierta_que_luego_se_llena_queda_protegida_con_el_tamano_real(self):
        self.papel.cancel_by_cloid = lambda *a, **k: {"status": "ok"}
        self.papel.cancel = lambda *a, **k: {"status": "ok"}
        self.gestor.abrir(self.plan(tam=0.05))
        mover(self.papel, self.m, "ETH", 3990.0)                 # la orden límite se llena más tarde
        self.papel.procesar()
        self.gestor.conciliar(self.m.precios)
        op = self.estado["posiciones"]["ETH"]
        self.assertEqual(op["estado"], "abierta")
        real = abs(self.papel.d["posiciones"]["ETH"]["szi"])
        self.assertAlmostEqual(op["tam"], real)
        self.assertEqual(len(self.stops()), 1)
        self.assertAlmostEqual(self.stops()[0]["sz"], real)       # tamaño protegido = tamaño real
        self.assertNotIn("Ioc", self.tifs())

    def test_cancelacion_confirmada_completa_solo_lo_que_falta(self):
        # La ALO cruzaría (se rechaza): lo ejecutado es 0 confirmado -> IOC por el plan completo, una sola vez.
        self.m.l2_snapshot = lambda moneda: {"levels": [[{"px": "4001", "sz": "1000"}], [{"px": "4000", "sz": "1000"}]]}
        op = self.gestor.abrir(self.plan(tam=0.05))
        self.assertEqual(self.tifs().count("Ioc"), 1)
        self.assertAlmostEqual(op["tam"], 0.05)
        self.assertAlmostEqual(self.stops()[0]["sz"], 0.05)


class Ambiguas(Base):
    entrada_limite = False

    def test_stop_con_respuesta_ambigua_no_se_duplica(self):
        original = self.papel.order
        ocultos = set()
        consulta = self.papel.query_order

        def stop_que_llega_pero_responde_mal(name, is_buy, sz, px, tipo, reduce_only=False, cloid=None, builder=None):
            r = original(name, is_buy, sz, px, tipo, reduce_only, cloid)
            if "trigger" in tipo and not ocultos:
                ocultos.add(str(cloid))
                raise TimeoutError("timeout")                   # la orden SÍ llegó, la respuesta no
            return r
        self.papel.order = stop_que_llega_pero_responde_mal
        # El exchange tarda en "conocerla" por cloid: la consulta dice que no consta.
        self.papel.query_order = lambda oid=None, cloid=None: ({"status": "unknownOid"} if str(cloid) in ocultos
                                                                else consulta(oid=oid, cloid=cloid))
        op = self.gestor.abrir(self.plan())
        self.assertIsNotNone(op)
        self.assertEqual(len(self.stops()), 1)                  # se adoptó el que ya estaba, no se duplicó
        self.assertEqual(op["stop_cloid"], self.stops()[0]["cloid"])

    def test_ioc_ambigua_no_duplica_la_entrada(self):
        original = self.papel.order
        llamadas = []

        def ioc_ambigua(name, is_buy, sz, px, tipo, reduce_only=False, cloid=None, builder=None):
            r = original(name, is_buy, sz, px, tipo, reduce_only, cloid)
            if "limit" in tipo and tipo["limit"]["tif"] == "Ioc" and not reduce_only:
                llamadas.append(cloid)
                raise ConnectionError("se cortó la conexión")
            return r
        self.papel.order = ioc_ambigua
        self.gestor.abrir(self.plan(tam=0.05))
        self.assertEqual(len(llamadas), 1)                      # nunca se reenvía
        self.assertAlmostEqual(abs(self.papel.d["posiciones"]["ETH"]["szi"]), 0.05)

    def test_stops_duplicados_se_reducen_a_uno(self):
        op = self.gestor.abrir(self.plan())
        # Aparece un segundo stop del bot (p. ej. un envío antiguo que llegó tarde).
        extra = self.gestor.poner_stop(op, op["sl"] * 0.99, op["tam"])
        self.assertIsNotNone(extra)
        self.assertEqual(len(self.stops()), 2)
        self.gestor.conciliar(self.m.precios)
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual(self.stops()[0]["cloid"], op["stop_cloid"])   # se conserva el registrado

    def test_stop_ausente_se_repone_sin_duplicar(self):
        op = self.gestor.abrir(self.plan())
        self.papel.cancel_by_cloid("ETH", op["stop_cloid"])
        for _ in range(3):
            self.gestor.conciliar(self.m.precios)
        self.assertEqual(len(self.stops()), 1)


class ReduceOnly(Base):
    entrada_limite = False

    def test_orden_de_proteccion_sin_reduce_only_se_bloquea(self):
        for rol in ("stop", "tp1", "tpf", "cierre", "stop_sobrante"):
            with self.assertRaises(ValueError):
                self.gestor.ej.enviar("t", rol, "ETH", False, 0.01, 3900.0, {"limit": {"tif": "Gtc"}}, reduce_only=False)
        self.assertEqual(self.enviadas, [])

    def test_cierre_verifica_que_no_quedan_ordenes(self):
        op = self.gestor.abrir(self.plan())
        self.assertTrue(self.gestor.cerrar(op, "prueba"))
        self.assertEqual([o for o in self.papel.d["ordenes"] if o["coin"] == "ETH"], [])
        self.assertNotIn("ETH", self.gestor.ej.congelados)

    def test_cierre_con_orden_que_no_se_puede_cancelar_congela(self):
        op = self.gestor.abrir(self.plan())
        fantasma = {"coin": "ETH", "oid": 999999, "cloid": op["stop_cloid"], "isTrigger": True, "triggerPx": "1",
                    "limitPx": "1", "sz": "0.05", "origSz": "0.05", "side": "A", "reduceOnly": True, "orderType": "Stop"}
        original = self.papel.frontend_open_orders
        # El exchange sigue listando el stop aunque se intente cancelar (no se deja cancelar).
        self.papel.frontend_open_orders = lambda: original() + [fantasma]
        self.gestor.cerrar(op, "prueba")
        self.assertIn("ETH", self.gestor.ej.congelados)
        self.assertTrue(any(s == "error" and "sin cancelación confirmada" in t for s, t in self.alertas))


class Idempotencia(Base):
    entrada_limite = False

    def test_cierre_repetido_no_duplica_pnl_ni_comisiones(self):
        op = self.gestor.abrir(self.plan())
        copia = dict(op, ordenes={k: dict(v) for k, v in op["ordenes"].items()})
        self.gestor.cerrar(op, "prueba")
        ops, _ = self.ledger.operaciones()
        antes = ops[op["trade_id"]]
        # Simula una caída a mitad: el mismo cierre se procesa otra vez.
        copia["fills_vistos"] = []
        self.estado["posiciones"]["ETH"] = copia
        self.gestor.procesar_cierre(copia, "prueba")
        ops, _ = self.ledger.operaciones()
        despues = ops[op["trade_id"]]
        self.assertAlmostEqual(despues["comisiones"], antes["comisiones"])
        self.assertAlmostEqual(despues["pnl_neto"], antes["pnl_neto"])
        self.assertGreater(despues["eventos_repetidos"], 0)

    def test_transiciones(self):
        self.assertFalse(transicion_valida("completada", "abierta"))
        self.assertFalse(transicion_valida("cancelada", "parcial"))
        self.assertTrue(transicion_valida("no_confirmada", "abierta"))
        o = {"estado": "completada"}
        self.assertFalse(cambiar_estado(o, "abierta"))
        self.assertEqual(o["estado"], "completada")


class UnaInstancia(ConCarpeta):
    def test_segunda_instancia_no_arranca_y_el_bloqueo_se_libera(self):
        from instancia import BloqueoInstancia, InstanciaDuplicada
        comun = os.path.join(self.dir, "comun")
        a = BloqueoInstancia(os.path.join(self.dir, "bot1"), "0xabc", comun).tomar()
        # misma cuenta desde otra carpeta del mismo equipo
        with self.assertRaises(InstanciaDuplicada):
            BloqueoInstancia(os.path.join(self.dir, "bot2"), "0xABC", comun).tomar()
        # misma carpeta
        with self.assertRaises(InstanciaDuplicada):
            BloqueoInstancia(os.path.join(self.dir, "bot1"), "0xdef", comun).tomar()
        a.soltar()
        b = BloqueoInstancia(os.path.join(self.dir, "bot2"), "0xabc", comun).tomar()   # recuperable
        b.soltar()

    def test_bloqueo_de_un_proceso_muerto_no_queda_pegado(self):
        import subprocess
        import sys
        from instancia import BloqueoInstancia
        comun = os.path.join(self.dir, "comun")
        carpeta = os.path.join(self.dir, "bot")
        codigo = ("import sys; sys.path.insert(0, %r); from instancia import BloqueoInstancia; "
                  "BloqueoInstancia(%r, '0x1', %r).tomar(); import os; os._exit(9)") % (
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), carpeta, comun)
        subprocess.run([sys.executable, "-c", codigo], check=False, timeout=30)
        BloqueoInstancia(carpeta, "0x1", comun).tomar().soltar()      # el proceso murió: se puede tomar
