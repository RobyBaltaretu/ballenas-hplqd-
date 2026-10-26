import copy
import json
import os

from tests.comunes import ConCarpeta, conf, config

SUB = "0x" + "ab" * 20


def base(modo="simulado"):
    c = copy.deepcopy(conf.DEFECTOS)
    c["modo"] = modo
    return c


class Modos(ConCarpeta):
    def test_simulado_por_defecto_valido(self):
        self.assertEqual(config()["modo"], "simulado")

    def test_paper_es_sinonimo_de_simulado(self):
        self.assertEqual(conf.validar(base("paper"), {})["modo"], "simulado")

    def test_sin_modo_falla(self):
        c = base()
        del c["modo"]
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(c, {})

    def test_modo_desconocido_falla(self):
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(base("vivo"), {})

    def test_contradiccion_entorno(self):
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(base("simulado"), {"HL_MODO": "real"})

    def test_shadow_no_necesita_claves(self):
        self.assertEqual(conf.validar(base("shadow"), {})["modo"], "shadow")


class ModoReal(ConCarpeta):
    def entorno(self, **k):
        e = {"BOT_REAL_CONFIRMADO": "SI", "HL_DIRECCION_CUENTA": SUB, "HL_API_WALLET_KEY": "x"}
        e.update(k)
        return e

    def real(self):
        c = base("real")
        c["real"] = {"habilitado": True, "subcuenta": SUB}
        return c

    def test_real_sin_habilitar_falla(self):
        c = base("real")
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(c, self.entorno())

    def test_real_sin_confirmacion_de_entorno_falla(self):
        e = self.entorno()
        del e["BOT_REAL_CONFIRMADO"]
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(self.real(), e)

    def test_real_con_otra_cuenta_falla(self):
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(self.real(), self.entorno(HL_DIRECCION_CUENTA="0x" + "cd" * 20))

    def test_real_completo_valida(self):
        self.assertEqual(conf.validar(self.real(), self.entorno())["modo"], "real")

    def test_habilitado_con_modo_no_real_falla(self):
        c = base("simulado")
        c["real"] = {"habilitado": True, "subcuenta": SUB}
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(c, {})

    def test_confirmacion_real_en_modo_no_real_falla(self):
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(base("shadow"), {"BOT_REAL_CONFIRMADO": "SI"})


class Valores(ConCarpeta):
    def test_numero_no_finito(self):
        c = base()
        c["riesgo"]["riesgo_por_operacion_pct"] = float("nan")
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(c, {})

    def test_riesgo_negativo(self):
        c = base()
        c["riesgo"]["perdida_diaria_max_pct"] = -1
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(c, {})

    def test_panel_expuesto_sin_contrasena(self):
        c = base()
        c["panel"]["host"] = "0.0.0.0"
        with self.assertRaises(conf.ErrorConfig):
            conf.validar(c, {})

    def test_sin_secretos(self):
        limpio = conf.sin_secretos({"HL_API_WALLET_KEY": "0xabc", "telegram": {"token": "123"}, "riesgo": 1})
        self.assertNotIn("0xabc", json.dumps(limpio))
        self.assertNotIn("123", json.dumps(limpio))
        self.assertEqual(limpio["riesgo"], 1)


class Migracion(ConCarpeta):
    def test_v1_a_v2_con_copia_y_sin_perder_valores(self):
        v1 = {"modo": "simulado", "riesgo_por_operacion_pct": 0.7, "max_posiciones_abiertas": 2,
              "max_operaciones_dia": 6, "comision_taker_pct": 0.05, "comision_maker_pct": 0.02,
              "duracion_max_min": 600, "filtros_entrada": {"sl_min_pct": 0.3, "funding_max_en_contra_pct": 0.01}}
        ruta = os.path.join(self.dir, "config.json")
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(v1, fh)
        cambios = conf.migrar_archivo(ruta)
        self.assertTrue(cambios)
        copias = [f for f in os.listdir(self.dir) if f.startswith("config.json.v1-") and f.endswith(".bak")]
        self.assertEqual(len(copias), 1)
        with open(os.path.join(self.dir, copias[0]), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), v1)          # la copia es exactamente el original
        nuevo = conf.validar(conf.cargar(ruta), {})
        self.assertEqual(nuevo["riesgo"]["riesgo_por_operacion_pct"], 0.7)
        self.assertEqual(nuevo["riesgo"]["max_posiciones_abiertas"], 2)
        self.assertEqual(nuevo["comisiones"]["taker_pct"], 0.05)
        self.assertEqual(nuevo["filtros_entrada"]["funding_max_en_contra_pct"], 0.01)
        self.assertNotIn("sl_min_pct", nuevo["filtros_entrada"])
        self.assertEqual(conf.migrar_archivo(ruta), [])    # idempotente


class NombresNoSecretos(ConCarpeta):
    def test_clave_de_estrategia_no_se_oculta(self):
        d = conf.sin_secretos({"clave": "ruptura_1h", "clave_privada": "0xabc", "api_key": "x"})
        self.assertEqual(d["clave"], "ruptura_1h")
        self.assertEqual(d["clave_privada"], "***")
        self.assertEqual(d["api_key"], "***")
