import json
import os

import config_panel as cp
import configuracion as conf
from tests.comunes import ConCarpeta
import tests.test_panel_observabilidad as tpo

ENT = {}


def cfg_base():
    c = conf._fusionar(conf.DEFECTOS, {"modo": "simulado", "version_config": conf.VERSION_CONFIG})
    return conf.validar(c, entorno=ENT)


class Cambios(ConCarpeta):
    def test_lista_blanca_y_prohibidos(self):
        c = cfg_base()
        for ruta, v in (("modo", "real"), ("real.habilitado", True), ("panel.comandos_remotos", True),
                        ("piloto_real.capital_max_usd", 1e6), ("comisiones.taker_pct", 0), ("algo.raro", 1)):
            with self.assertRaises(cp.ErrorCambio):
                cp.aplicar_cambios(c, {ruta: v}, entorno=ENT)

    def test_rangos_y_tipos(self):
        c = cfg_base()
        with self.assertRaises(cp.ErrorCambio):
            cp.aplicar_cambios(c, {"riesgo.riesgo_por_operacion_pct": 50}, entorno=ENT)
        with self.assertRaises(cp.ErrorCambio):
            cp.aplicar_cambios(c, {"riesgo.max_posiciones_abiertas": 2.5}, entorno=ENT)
        with self.assertRaises(cp.ErrorCambio):
            cp.aplicar_cambios(c, {"universo.incluir_siempre": ["BTC", "<script>"]}, entorno=ENT)
        with self.assertRaises(cp.ErrorCambio):       # regla cruzada de validar(): tp1 < tp final
            cp.aplicar_cambios(c, {"salida.tp1_r": 5, "salida.tp_final_r": 3}, entorno=ENT)

    def test_cambio_valido_y_aviso_de_riesgo(self):
        c = cfg_base()
        nuevo, lista = cp.aplicar_cambios(c, {"riesgo.riesgo_por_operacion_pct": 2, "universo.max_monedas": 30,
                                              "universo.incluir_siempre": ["BTC", "ETH", "SOL"],
                                              "filtros_ballenas.numero_ballenas": 25}, entorno=ENT)
        self.assertEqual(nuevo["riesgo"]["riesgo_por_operacion_pct"], 2)
        self.assertEqual(nuevo["universo"]["incluir_siempre"], ["BTC", "ETH", "SOL"])
        self.assertEqual(nuevo["filtros_ballenas"]["numero_ballenas"], 25)
        self.assertTrue(next(x for x in lista if x["ruta"] == "riesgo.riesgo_por_operacion_pct")["sube_riesgo"])
        self.assertEqual(nuevo["modo"], "simulado")

    def test_ballenas_manuales_sin_reenviar_direcciones(self):
        c = cfg_base()
        a, b = "0x" + "ab" * 20, "0x" + "cd" * 20
        nuevo, _ = cp.aplicar_cambios(c, {"ballenas_manuales": {"anadir": [a, b]}}, entorno=ENT)
        esq = cp.esquema(nuevo)
        v = next(x for x in esq["campos"] if x["ruta"] == "ballenas_manuales")["valor"]
        self.assertNotIn(a, json.dumps(esq))                   # en pantalla, enmascaradas
        self.assertEqual(len(v), 2)
        nuevo2, lista = cp.aplicar_cambios(nuevo, {"ballenas_manuales": {"quitar": [0]}}, entorno=ENT)
        self.assertEqual(nuevo2["ballenas_manuales"], [b])
        self.assertNotIn(a, json.dumps(lista))
        with self.assertRaises(cp.ErrorCambio):
            cp.aplicar_cambios(c, {"ballenas_manuales": {"anadir": ["0x123"]}}, entorno=ENT)

    def test_estrategias(self):
        c = cfg_base()
        nuevo, _ = cp.aplicar_cambios(c, {"estrategias.ruptura_1h.activa": False}, ["ruptura_1h"], entorno=ENT)
        self.assertFalse(conf.estrategia_activa(nuevo, "ruptura_1h"))
        with self.assertRaises(cp.ErrorCambio):
            cp.aplicar_cambios(c, {"estrategias.inventada.activa": True}, ["ruptura_1h"], entorno=ENT)

    def test_guardar_copia_historial_y_restaurar(self):
        c = cfg_base()
        ruta = os.path.join(self.dir, "config.json")
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(c, fh)
        nuevo, lista = cp.aplicar_cambios(c, {"universo.max_monedas": 33}, entorno=ENT)
        copia = cp.guardar(self.dir, nuevo, lista, "prueba")
        with open(ruta, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["universo"]["max_monedas"], 33)
        h = cp.historial(self.dir)
        self.assertEqual(h[0]["cambios"][0]["despues"], 33)
        self.assertTrue(h[0]["copia_disponible"])
        cp.restaurar(self.dir, copia, "prueba", entorno=ENT)
        with open(ruta, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["universo"]["max_monedas"], c["universo"]["max_monedas"])
        with self.assertRaises(cp.ErrorCambio):
            cp.restaurar(self.dir, "../config.json", "prueba", entorno=ENT)

    def test_restaurar_no_cambia_el_modo(self):
        c = cfg_base()
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(c, fh)
        os.makedirs(os.path.join(self.dir, cp.DIR_COPIAS))
        with open(os.path.join(self.dir, cp.DIR_COPIAS, "config-20260101T000000Z.json"), "w", encoding="utf-8") as fh:
            json.dump(dict(c, modo="shadow"), fh)
        with self.assertRaises(cp.ErrorCambio):
            cp.restaurar(self.dir, "config-20260101T000000Z.json", "prueba", entorno=ENT)


class ApiConfig(tpo.Panel):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg_base(), fh)

    def test_previsualizar_guardar_y_pedir_aplicar(self):
        st, r = self.post({"cambios": {"universo.max_monedas": 31}}, ruta="/api/config")
        self.assertEqual(st, 200)
        self.assertTrue(r["previsualizacion"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "comandos_panel.json")))
        st, r = self.post({"cambios": {"universo.max_monedas": 31}, "confirmar": True}, ruta="/api/config")
        self.assertEqual(st, 202)
        with open(os.path.join(self.dir, "comandos_panel.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[-1]["cmd"], "/aplicarconfig")
        with open(os.path.join(self.dir, "config.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["universo"]["max_monedas"], 31)

    def test_no_cambia_el_modo_ni_desde_fuera(self):
        st, _ = self.post({"cambios": {"modo": "real"}, "confirmar": True}, ruta="/api/config")
        self.assertEqual(st, 400)
        st, _ = self.post({"cambios": {"universo.max_monedas": 31}, "confirmar": True}, {"X-Forwarded-For": "8.8.8.8"},
                          ruta="/api/config")
        self.assertEqual(st, 403)
        st, _ = self.post({"cambios": {"universo.max_monedas": 31}, "confirmar": True}, {"X-Panel-Token": "malo"},
                          ruta="/api/config")
        self.assertEqual(st, 403)
        with open(os.path.join(self.dir, "config.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["modo"], "simulado")

    def test_esquema(self):
        st, cuerpo, _ = self.get("/api/config")
        d = json.loads(cuerpo)
        self.assertEqual(st, 200)
        rutas = {c["ruta"] for c in d["campos"]}
        self.assertIn("filtros_ballenas.numero_ballenas", rutas)
        self.assertFalse(any(r.split(".")[0] in cp.PROHIBIDOS for r in rutas))
