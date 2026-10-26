import io
import unittest
from contextlib import redirect_stdout

import comprobar


class Comprobar(unittest.TestCase):
    def setUp(self):
        comprobar.RESULTADOS.clear()

    def test_salud_desconocida_no_es_correcta(self):
        with redirect_stdout(io.StringIO()) as out:
            comprobar.anotar(None, "salud desconocida", critico=False)
        self.assertIn("?", out.getvalue())
        self.assertNotIn("✓", out.getvalue())
        self.assertIs(comprobar.RESULTADOS[0][0], None)
