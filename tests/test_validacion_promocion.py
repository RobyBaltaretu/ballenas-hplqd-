import os
import unittest

import validacion as vd
from promocion import Registro, huella
from tests.comunes import ConCarpeta, config, evidencia_para_apta


def trades(n, t0=0, paso=3600, dur=7200, r=0.1):
    return [{"t": t0 + i * paso, "cierre_ts": t0 + i * paso + dur, "r": r if i % 3 else -r / 2, "moneda": "ETH",
             "lado": "L", "sl_pct": 1.0} for i in range(n)]


class SinFugas(unittest.TestCase):
    def test_particiones_ordenadas_y_holdout_al_final(self):
        p = vd.particionar(0, 1000, 5, 0.15)
        tramos = p["pliegues"]
        self.assertEqual(len(tramos), 5)
        for (a, b), (c, d) in zip(tramos, tramos[1:]):
            self.assertAlmostEqual(b, c)
        self.assertAlmostEqual(tramos[-1][1], p["holdout"][0])
        self.assertAlmostEqual(p["holdout"][1], 1000)

    def test_purga_y_embargo(self):
        ts = trades(100)
        inicio = 50 * 3600
        ent = vd.entrenamiento_purgado(ts, inicio, embargo_seg=3 * 3600)
        self.assertTrue(all(t["cierre_ts"] <= inicio - 3 * 3600 for t in ent))
        self.assertTrue(all(t["t"] < inicio for t in ent))
        # una operación que abrió antes pero cerró dentro del tramo de prueba queda fuera
        self.assertNotIn(ts[49], ent)

    def test_walk_forward_elige_con_el_pasado(self):
        import laboratorio as lab
        buenas_antes = [dict(t, r=1.0) if t["t"] < 500 * 3600 else dict(t, r=-1.0) for t in trades(1000)]
        malas_antes = [dict(t, r=-1.0) if t["t"] < 500 * 3600 else dict(t, r=1.0) for t in trades(1000)]
        part = vd.particionar(0, 1000 * 3600, 5, 0.15)
        params, oos, ho, elecciones, _ = lab.walk_forward([({"a": 1}, buenas_antes), ({"a": 2}, malas_antes)], part, 7200)
        self.assertEqual(elecciones[0]["params"], {"a": 1})     # en el primer tramo solo conoce el pasado
        self.assertTrue(all(t["t"] >= part["pliegues"][0][0] for t in oos))
        self.assertTrue(all(t["t"] >= part["holdout"][0] for t in ho))

    def test_bootstrap_reproducible(self):
        xs = [t["r"] for t in trades(200)]
        self.assertEqual(vd.bootstrap_bloques(xs, 10, 300, 7), vd.bootstrap_bloques(xs, 10, 300, 7))

    def test_correccion_por_variantes(self):
        self.assertGreater(vd.t_requerido(200), vd.t_requerido(10))
        self.assertGreater(vd.t_requerido(10), vd.t_requerido(1))

    def test_t_por_dias_frena_operaciones_amontonadas(self):
        lab = config()["laboratorio"]
        # 200 operaciones buenas pero TODAS en 3 días: la t por operación es alta, la t por días no.
        ts = [dict(t, t=(i % 3) * 86400 + i, cierre_ts=(i % 3) * 86400 + i + 60) for i, t in enumerate(trades(200))]
        ho = [dict(t, t=t["t"] + 10 * 86400, cierre_ts=t["cierre_ts"] + 10 * 86400) for t in trades(40)]
        v = vd.veredicto(ts, ho, 1, lab)
        regla = next(c for c in v["comprobaciones"] if c["regla"].startswith("t por días"))
        self.assertFalse(regla["cumple"])
        self.assertNotEqual(v["estado"], "supera")

    def test_pocas_operaciones_no_evaluable(self):
        lab = config()["laboratorio"]
        self.assertEqual(vd.veredicto(trades(5), [], 10, lab)["estado"], "no_evaluable")

    def test_universo_en_el_momento(self):
        vols = {"A": [(d * 86400, 100.0) for d in range(60)], "B": [(d * 86400, 1.0 if d < 40 else 1000.0) for d in range(60)]}
        esta = vd.universo_en_el_tiempo(vols, 1, 10 * 86400)
        self.assertTrue(esta("A", 20 * 86400))
        self.assertFalse(esta("B", 20 * 86400))      # B aún no era líquida entonces
        self.assertTrue(esta("B", 55 * 86400))

    def test_cartera_respeta_limites(self):
        c = config()
        ts = [{"t": 0, "cierre_ts": 10_000, "r": 1, "moneda": f"M{i}", "lado": "L"} for i in range(10)]
        res = vd.cartera_produccion(ts, c["riesgo"])
        self.assertLessEqual(res["operaciones"], c["riesgo"]["max_posiciones_abiertas"])


class Promocion(ConCarpeta):
    def setUp(self):
        super().setUp()
        self.reg = Registro(os.path.join(self.dir, "promociones.json"))
        self.c = config()
        self.e = {"clave": "ruptura_1h", "version": "1", "params": {"n": 20}, "salida": {}, "barras_max": 30}
        self.h = huella(self.e, self.c, codigo="abc")

    def test_laboratorio_no_cambia_la_etapa_ni_autoriza_real(self):
        self.reg.registrar_evidencia("ruptura_1h", "laboratorio", self.h, "lab-1", {"estado": "supera"})
        self.assertEqual(self.reg.d["estrategias"]["ruptura_1h"]["etapa"], "investigacion")
        self.assertFalse(self.reg.autorizada_real("ruptura_1h", self.h))
        self.assertEqual(self.reg.vigente("ruptura_1h", self.h)["etapa"], "laboratorio")

    def test_lab_no_superado_no_cuenta(self):
        self.reg.registrar_evidencia("ruptura_1h", "laboratorio", self.h, "lab-1", {"estado": "no_supera"})
        self.assertEqual(self.reg.vigente("ruptura_1h", self.h)["etapa"], "investigacion")
        with self.assertRaises(ValueError):
            self.reg.aprobar("ruptura_1h", "apta_revision", "Roberto", self.h)

    def test_real_exige_pasos_previos(self):
        with self.assertRaises(ValueError):
            self.reg.aprobar("ruptura_1h", "real_habilitado", "Roberto", self.h)
        self.reg.registrar_evidencia("ruptura_1h", "laboratorio", self.h, "lab-1", {"estado": "supera"})
        with self.assertRaises(ValueError):
            self.reg.aprobar("ruptura_1h", "apta_revision", "Roberto", self.h)      # falta shadow/paper
        evidencia_para_apta(self.reg, "ruptura_1h", self.h)
        self.reg.aprobar("ruptura_1h", "apta_revision", "Roberto", self.h)
        self.reg.aprobar("ruptura_1h", "real_habilitado", "Roberto", self.h)
        self.assertTrue(self.reg.autorizada_real("ruptura_1h", self.h))

    def test_una_sola_operacion_paper_no_basta(self):
        self.reg.registrar_evidencia("ruptura_1h", "laboratorio", self.h, "lab-1", {"estado": "supera"})
        self.reg.registrar_evidencia("ruptura_1h", "paper", self.h, "t1", {"r_neto": 2.0, "pnl_neto": 5.0})
        with self.assertRaises(ValueError) as e:
            self.reg.aprobar("ruptura_1h", "apta_revision", "Roberto", self.h)
        self.assertIn("operaciones", str(e.exception))
        faltan = self.reg.progreso("ruptura_1h", self.h)["requisitos_apta"]["faltan"]
        self.assertTrue(any("días de observación" in f for f in faltan))

    def test_evidencia_invalida_o_de_otra_huella_no_cuenta(self):
        self.reg.registrar_evidencia("ruptura_1h", "paper", self.h, "t1", {"r_neto": 1.0, "conciliacion_ok": False})
        self.reg.registrar_evidencia("ruptura_1h", "paper", self.h, "t2", {})                     # sin resultado
        self.reg.registrar_evidencia("ruptura_1h", "paper", "otra", "t3", {"r_neto": 1.0})
        a = self.reg._acumulado("ruptura_1h", self.h)
        self.assertEqual(a["n"], 0)
        self.assertEqual(a["invalidas"], 2)

    def test_resultado_negativo_o_lab_viejo_no_pasan(self):
        evidencia_para_apta(self.reg, "ruptura_1h", self.h, r=-0.3)
        with self.assertRaises(ValueError):
            self.reg.aprobar("ruptura_1h", "apta_revision", "R", self.h)
        reg2 = Registro(os.path.join(self.dir, "p2.json"))
        evidencia_para_apta(reg2, "ruptura_1h", self.h)
        reg2.d["estrategias"]["ruptura_1h"]["evidencias"][0]["ts"] = "2020-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            reg2.aprobar("ruptura_1h", "apta_revision", "R", self.h)

    def test_lab_exploratorio_no_vale(self):
        evidencia_para_apta(self.reg, "ruptura_1h", self.h)
        self.reg.registrar_evidencia("ruptura_1h", "laboratorio", self.h, "lab-2", {"estado": "exploratorio"})
        with self.assertRaises(ValueError):
            self.reg.aprobar("ruptura_1h", "apta_revision", "R", self.h)

    def test_cambio_de_huella_invalida(self):
        evidencia_para_apta(self.reg, "ruptura_1h", self.h)
        self.reg.aprobar("ruptura_1h", "apta_revision", "R", self.h)
        self.reg.aprobar("ruptura_1h", "real_habilitado", "R", self.h)
        otro = huella(dict(self.e, params={"n": 25}), self.c, codigo="abc")
        self.assertFalse(self.reg.autorizada_real("ruptura_1h", otro))
        self.assertTrue(self.reg.vigente("ruptura_1h", otro)["invalidada"])
        self.assertNotEqual(huella(self.e, self.c, codigo="otro-codigo"), self.h)
        self.assertFalse(self.reg.autorizada_real("ruptura_1h", None))      # huella no calculable: nunca

    def test_aprobar_exige_quien(self):
        with self.assertRaises(ValueError):
            self.reg.aprobar("ruptura_1h", "laboratorio", "  ", self.h)


class FiltroAprendizaje(unittest.TestCase):
    def regs(self, n_ac, r_ac, n_re, r_re):
        out = []
        for i in range(n_ac):
            out.append({"tipo": "sombra", "resultado_r": r_ac + (0.5 if i % 2 else -0.5),
                        "juicio_aprendizaje": {"abrir": True, "juzgada": True}})
        for i in range(n_re):
            out.append({"tipo": "real", "resultado_r": r_re + (0.5 if i % 2 else -0.5),
                        "juicio_aprendizaje": {"abrir": False, "juzgada": True}})
        out.append({"tipo": "backtest", "resultado_r": 9, "juicio_aprendizaje": {"abrir": True, "juzgada": True}})
        out.append({"tipo": "sombra", "resultado_r": 9, "juicio_aprendizaje": {"abrir": True, "juzgada": False}})
        return out

    def test_sin_datos(self):
        from aprendizaje import evaluar_filtro
        self.assertEqual(evaluar_filtro(self.regs(5, 1, 5, -1))["veredicto"], "sin_datos")

    def test_aporta_resta_y_no_se_distingue(self):
        from aprendizaje import evaluar_filtro
        self.assertEqual(evaluar_filtro(self.regs(40, 0.5, 40, -0.5))["veredicto"], "aporta")
        self.assertEqual(evaluar_filtro(self.regs(40, -0.5, 40, 0.5))["veredicto"], "resta")
        self.assertEqual(evaluar_filtro(self.regs(40, 0.0, 40, 0.0))["veredicto"], "no_se_distingue")
        ev = evaluar_filtro(self.regs(40, 0.5, 40, -0.5))
        self.assertEqual(ev["aceptadas"]["n"], 40)          # ni backtest ni no juzgadas cuentan


class Huella(ConCarpeta):
    """La huella cambia ante CUALQUIER cambio material y falla cerrada si no se puede calcular."""

    def setUp(self):
        super().setUp()
        import shutil

        import promocion
        self.p = promocion
        self.c = config()
        self.e = {"clave": "ruptura_1h", "modulo": "ruptura", "intervalo": "1h", "version": "1", "params": {"n": 20},
                  "salida": {}, "barras_max": 30}
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in promocion.ARCHIVOS_CODIGO:
            shutil.copy(os.path.join(raiz, f), os.path.join(self.dir, f))
        self.base = promocion.huella(self.e, self.c, codigo=promocion.hash_codigo(self.dir))

    def h(self, cfg=None, carpeta=None):
        return self.p.huella(self.e, cfg or self.c, codigo=self.p.hash_codigo(carpeta or self.dir))

    def test_cada_archivo_de_codigo_cambia_la_huella(self):
        for f in self.p.ARCHIVOS_CODIGO:
            ruta = os.path.join(self.dir, f)
            with open(ruta, "rb") as fh:
                original = fh.read()
            with open(ruta, "ab") as fh:
                fh.write(b"\n# cambio\n")
            self.assertNotEqual(self.h(), self.base, f)
            with open(ruta, "wb") as fh:
                fh.write(original)
        self.assertEqual(self.h(), self.base)

    def test_cada_limite_de_riesgo_y_orden_cambia_la_huella(self):
        import copy
        for bloque in ("riesgo", "piloto_real", "ordenes", "salida", "comisiones", "universo", "filtros_entrada",
                       "aprendizaje", "gestion_capital", "motor", "ballenas"):
            for k, v in self.c[bloque].items():
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                c2 = copy.deepcopy(self.c)
                c2[bloque][k] = v + 1
                self.assertNotEqual(self.h(c2), self.base, f"{bloque}.{k}")

    def test_lo_que_no_decide_no_cambia_la_huella(self):
        import copy
        c2 = copy.deepcopy(self.c)
        c2["panel"]["puerto"] = 9999
        c2["modo"] = "shadow"
        c2["saldo_simulado_usd"] = 1234
        self.assertEqual(self.h(c2), self.base)

    def test_falta_un_archivo_falla_cerrado(self):
        os.remove(os.path.join(self.dir, "riesgo.py"))
        with self.assertRaises(self.p.ErrorHuella):
            self.p.hash_codigo(self.dir)
        self.assertIsNone(self.p.huella_segura(self.e, {}, codigo="x"))       # config incompleta

    def test_todo_archivo_py_esta_clasificado(self):
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sueltos = {f for f in os.listdir(raiz) if f.endswith(".py")} - set(self.p.ARCHIVOS_CODIGO) - \
            set(self.p.ARCHIVOS_SIN_EFECTO)
        self.assertEqual(sueltos, set(), "clasifica estos archivos en promocion.py")

    def test_la_huella_no_contiene_secretos(self):
        import copy
        c2 = copy.deepcopy(self.c)
        c2["real"]["subcuenta"] = "0x" + "ab" * 20
        self.assertEqual(self.h(c2), self.base)            # la subcuenta no entra
        self.assertEqual(len(self.base), 20)
