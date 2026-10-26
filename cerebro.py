"""
«Cerebro»: un único modelo que decide qué posibles entradas merecen operarse, con TODOS los datos
(Hyperliquid + Binance + Bybit, ver mercado_unificado.py).

Cómo funciona:
  - Las estrategias ya no compiten entre sí: proponen candidatas. Cada candidata lleva sus rasgos de mercado y
    un indicador de qué estrategia la propuso.
  - Un modelo LINEAL con penalización (ridge), sencillo a propósito para no sobreajustar, estima el resultado
    esperado en R. Se opera solo si la estimación supera un umbral FIJADO DE ANTEMANO (no se optimiza).
  - Se entrena por tiempo (walk-forward): para decidir en un tramo solo usa operaciones que CERRARON antes de
    que empiece ese tramo, menos un embargo. Nunca ve el futuro.
  - Un rasgo sin dato vale 0 tras estandarizar (= «ni a favor ni en contra») y se añade un indicador de «falta».

En el laboratorio se compara: todas las candidatas · cerebro solo con Hyperliquid · cerebro con todo. Así se ve
si los datos de Binance y Bybit aportan algo de verdad. Nada de esto activa nada en real.
"""

import math

from mercado_unificado import RASGOS, RASGOS_HL

UMBRAL_R = 0.05          # se opera si el resultado esperado (en R) supera esto. Fijo: no se ajusta con resultados.
LAMBDA = 10.0            # penalización ridge (fija)
R_TOPE = (-1.5, 4.0)     # el objetivo se recorta para que dos operaciones extremas no manden
MIN_ENTRENO = 60


def _resolver(a, b):
    """Resuelve a·x = b por eliminación gaussiana con pivote (a cuadrada, pequeña)."""
    n = len(b)
    m = [list(fila) + [b[k]] for k, fila in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            continue
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[k][n] / m[k][k] if abs(m[k][k]) > 1e-12 else 0.0 for k in range(n)]


class Modelo:
    def __init__(self, rasgos=RASGOS, estrategias=()):
        self.rasgos, self.estrategias = list(rasgos), sorted(set(estrategias))
        self.media, self.desv, self.w, self.n = {}, {}, [], 0

    def _vector(self, x, est):
        v = [1.0]
        for k in self.rasgos:
            val = x.get(k)
            v.append(0.0 if val is None else max(-5.0, min(5.0, (val - self.media[k]) / self.desv[k])))
            v.append(1.0 if val is None else 0.0)
        v += [1.0 if est == e else 0.0 for e in self.estrategias]
        return v

    def entrenar(self, filas):
        """filas: [(rasgos dict, estrategia, r)]"""
        self.n = len(filas)
        for k in self.rasgos:
            vals = [f[0][k] for f in filas if f[0].get(k) is not None]
            mu = sum(vals) / len(vals) if vals else 0.0
            sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals)) if len(vals) > 1 else 1.0
            self.media[k], self.desv[k] = mu, sd or 1.0
        xs = [self._vector(f[0], f[1]) for f in filas]
        ys = [max(R_TOPE[0], min(R_TOPE[1], f[2])) for f in filas]
        p = len(xs[0])
        a = [[sum(x[i] * x[j] for x in xs) + (LAMBDA if i == j and i > 0 else 0.0) for j in range(p)] for i in range(p)]
        b = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(p)]
        self.w = _resolver(a, b)
        return self

    def predecir(self, x, est):
        return sum(w * v for w, v in zip(self.w, self._vector(x, est)))

    def a_dict(self):
        return {"rasgos": self.rasgos, "estrategias": self.estrategias, "media": self.media, "desv": self.desv,
                "w": self.w, "n": self.n, "lambda": LAMBDA, "umbral_r": UMBRAL_R}

    @classmethod
    def desde_dict(cls, d):
        """Carga un modelo guardado y lo VALIDA: si algo no cuadra, devuelve None (el bot no decide con un modelo roto)."""
        try:
            m = cls(d["rasgos"], d["estrategias"])
            m.media = {k: float(d["media"][k]) for k in m.rasgos}
            m.desv = {k: float(d["desv"][k]) or 1.0 for k in m.rasgos}
            m.w = [float(x) for x in d["w"]]
            m.n = int(d.get("n") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        esperado = 1 + 2 * len(m.rasgos) + len(m.estrategias)
        if len(m.w) != esperado or not all(math.isfinite(x) and abs(x) < 100 for x in m.w) or m.n < MIN_ENTRENO:
            return None
        if not all(math.isfinite(v) for v in list(m.media.values()) + list(m.desv.values())):
            return None
        return m

    def pesos(self):
        """Peso de cada rasgo (en R por desviación típica) para el informe."""
        return {k: round(self.w[1 + 2 * n], 4) for n, k in enumerate(self.rasgos)} if self.w else {}


def walk_forward(candidatas, pliegues, embargo_seg, rasgos=RASGOS):
    """candidatas: [{t, cierre_ts, r, estrategia, rasgos, ...}] ordenadas. Devuelve (elegidas, info por tramo).
    En cada tramo [ini, fin) se entrena con las candidatas que cerraron antes de ini − embargo."""
    elegidas, tramos = [], []
    ests = {c["estrategia"] for c in candidatas}
    for ini, fin in pliegues:
        entreno = [(c["rasgos"], c["estrategia"], c["r"]) for c in candidatas if c["cierre_ts"] < ini - embargo_seg]
        prueba = [c for c in candidatas if ini <= c["t"] < fin]
        if len(entreno) < MIN_ENTRENO:
            tramos.append({"desde": ini, "entreno": len(entreno), "candidatas": len(prueba), "elegidas": 0,
                           "nota": "sin historia suficiente para entrenar: no decide"})
            continue
        m = Modelo(rasgos, ests).entrenar(entreno)
        sel = [dict(c, prediccion=round(p, 4)) for c in prueba if (p := m.predecir(c["rasgos"], c["estrategia"])) > UMBRAL_R]
        elegidas += sel
        tramos.append({"desde": ini, "entreno": len(entreno), "candidatas": len(prueba), "elegidas": len(sel)})
    return elegidas, tramos


def comparar(candidatas, particion, embargo_seg, estadisticas):
    """Las tres variantes del informe sobre los mismos tramos fuera de muestra y el holdout."""
    pl = list(particion["pliegues"])
    ho = particion["holdout"]
    oos_todas = [c for c in candidatas if pl and pl[0][0] <= c["t"] < pl[-1][1]]
    out = {"todas_las_candidatas": {"fuera_de_muestra": estadisticas(oos_todas),
                                    "holdout": estadisticas([c for c in candidatas if c["t"] >= ho[0]])}}
    for nombre, rs in (("cerebro_solo_hyperliquid", RASGOS_HL), ("cerebro_con_todo", RASGOS)):
        oos, tramos = walk_forward(candidatas, pl, embargo_seg, rs)
        sel_ho, _ = walk_forward(candidatas, [ho], embargo_seg, rs)
        modelo = Modelo(rs, {c["estrategia"] for c in candidatas}).entrenar(
            [(c["rasgos"], c["estrategia"], c["r"]) for c in candidatas if c["cierre_ts"] < ho[0] - embargo_seg]) \
            if len(candidatas) >= MIN_ENTRENO else None
        out[nombre] = {"fuera_de_muestra": estadisticas(oos), "holdout": estadisticas(sel_ho), "tramos": tramos,
                       "pesos": modelo.pesos() if modelo else {}, "_oos": oos, "_holdout": sel_ho}
    return out
