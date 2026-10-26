import json
import os
import unittest
from unittest import mock

import laboratorio_semanal as ls
from tests.comunes import ConCarpeta


class Cambios(unittest.TestCase):
    def test_detecta_lo_relevante(self):
        c = ls.cambios({"a": "no_supera", "b": "supera", "c": "no_evaluable"},
                       {"a": "supera", "b": "no_supera", "c": "no_supera", "d": "no_supera"})
        self.assertEqual(len(c), 3)
        self.assertTrue(c[0].startswith("✅ a"))
        self.assertTrue(c[1].startswith("⚠️ b"))
        self.assertEqual(ls.cambios({"a": "no_supera"}, {"a": "no_supera"}), [])


class Ejecucion(ConCarpeta):
    def test_sin_cambios_no_avisa_y_registra(self):
        ruta = os.path.join(self.dir, "elegida.json")
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump({"resultados": {"x": {"estado_evaluacion": "no_supera"}}}, fh)
        with mock.patch.object(ls, "RUTA_ELEGIDA", ruta), mock.patch.object(ls, "DIR_REGISTRO", self.dir), \
                mock.patch.object(ls, "RUTA_BLOQUEO", os.path.join(self.dir, "lock")), \
                mock.patch.object(ls.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(ls, "avisar") as aviso:
            self.assertEqual(ls.main([]), 0)
        self.assertIn("--presupuesto", run.call_args[0][0])
        aviso.assert_not_called()
        with open(os.path.join(self.dir, "laboratorio_semanal.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["cambios"], [])
