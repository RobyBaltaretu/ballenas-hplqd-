"""G10: direcciones, claves, tokens y credenciales no aparecen en el panel, sus exportaciones, el feed ni los logs."""

import json
import logging
import os

from ledger import Ledger
from tests.comunes import ConCarpeta

DIRECCION = "0x" + "a1b2" * 10
CLAVE = "0x" + "7e" * 32
TOKEN_TG = "123456789:" + "Q" * 35
HC = "https://hc-ping.com/1234abcd-5678-90ef-aaaa-bbbbccccdddd"


class SinSecretos(ConCarpeta):
    def setUp(self):
        super().setUp()
        import panel
        self.panel = panel
        self.antes = panel.AQUI
        panel.AQUI = self.dir
        datos = {"ts": 1, "modo": "simulado", "cuenta": DIRECCION, "config": {"ballenas_manuales": [DIRECCION],
                 "telegram": {"token": TOKEN_TG}}, "ballenas": [{"direccion": DIRECCION}],
                 "alertas": [{"mensaje": f"clave {CLAVE} token={TOKEN_TG} {HC}"}]}
        with open(os.path.join(self.dir, "panel_datos.json"), "w") as fh:
            json.dump(datos, fh)
        led = Ledger(os.path.join(self.dir, "registro"))
        led.registrar("alerta", "op-1", "simulado", moneda="ETH", mensaje=f"cuenta {DIRECCION} {CLAVE}")
        led.registrar("cierre", "op-1", "simulado", moneda="ETH", causa=f"x {DIRECCION}", pnl_bruto=1.0)

    def tearDown(self):
        self.panel.AQUI = self.antes
        super().tearDown()

    def comprobar(self, texto):
        for s in (DIRECCION, CLAVE, TOKEN_TG, "1234abcd-5678"):
            self.assertNotIn(s, texto)

    def test_api_datos(self):
        self.comprobar(json.dumps(self.panel.todos_los_datos(), ensure_ascii=False))

    def test_exportacion_csv_y_detalle(self):
        self.comprobar(self.panel.csv_operaciones({}))
        self.comprobar(json.dumps(self.panel.limpio(self.panel.detalle_operacion("op-1")), ensure_ascii=False))

    def test_diagnostico_y_logs(self):
        from observabilidad import configurar_logs
        configurar_logs(self.dir, consola=False)
        logging.getLogger("bot.sistema").warning("ballena %s clave %s token %s url %s", DIRECCION, CLAVE, TOKEN_TG, HC)
        for h in logging.getLogger("bot").handlers:
            h.flush()
        for nombre in ("bot.log", os.path.join("logs", "bot.jsonl")):
            with open(os.path.join(self.dir, nombre), encoding="utf-8") as fh:
                self.comprobar(fh.read())
        self.comprobar(json.dumps(self.panel.diagnostico(), ensure_ascii=False))
        for h in list(logging.getLogger("bot").handlers):
            h.close()
            logging.getLogger("bot").removeHandler(h)
