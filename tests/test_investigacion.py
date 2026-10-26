import json
import os
import random
import unittest

import investigacion as inv
import vigilancia
from senales import Contexto
from tests.comunes import ConCarpeta

H = 3600 * 1000


def velas(n, paso=0.0, salto_en=None):
    out, px = [], 100.0
    for i in range(n):
        px *= 1 + paso + (0.003 if i % 2 else -0.003)
        if salto_en is not None and i == salto_en:
            px *= 1.06
        out.append({"t": i * H, "o": str(px / (1 + paso)), "h": str(px * 1.004), "l": str(px * 0.996), "c": str(px), "v": "1"})
    return out


class Gramatica(unittest.TestCase):
    def test_genera_ideas_validas_sin_repetir(self):
        rng = random.Random(1)
        ideas = inv.generar(rng, set(), 50)
        self.assertEqual(len({i["id"] for i in ideas}), 50)
        ya = {i["id"] for i in ideas[:10]}
        otras = inv.generar(random.Random(1), ya, 50)
        self.assertFalse(ya & {i["id"] for i in otras})
        for i in ideas:
            d = i["def"]
            if d["disparador"]["tipo"] == "cruce_medias":
                self.assertLess(d["disparador"]["rapida"], d["disparador"]["lenta"])
            self.assertTrue(inv.describir(d))

    def test_id_estable(self):
        d = {"disparador": {"tipo": "ruptura", "n": 20}, "filtros": [], "salida": "media", "sl_atr": 2.0, "intervalo": "1h"}
        self.assertEqual(inv.id_idea(d), inv.id_idea(json.loads(json.dumps(d))))


class Deteccion(unittest.TestCase):
    def test_ruptura_y_filtros(self):
        ctx = Contexto(velas(300, salto_en=280))
        d = {"disparador": {"tipo": "ruptura", "n": 20}, "filtros": [], "salida": "media", "sl_atr": 2.0, "intervalo": "1h"}
        s = inv.detectar(d, ctx, 280)
        self.assertEqual(s["lado"], "L")
        self.assertGreater(s["sl_pct"], 0)
        d2 = dict(d, filtros=[{"tipo": "a_favor_btc"}])
        self.assertIsNone(inv.detectar(d2, ctx, 280, btc_alcista=False))
        self.assertIsNotNone(inv.detectar(d2, ctx, 280, btc_alcista=True))
        self.assertIsNone(inv.detectar(d2, ctx, 280, btc_alcista=None))

    def test_no_mira_el_futuro(self):
        ctx_corto = Contexto(velas(281, salto_en=280))
        ctx_largo = Contexto(velas(300, salto_en=280))
        d = {"disparador": {"tipo": "vela_grande", "k": 1.5, "modo": "seguir"}, "filtros": [], "salida": "corta",
             "sl_atr": 2.0, "intervalo": "1h"}
        self.assertEqual(inv.detectar(d, ctx_corto, 280), inv.detectar(d, ctx_largo, 280))


class Aspirantes(ConCarpeta):
    def test_juicio_en_vivo(self):
        reg = {"probadas_total": 5, "ideas": {
            "a": {"def": {"disparador": {"tipo": "ruptura", "n": 20}, "filtros": [], "salida": "media", "sl_atr": 2.0,
                          "intervalo": "1h"}, "estado": "aspirante", "metricas": {}},
            "b": {"def": {"disparador": {"tipo": "ruptura", "n": 40}, "filtros": [], "salida": "media", "sl_atr": 2.0,
                          "intervalo": "1h"}, "estado": "aspirante", "metricas": {}}}}
        mem = [{"tipo": "aspirante", "modulo": "inv:a", "resultado_r": -0.5 + (i % 2) * 0.2} for i in range(30)]
        mem += [{"tipo": "aspirante", "modulo": "inv:b", "resultado_r": 0.6 + (i % 3) * 0.3} for i in range(30)]
        cambios = inv.juzgar_aspirantes(reg, mem)
        self.assertEqual(reg["ideas"]["a"]["estado"], "cementerio")
        self.assertEqual(reg["ideas"]["b"]["estado"], "lista_revision")
        self.assertEqual(len(cambios), 2)

    def test_memoria_no_aprende_de_aspirantes(self):
        from aprendizaje import Memoria
        from tests.comunes import config
        c = config()
        m = Memoria(os.path.join(self.dir, "ap.json"), c["aprendizaje"], c["salida"], (0, 0, 0))
        plan = {"precio": 100.0, "sl_pct": 1.0, "salida": c["salida"], "duracion_min": 60, "estrategia": "inv:a"}
        self.assertIsNotNone(m.abrir_sombra("ETH", "L", {}, [], plan, [], tipo="aspirante"))
        self.assertIsNone(m.abrir_sombra("ETH", "L", {}, [], plan, [], tipo="aspirante"))     # la misma, una vez
        self.assertIsNotNone(m.abrir_sombra("ETH", "L", {}, [], dict(plan, estrategia="inv:b"), [], tipo="aspirante"))
        self.assertEqual(m._peso_tipo("aspirante"), 0.0)


class Vigilancia(unittest.TestCase):
    def regs(self, n, media, modulo="x", tipo="real"):
        return [{"tipo": tipo, "modulo": modulo, "resultado_r": media + (0.3 if i % 2 else -0.3), "cierre_ts": 1e9 + i}
                for i in range(n)]

    def test_estados(self):
        ev = vigilancia.evaluar(self.regs(40, -0.5) + self.regs(40, 0.2, "y") + self.regs(5, -1, "z")
                                + self.regs(40, -0.02, "w") + self.regs(40, -9, "lab", "backtest"), ahora=1e9)
        self.assertEqual(ev["x"]["estado"], "degradada")
        self.assertEqual(ev["y"]["estado"], "ok")
        self.assertEqual(ev["z"]["estado"], "sin_datos")
        self.assertEqual(ev["w"]["estado"], "vigilar")
        self.assertNotIn("lab", ev)
        self.assertLess(ev["x"]["cota_superior"], 0)

    def test_correccion_por_estrategias_vigiladas(self):
        una = vigilancia.evaluar(self.regs(40, -0.1), ahora=1e9)["x"]
        muchas = vigilancia.evaluar(sum((self.regs(40, -0.1, f"m{i}") for i in range(10)), self.regs(40, -0.1)),
                                    ahora=1e9)["x"]
        self.assertGreater(muchas["nivel"], una["nivel"])
        self.assertGreaterEqual(muchas["cota_superior"], una["cota_superior"])

    def test_ventana(self):
        ev = vigilancia.evaluar(self.regs(40, -0.5), ahora=1e9 + 100 * 86400)
        self.assertEqual(ev, {})

    def test_fases_nunca_suben_de_golpe(self):
        t0 = 1e9
        f, _ = vigilancia.fases({}, {"x": {"estado": "degradada"}}, t0)
        self.assertEqual(f["x"]["factor"], 0.5)
        f, _ = vigilancia.fases(f, {"x": {"estado": "ok", "r_medio": 0.3}}, t0 + 3600)
        self.assertEqual(f["x"]["fase"], "recuperacion")
        f, _ = vigilancia.fases(f, {"x": {"estado": "vigilar", "r_medio": -0.1}}, t0 + 6 * 86400)   # recae: reinicia
        f, _ = vigilancia.fases(f, {"x": {"estado": "ok", "r_medio": 0.3}}, t0 + 10 * 86400)
        self.assertEqual(f["x"]["factor"], 0.5)
        f, _ = vigilancia.fases(f, {"x": {"estado": "ok", "r_medio": 0.3}}, t0 + 14 * 86400)
        self.assertEqual(f["x"]["factor"], 0.75)
        f, _ = vigilancia.fases(f, {"x": {"estado": "degradada"}}, t0 + 15 * 86400)
        self.assertEqual(f["x"]["factor"], 0.5)
