"""Regresión del feed «Señales en vivo» (bloque E): persistente, incremental, sin pérdidas, sin reordenar ni
duplicar al reconectar (también por SSE), marca lo obsoleto y nunca expone direcciones ni secretos."""

import http.client
import json
import os
import threading
import time

from feed import Feed, LectorFeed, filtro_desde_parametros
from ledger import Ledger
from tests.comunes import ConCarpeta

DIR_BALLENA = "0x" + "ab12" * 10
CLAVE = "0x" + "9f" * 32


class Persistencia(ConCarpeta):
    def test_secuencia_continua_tras_reiniciar(self):
        f = Feed(self.dir)
        for i in range(5):
            f.publicar("alerta", detalle=f"a{i}")
        f2 = Feed(self.dir)                                   # "reinicio" del bot
        ev = f2.publicar("alerta", detalle="tras reiniciar")
        self.assertEqual(ev["seq"], 6)

    def test_lector_incremental_sin_perdidas_ni_duplicados_con_rotacion(self):
        f = Feed(self.dir, {"max_mb": 0.002})                 # rota cada ~2 KB
        lector = LectorFeed(self.dir)
        recibidos, cursor = [], 0
        for tanda in range(6):
            for i in range(15):
                f.publicar("alerta", moneda="ETH", detalle=f"t{tanda}-{i}")
            evs, ultimo, _, _ = lector.consulta(cursor, 1000)
            recibidos += evs
            cursor = evs[-1]["seq"] if evs else cursor
        self.assertGreater(len([x for x in os.listdir(self.dir) if x.startswith("feed-")]), 1)   # hubo rotaciones
        seqs = [e["seq"] for e in recibidos]
        self.assertEqual(seqs, list(range(1, 91)))           # ni pérdidas, ni duplicados, en orden

    def test_linea_a_medio_escribir_no_se_lee_hasta_completarse(self):
        f = Feed(self.dir)
        f.publicar("alerta", detalle="uno")
        with open(f.ruta, "a", encoding="utf-8") as fh:
            fh.write('{"seq": 2, "tipo": "alerta"')           # corte a mitad de escritura
        lector = LectorFeed(self.dir)
        evs, _, _, _ = lector.consulta(0)
        self.assertEqual([e["seq"] for e in evs], [1])
        with open(f.ruta, "a", encoding="utf-8") as fh:
            fh.write(', "detalle": "dos"}\n')
        evs, _, _, _ = lector.consulta(1)
        self.assertEqual([e["seq"] for e in evs], [2])

    def test_retencion_borra_archivados_viejos(self):
        f = Feed(self.dir, {"max_mb": 0.001, "dias_retencion": 1})
        for i in range(40):
            f.publicar("alerta", detalle="x" * 50)
        viejos = [os.path.join(self.dir, x) for x in os.listdir(self.dir) if x.startswith("feed-")]
        for v in viejos:
            os.utime(v, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        f.limpiar()
        self.assertEqual([x for x in os.listdir(self.dir) if x.startswith("feed-")], [])
        self.assertTrue(os.path.exists(f.ruta))

    def test_filtros(self):
        f = Feed(self.dir)
        f.publicar("senal_detectada", moneda="ETH", lado="L", estrategia="ruptura_1h", detalle="ruptura")
        f.publicar("decision_rechazada", moneda="SOL", lado="S", estado="rechazada", detalle="spread")
        f.publicar("ballena_fill", moneda="ETH", lado="compra")
        lector = LectorFeed(self.dir)
        def q(**k):
            return [e["tipo"] for e in lector.consulta(0, 100, filtro_desde_parametros({a: [b] for a, b in k.items()}))[0]]
        self.assertEqual(q(moneda="eth"), ["senal_detectada", "ballena_fill"])
        self.assertEqual(q(tipo="decision_rechazada,ballena_fill"), ["decision_rechazada", "ballena_fill"])
        self.assertEqual(q(q="spread"), ["decision_rechazada"])
        self.assertEqual(q(lado="L", estrategia="ruptura_1h"), ["senal_detectada"])


class Veracidad(ConCarpeta):
    def test_direcciones_y_secretos_enmascarados(self):
        f = Feed(self.dir)
        f.publicar("ballena_fill", detalle=f"cuenta {DIR_BALLENA} clave {CLAVE} token=abc123secreto",
                   datos={"cuenta": DIR_BALLENA, "otra": f"https://x/bot123456789:{'A' * 35}/"})
        with open(f.ruta, encoding="utf-8") as fh:
            crudo = fh.read()
        self.assertNotIn(DIR_BALLENA, crudo)
        self.assertNotIn(CLAVE, crudo)
        self.assertNotIn("abc123secreto", crudo)
        self.assertNotIn("A" * 35, crudo)
        self.assertIn("0xab12…ab12", crudo)

    def test_desde_ledger_conserva_cada_motivo_y_estado(self):
        led = Ledger(os.path.join(self.dir, "reg"))
        f = Feed(self.dir)
        led.observador = f.desde_ledger
        led.registrar("decision", "t1", "simulado", moneda="ETH", lado="L", estrategia="ruptura_1h", aprobada=False,
                      motivos=["horquilla demasiado ancha", "funding en contra"], plan={"tam": 0.1})
        led.registrar("orden_estado", "t1", "simulado", moneda="ETH", rol="entrada", cloid="0x1", estado="cancelada")
        led.registrar("orden_estado", "t1", "simulado", moneda="ETH", rol="entrada", cloid="0x1", estado="no_confirmada")
        evs = LectorFeed(self.dir).consulta(0)[0]
        self.assertEqual([e["tipo"] for e in evs], ["decision_rechazada", "orden_cancelada", "orden_no_confirmada"])
        self.assertEqual(evs[0]["datos"]["motivos"], ["horquilla demasiado ancha", "funding en contra"])
        self.assertIsNone(evs[1]["ts_exchange"])              # desconocido: no se inventa

    def test_un_fallo_del_feed_no_rompe_el_ledger(self):
        led = Ledger(os.path.join(self.dir, "reg"))
        led.observador = lambda ev: 1 / 0
        led.registrar("alerta", "t", "simulado", mensaje="x")
        self.assertEqual(len(led.leer()[0]), 1)


class SSE(ConCarpeta):
    def setUp(self):
        super().setUp()
        import panel
        self.panel = panel
        self.antes = (panel.AQUI, panel.LECTOR_FEED)
        panel.AQUI = self.dir
        os.makedirs(os.path.join(self.dir, "registro"), exist_ok=True)
        panel.LECTOR_FEED = LectorFeed(os.path.join(self.dir, "registro"))
        panel.SSE_INTERVALO, panel.SSE_DURACION_MAX = 0.05, 2
        self.feed = Feed(os.path.join(self.dir, "registro"))
        from http.server import ThreadingHTTPServer
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), panel.Manejador)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.panel.AQUI, self.panel.LECTOR_FEED = self.antes
        self.panel.SSE_INTERVALO, self.panel.SSE_DURACION_MAX = 1.0, 300
        super().tearDown()

    def leer_stream(self, cabeceras=None, ruta="/api/feed/stream?desde=0"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request("GET", ruta, headers=cabeceras or {})
        r = c.getresponse()
        cuerpo = r.read().decode()          # el servidor cierra a los SSE_DURACION_MAX
        c.close()
        return [json.loads(b.split("data: ", 1)[1]) for b in cuerpo.split("\n\n") if "event: evento" in b]

    def test_reconexion_sin_perder_ni_duplicar(self):
        for i in range(3):
            self.feed.publicar("alerta", detalle=f"a{i}")
        primeros = self.leer_stream()
        self.assertEqual([e["seq"] for e in primeros], [1, 2, 3])
        for i in range(2):
            self.feed.publicar("alerta", detalle=f"b{i}")
        segundos = self.leer_stream({"Last-Event-ID": "3"})    # el navegador reanuda con su último id
        self.assertEqual([e["seq"] for e in segundos], [4, 5])

    def test_api_incremental_y_obsoleto(self):
        for i in range(4):
            self.feed.publicar("alerta", detalle=f"a{i}")
        with open(os.path.join(self.dir, "panel_datos.json"), "w") as fh:
            json.dump({"ts": time.time() - 3600}, fh)
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request("GET", "/api/feed?desde=2")
        d = json.loads(c.getresponse().read())
        c.close()
        self.assertEqual([e["seq"] for e in d["eventos"]], [3, 4])
        self.assertTrue(d["obsoleto"])
        self.assertGreater(d["bot_antiguedad_seg"], 3000)
