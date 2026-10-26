"""Regresión del laboratorio (bloque C): sin falsos positivos por línea base vacía, muestra insuficiente, holdout
reutilizado, operaciones que cruzan particiones o umbrales mal calculados. Sin red."""

import json
import os
import random
import unittest

import validacion as vd
from tests.comunes import ConCarpeta, config

LAB = config()["laboratorio"]
DIA = 86400


def ops(n, media=0.4, t0=0, paso=DIA, semilla=1, dur=3600, lado=None):
    rng = random.Random(semilla)
    return [{"t": t0 + i * paso, "cierre_ts": t0 + i * paso + dur, "r": media + rng.uniform(-1, 1),
             "moneda": "ETH", "lado": lado or ("L" if i % 2 else "S"), "sl_pct": 2.0, "salida": "objetivo"}
            for i in range(n)]


class Veredicto(unittest.TestCase):
    def base_ok(self):
        oos = ops(120, 0.6)
        return oos, ops(40, 0.6, t0=200 * DIA, semilla=2), {"aleatoria": ops(120, 0.0, semilla=3)}

    def test_resultado_claro_supera(self):
        oos, ho, bases = self.base_ok()
        self.assertEqual(vd.veredicto(oos, ho, 10, LAB, bases)["estado"], "supera")

    def test_linea_base_vacia_o_insuficiente_no_aprueba(self):
        oos, ho, _ = self.base_ok()
        for base in ([], ops(5, -1.0)):
            v = vd.veredicto(oos, ho, 10, LAB, {"aleatoria": base})
            self.assertEqual(v["estado"], "no_evaluable", len(base))
            self.assertIn("línea base", v["motivo"])

    def test_muestra_insuficiente_no_evaluable(self):
        _, ho, bases = self.base_ok()
        self.assertEqual(vd.veredicto(ops(20, 2.0), ho, 1, LAB, bases)["estado"], "no_evaluable")
        oos, _, bases = self.base_ok()
        self.assertEqual(vd.veredicto(oos, ops(3, 2.0), 1, LAB, bases)["estado"], "no_evaluable")   # holdout corto

    def test_holdout_reutilizado_no_supera(self):
        oos, ho, bases = self.base_ok()
        v = vd.veredicto(oos, ho, 10, LAB, bases, holdout_fresco=[])
        self.assertEqual(v["estado"], "exploratorio")
        self.assertFalse(v["holdout_independiente"])

    def test_escenario_adverso_decide(self):
        oos = ops(120, 0.05)                    # ventaja diminuta: no aguanta costes al doble
        esc = vd.escenarios_estres(oos, 0.045, 0.05)
        v = vd.veredicto(oos, ops(40, 0.6, t0=300 * DIA), 1, LAB, {"aleatoria": ops(120, -0.5)}, escenarios=esc)
        self.assertNotEqual(v["estado"], "supera")
        self.assertEqual(set(esc), {"costes_x2", "deslizamiento_x3", "huecos_en_stops", "combinado"})


class Estadistica(unittest.TestCase):
    def test_t_student_valores_conocidos(self):
        self.assertAlmostEqual(vd.t_critico(9, 0.05), 1.8331, places=3)
        self.assertAlmostEqual(vd.t_critico(29, 0.05), 1.6991, places=3)
        self.assertAlmostEqual(vd.t_critico(29, 0.025), 2.0452, places=3)
        # con pocas operaciones la t de Student exige más que la normal
        self.assertGreater(vd.t_requerido(50, n_ops=30), vd.t_requerido(50))
        # más variantes -> más exigente
        self.assertGreater(vd.t_requerido(500, n_ops=60), vd.t_requerido(5, n_ops=60))


class Purga(unittest.TestCase):
    def test_operacion_que_cruza_la_particion_se_purga(self):
        dentro = {"t": 10, "cierre_ts": 50, "r": 1}
        cruza = {"t": 90, "cierre_ts": 130, "r": 1}           # abre en el tramo, cierra en el siguiente
        en_embargo = {"t": 80, "cierre_ts": 98, "r": 1}
        tr = vd.en_tramo_purgado([dentro, cruza, en_embargo], 0, 100, embargo_seg=5)
        self.assertEqual(tr, [dentro])
        self.assertEqual(vd.entrenamiento_purgado([dentro, cruza], 100, 5), [dentro])

    def test_walk_forward_no_cuenta_operaciones_que_entran_en_el_holdout(self):
        import laboratorio as lab
        part = vd.particionar(0, 1000 * 3600, pliegues=2, fraccion_holdout=0.2)
        ini_h = part["holdout"][0]
        trades = [{"t": t, "cierre_ts": t + 50 * 3600, "r": 1.0, "moneda": "ETH", "lado": "L"} for t in
                  range(0, 1000 * 3600, 20 * 3600)]
        _, oos, holdout, _, _ = lab.walk_forward([({"p": 1}, trades)], part, 3600)
        self.assertTrue(all(t["cierre_ts"] <= ini_h - 3600 for t in oos))
        self.assertTrue(all(t["t"] >= ini_h for t in holdout))

    def test_linea_base_no_sale_de_la_particion(self):
        def sim(moneda, t, lado, sl):
            return {"r": 0.1, "cierre_ts": t + 2 * DIA}
        oos = ops(50, t0=0, paso=DIA // 2)
        base = vd.linea_base_aleatoria(oos, sim, 1, t_min=0, t_max=26 * DIA)
        self.assertTrue(all(b["cierre_ts"] <= 26 * DIA for b in base))
        self.assertTrue(all(0 <= b["t"] < 26 * DIA for b in base))


class Registros(ConCarpeta):
    def test_holdout_de_un_solo_uso(self):
        r = vd.RegistroHoldout(os.path.join(self.dir, "h.json"))
        ho = ops(40, t0=1000)
        self.assertEqual(len(r.fresco(ho, "ruptura_1h", "1h")), 40)
        r.marcar("ruptura_1h", 1000 + 40 * DIA)
        r.guardar()
        r2 = vd.RegistroHoldout(os.path.join(self.dir, "h.json"))
        self.assertEqual(r2.fresco(ho, "ruptura_1h", "1h"), [])
        nuevos = ops(5, t0=1000 + 41 * DIA)
        self.assertEqual(len(r2.fresco(ho + nuevos, "ruptura_1h", "1h")), 5)

    def test_migracion_desde_formato_antiguo_es_conservadora_y_reversible(self):
        ruta = os.path.join(self.dir, "h.json")
        with open(ruta, "w") as fh:
            json.dump({"1h:2026-08-01": 3}, fh)
        r = vd.RegistroHoldout(ruta)
        self.assertEqual(r.d["usos"], {"1h:2026-08-01": 3})                  # se conserva lo anterior
        self.assertEqual(r.fresco(ops(10, t0=0), "cualquiera_1h", "1h"), [])  # lo ya mirado no es fresco

    def test_variantes_acumuladas(self):
        r = vd.RegistroVariantes(os.path.join(self.dir, "v.json"))
        ids = [vd.RegistroVariantes.id_variante("lab", {"p": i}) for i in range(10)]
        self.assertEqual(r.añadir(ids), 10)
        self.assertEqual(r.añadir(ids), 10)                  # repetir no suma
        self.assertEqual(r.añadir(["inv:x"]), 11)


class Estudios(unittest.TestCase):
    def test_aportacion_y_solapamiento(self):
        import estudios
        a = ops(40, 0.3, semilla=1)
        copia = [dict(t) for t in a]                          # misma señal: solapa del todo
        b = ops(40, -0.2, t0=10 * DIA, semilla=5, lado="L")
        inf = estudios.aportacion({"fuerza_4h": a, "momentum_4h": copia, "otra": b})
        self.assertEqual(inf["estrategias"]["fuerza_4h"]["solapamiento"]["momentum_4h"], 1.0)
        self.assertLess(inf["estrategias"]["otra"]["marginal"]["r_total_aportado"], 0)
        self.assertEqual(set(inf["consideradas"]), {"fuerza_4h", "momentum_4h", "otra"})
