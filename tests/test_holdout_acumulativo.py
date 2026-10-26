"""Holdout acumulativo: lo no mirado se sella hasta reunir suficientes operaciones (no se gasta cada semana)."""

import os
import shutil
import tempfile
import unittest

from tests import comunes  # noqa: F401

import validacion as vd


def ops(ts, r=1.0):
    return [{"t": t, "r": r, "moneda": "BTC", "lado": "largo"} for t in ts]


class TestSellado(unittest.TestCase):
    def test_sella_si_no_hay_bastantes(self):
        visto, nuevo = ops(range(0, 20)), ops(range(100, 104))
        vis, fresco, sello = vd.sellar_holdout(visto + nuevo, nuevo, 10)
        self.assertFalse(sello["abierto"])
        self.assertEqual(sello["n_reservado"], 4)
        self.assertEqual(fresco, [])
        self.assertEqual(len(vis), 20)                       # lo sellado no aparece en ninguna estadística

    def test_abre_al_llegar_al_minimo(self):
        visto, nuevo = ops(range(0, 20)), ops(range(100, 112))
        vis, fresco, sello = vd.sellar_holdout(visto + nuevo, nuevo, 10)
        self.assertTrue(sello["abierto"])
        self.assertEqual((len(vis), len(fresco)), (32, 12))

    def test_se_acumula_semana_a_semana(self):
        """Con 4 operaciones nuevas por semana: antes se marcaba todo cada semana y nunca llegaba a 10.
        Ahora, sin marcar mientras está sellado, a la tercera semana hay 12 y se abre."""
        d = tempfile.mkdtemp()
        try:
            reg = vd.RegistroHoldout(os.path.join(d, "h.json"))
            reg.marcar("x_4h", 1000)                          # lo ya mirado hasta hoy
            todas = ops(range(0, 1000, 50))
            abierto_en = None
            for semana in range(1, 6):
                todas += ops(range(1000 + (semana - 1) * 100, 1000 + semana * 100, 25))   # 4 nuevas
                fresco = reg.fresco(todas, "x_4h", "4h")
                _, _, sello = vd.sellar_holdout(todas, fresco, vd.min_holdout())
                if sello["abierto"]:
                    abierto_en = semana
                    reg.marcar("x_4h", 1000 + semana * 100)
                    break
            self.assertEqual(abierto_en, 3)
            self.assertEqual(reg.visto_hasta("x_4h"), 1300)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_veredicto_sellado_no_supera_nunca(self):
        """Sellado = sin confirmación independiente: como mucho 'exploratorio', nunca 'supera'."""
        oos = [{"t": i, "r": 1.0 if i % 3 else 0.8, "moneda": "BTC", "lado": "largo"} for i in range(200)]
        visto = ops(range(1000, 1030), 1.0)
        vis, fresco, sello = vd.sellar_holdout(visto + ops(range(2000, 2005)), ops(range(2000, 2005)), 10)
        lab = {"bootstrap_iteraciones": 200, "bloque_bootstrap": 10, "semilla": 1}
        v = vd.veredicto(oos, vis, 1, lab, None, None, None, holdout_fresco=fresco)
        self.assertNotEqual(v["estado"], "supera")


class TestSaldoSimulado(unittest.TestCase):
    def _cuenta(self, ruta, saldo):
        import papel
        return papel.CuentaPapel(None, ruta, saldo, 0.00015, 0.00045)

    def test_adopta_saldo_nuevo_solo_sin_estrenar(self):
        import json
        d = tempfile.mkdtemp()
        try:
            ruta = os.path.join(d, "papel.json")
            self._cuenta(ruta, 58)
            c = self._cuenta(ruta, 232)
            self.assertEqual((c.d["efectivo"], c.d["saldo_inicial"]), (232, 232))
            with open(ruta, encoding="utf-8") as fh:
                guardado = json.load(fh)
            guardado["fills"] = [{"x": 1}]
            with open(ruta, "w", encoding="utf-8") as fh:
                json.dump(guardado, fh)
            c = self._cuenta(ruta, 500)                     # ya tiene historia: no se toca
            self.assertEqual(c.d["saldo_inicial"], 232)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class TestRebaseCapital(unittest.TestCase):
    def test_deposito_no_es_beneficio(self):
        from riesgo import rebasar_capital
        e = {"saldo_inicial": 58.0, "saldo_inicio_dia": 58.0, "saldo_inicio_semana": 60.0, "saldo_maximo": 232.3,
             "historial_saldo": [[1, 58.0], [2, 59.0], [3, 232.1]]}
        self.assertEqual(rebasar_capital(e, 232.0), 174.0)
        self.assertEqual((e["saldo_inicial"], e["saldo_inicio_dia"], e["saldo_inicio_semana"], e["saldo_maximo"]),
                         (232.0, 232.0, 234.0, 232.3))   # el máximo ya era posterior
        self.assertEqual(e["historial_saldo"], [[1, 232.0], [2, 233.0], [3, 232.1]])
        self.assertEqual(rebasar_capital(e, 232.0), 0.0)          # idempotente
        self.assertEqual(rebasar_capital({}, 232.0), 0.0)          # estado nuevo: nada que ajustar


class TestRebaseConEvento(unittest.TestCase):
    def test_con_hora_del_evento_solo_mueve_lo_anterior(self):
        from riesgo import rebasar_capital, ventana_utc
        ts_cambio = 1_792_750_000
        dia, semana = ventana_utc(ts_cambio - 3600)
        e = {"saldo_inicial": 58.0, "saldo_inicio_dia": 58.0, "saldo_inicio_semana": 58.0, "saldo_maximo": 240.0,
             "dia": dia, "semana": semana,
             "historial_saldo": [[ts_cambio - 100, 58.0], [ts_cambio + 100, 240.0], [ts_cambio + 200, 231.0]]}
        self.assertEqual(rebasar_capital(e, 232.0, ts_cambio), 174.0)
        self.assertEqual(e["historial_saldo"], [[ts_cambio - 100, 232.0], [ts_cambio + 100, 240.0], [ts_cambio + 200, 231.0]])
        self.assertEqual((e["saldo_inicio_dia"], e["saldo_inicio_semana"], e["saldo_maximo"]), (232.0, 232.0, 240.0))
