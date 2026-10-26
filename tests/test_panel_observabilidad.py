import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import observabilidad as obs
import panel
from ledger import Ledger
from tests.comunes import ConCarpeta

CLAVE_FALSA = "0x" + "9f" * 32
TOKEN_TG = "123456789:" + "A" * 35


class Panel(ConCarpeta):
    def setUp(self):
        super().setUp()
        self._aqui = panel.AQUI
        panel.AQUI = self.dir
        panel.CACHE = panel.CacheLedger()
        lg = Ledger(os.path.join(self.dir, "registro"))
        lg.registrar("senal", "op-1", "simulado", moneda="ETH", lado="L", estrategia="ruptura_1h",
                     motivo="<script>alert(1)</script>", precio=100.0)
        lg.registrar("decision", "op-1", "simulado", aprobada=True, motivos=[])
        lg.registrar("fill", "op-1", "simulado", rol="entrada", px=100.0, sz=1.0, comision=0.05)
        lg.registrar("cierre", "op-1", "simulado", causa="stop", pnl_bruto=-1.0)
        datos = {"ts": 0, "modo": "simulado", "posiciones": [{"moneda": "ETH", "trade_id": "op-1"}],
                 "config": {"HL_API_WALLET_KEY": CLAVE_FALSA, "telegram_token": TOKEN_TG}, "alertas": [],
                 "ballenas": [{"direccion": "0x" + "12" * 20, "direccion_completa": "0x" + "12" * 20}]}
        with open(os.path.join(self.dir, "panel_datos.json"), "w", encoding="utf-8") as fh:
            json.dump(datos, fh)
        with open(os.path.join(self.dir, "panel.html"), "w", encoding="utf-8") as fh:
            fh.write('<meta name="panel-token" content="{{TOKEN}}"><meta name="x" content="{{SOLO_LECTURA}}">')
        os.makedirs(os.path.join(self.dir, "logs"))
        with open(os.path.join(self.dir, "logs", "bot.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "2026-01-01T00:00:00Z", "nivel": "INFO", "componente": "operaciones",
                                 "mensaje": "ABIERTA LARGO ETH saldo 1000"}) + "\n")
            fh.write(json.dumps({"ts": "2026-01-01T00:00:01Z", "nivel": "ERROR", "componente": "sistema",
                                 "mensaje": "fallo de red", "traza": "ciclo-7"}) + "\n")
        panel.Manejador.clave = ""
        panel.Manejador.comandos_habilitados = True
        panel.Manejador.comandos_remotos = False
        panel.Manejador.limite = panel.Limite(100, 60)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), panel.Manejador)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        panel.AQUI = self._aqui
        panel.CACHE = panel.CacheLedger()
        super().tearDown()

    def get(self, ruta, cab=None):
        req = urllib.request.Request(self.url + ruta, headers=cab or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), dict(e.headers)

    def post(self, cuerpo, cab=None, ruta="/api/comando"):
        h = {"Content-Type": "application/json", "X-Panel-Token": panel.Manejador.token}
        h.update(cab or {})
        req = urllib.request.Request(self.url + ruta, data=json.dumps(cuerpo).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # ------------------------------------------------------------ lectura
    def test_detalle_de_operacion(self):
        st, cuerpo, _ = self.get("/api/operacion?id=op-1")
        self.assertEqual(st, 200)
        d = json.loads(cuerpo)
        self.assertEqual(d["estado"], "cerrada")
        self.assertEqual([e["tipo"] for e in d["eventos"]], ["senal", "decision", "fill", "cierre"])

    def test_lista_filtrada_paginada_y_csv(self):
        st, cuerpo, _ = self.get("/api/operaciones?moneda=ETH&por_pagina=1")
        d = json.loads(cuerpo)
        self.assertEqual((st, d["total"], d["paginas"]), (200, 1, 1))
        self.assertEqual(json.loads(self.get("/api/operaciones?moneda=BTC")[1])["total"], 0)
        st, csv, cab = self.get("/api/operaciones?formato=csv")
        self.assertEqual(st, 200)
        self.assertIn("attachment", cab.get("Content-Disposition", ""))
        self.assertIn("op-1", csv)

    def test_sin_secretos_y_direcciones_enmascaradas(self):
        _, cuerpo, _ = self.get("/api/datos")
        self.assertNotIn(CLAVE_FALSA, cuerpo)
        self.assertNotIn(TOKEN_TG, cuerpo)
        self.assertNotIn("12" * 20, cuerpo)

    def test_sin_cors_y_cabeceras_de_seguridad(self):
        _, _, cab = self.get("/api/datos", {"Origin": "http://otro.sitio"})
        self.assertNotIn("Access-Control-Allow-Origin", cab)
        self.assertEqual(cab.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", cab.get("Content-Security-Policy", ""))

    def test_observabilidad_sin_datos_de_trading(self):
        _, cuerpo, _ = self.get("/api/observabilidad")
        d = json.loads(cuerpo)
        self.assertEqual([e["mensaje"] for e in d["logs"]], ["fallo de red"])
        self.assertNotIn("saldo", cuerpo.lower())
        self.assertEqual(len(json.loads(self.get("/api/observabilidad?traza=ciclo-7")[1])["logs"]), 1)
        _, diag, _ = self.get("/api/diagnostico")
        self.assertNotIn("ABIERTA", diag)

    def test_contrasena(self):
        panel.Manejador.clave = "secreta"
        self.assertEqual(self.get("/api/datos")[0], 401)
        ok = base64.b64encode(b"x:secreta").decode()
        self.assertEqual(self.get("/api/datos", {"Authorization": "Basic " + ok})[0], 200)
        mal = base64.b64encode(b"x:otra").decode()
        self.assertEqual(self.get("/api/datos", {"Authorization": "Basic " + mal})[0], 401)

    # ------------------------------------------------------------ comandos
    def test_comando_requiere_token_y_mismo_origen(self):
        self.assertEqual(self.post({"cmd": "/pausa"}, {"X-Panel-Token": "malo"})[0], 403)
        self.assertEqual(self.post({"cmd": "/pausa"}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post({"cmd": "/pausa"})[0], 202)

    def test_lista_cerrada_de_comandos(self):
        for cmd in ("/real", "/modo real", "/cerrartodo", "/aprobar"):
            self.assertEqual(self.post({"cmd": cmd})[0], 400)

    def test_cierre_exige_confirmacion_y_alcance(self):
        self.assertEqual(self.post({"cmd": "/cerrarbot"})[0], 400)
        self.assertEqual(self.post({"cmd": "/cerrarbot", "confirmacion": "CERRAR-BOT", "alcance": ["BTC"]})[0], 409)
        st, r = self.post({"cmd": "/cerrarbot", "confirmacion": "CERRAR-BOT", "alcance": ["ETH"]})
        self.assertEqual(st, 202)
        with open(os.path.join(self.dir, "comandos_panel.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[-1]["alcance"], ["ETH"])

    def test_shadow_rechaza_cierre(self):
        with open(os.path.join(self.dir, "panel_datos.json"), "w", encoding="utf-8") as fh:
            json.dump({"modo": "shadow", "posiciones": []}, fh)
        st, r = self.post({"cmd": "/cerrarbot", "confirmacion": "CERRAR-BOT", "alcance": []})
        self.assertEqual(st, 409)
        self.assertIn("SHADOW", r["error"])

    def test_remoto_solo_lectura(self):
        self.assertEqual(self.post({"cmd": "/pausa"}, {"X-Forwarded-For": "8.8.8.8"})[0], 403)

    def test_limite_de_tamano(self):
        st, _ = self.post({"cmd": "/pausa", "relleno": "x" * 5000})
        self.assertEqual(st, 413)


class Observabilidad(ConCarpeta):
    def test_secretos_ocultos_en_logs(self):
        self.assertNotIn(CLAVE_FALSA, obs.ocultar_secretos(f"clave {CLAVE_FALSA}"))
        self.assertNotIn(TOKEN_TG, obs.ocultar_secretos(f"url bot{TOKEN_TG}/send"))
        self.assertNotIn("hunter2", obs.ocultar_secretos("PANEL_PASSWORD=hunter2"))

    def test_log_estructurado_y_filtrado(self):
        obs.configurar_logs(self.dir, consola=False)
        logging.getLogger("bot.sistema").error("fallo con %s", CLAVE_FALSA)
        logging.getLogger("bot.operaciones").info("ABIERTA LARGO ETH")
        for h in logging.getLogger("bot").handlers:
            h.flush()
        regs = obs.leer_logs_estructurados(self.dir)
        self.assertEqual([r["componente"] for r in regs], ["sistema"])
        self.assertNotIn(CLAVE_FALSA, json.dumps(regs))
        for h in list(logging.getLogger("bot").handlers):
            h.close()
            logging.getLogger("bot").removeHandler(h)

    def test_estados_e_incidentes_agrupados(self):
        s = obs.Sistema(self.dir, umbrales={"api_rest": 1})
        self.assertEqual(s.foto()["componentes"]["api_rest"]["estado"], "sin_datos")
        s.marcar("api_rest", "correcto")
        s.comp["api_rest"]["ts"] -= 10
        self.assertEqual(s.foto()["componentes"]["api_rest"]["estado"], "desactualizado")
        for _ in range(3):
            s.incidente("api_rest", "precios", "error", "fallo")
        self.assertEqual(len(s.incidentes), 1)
        self.assertEqual(s.incidentes[0]["repeticiones"], 3)
        self.assertTrue(s.reconocer(s.incidentes[0]["id"]))
        s.resolver("api_rest", "precios")
        self.assertEqual(s.incidentes[0]["estado"], "resuelto")
