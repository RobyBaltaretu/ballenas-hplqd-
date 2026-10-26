import json
import os

import archivo
from tests.comunes import ConCarpeta, MercadoFalso


def vela(t, c=100.0):
    return {"t": t, "T": t + 3_599_999, "o": str(c), "h": str(c + 1), "l": str(c - 1), "c": str(c), "v": "10", "n": 5}


class Archivo(ConCarpeta):
    def setUp(self):
        super().setUp()
        self.a = archivo.Archivo(self.dir, {"velas": True, "libro": True, "libro_cada_min": 5, "libro_niveles": 3})

    def test_velas_sin_repetir_y_persistentes(self):
        self.assertEqual(self.a.guardar_velas("BTC", "1h", [vela(0), vela(3_600_000)]), 2)
        self.assertEqual(self.a.guardar_velas("BTC", "1h", [vela(3_600_000), vela(7_200_000)]), 1)
        otro = archivo.Archivo(self.dir, {"velas": True})          # reinicio del bot
        self.assertEqual(otro.guardar_velas("BTC", "1h", [vela(7_200_000)]), 0)
        self.assertEqual([int(v["t"]) for v in archivo.leer_velas(self.dir, "BTC", "1h")], [0, 3_600_000, 7_200_000])

    def test_linea_cortada_se_ignora(self):
        self.a.guardar_velas("ETH", "1h", [vela(0)])
        with open(os.path.join(self.dir, "velas", "1h", "ETH.jsonl"), "a", encoding="utf-8") as fh:
            fh.write('{"t": 36')
        self.assertEqual(len(archivo.leer_velas(self.dir, "ETH", "1h")), 1)

    def test_combinar_prefiere_lo_descargado(self):
        api = [vela(3_600_000, 200.0)]
        propias = [vela(0, 100.0), vela(3_600_000, 150.0)]
        r = archivo.combinar(api, propias)
        self.assertEqual([v["c"] for v in r], ["100.0", "200.0"])

    def test_libro_y_desequilibrio(self):
        m = MercadoFalso()
        self.assertTrue(self.a.foto_libro(m, ["BTC", "ETH"], ahora=1_800_000_000, en_segundo_plano=False))
        self.assertFalse(self.a.foto_libro(m, ["BTC"], ahora=1_800_000_060, en_segundo_plano=False))   # aún no toca
        fotos = archivo.leer_libro(self.dir)
        self.assertEqual({f["m"] for f in fotos}, {"BTC", "ETH"})
        self.assertAlmostEqual(archivo.desequilibrio({"b": [[10, 3]], "a": [[10, 1]]}), 0.5)
        self.assertIsNone(archivo.desequilibrio({"b": [], "a": []}))
        self.assertEqual(self.a.resumen()["dias_libro"], 1)

    def test_libro_desactivado(self):
        a = archivo.Archivo(self.dir, {"libro": False})
        self.assertFalse(a.foto_libro(MercadoFalso(), ["BTC"], en_segundo_plano=False))
        self.assertFalse(any(f.startswith("libro_") for f in os.listdir(self.dir)))
        json.dumps(a.resumen())
