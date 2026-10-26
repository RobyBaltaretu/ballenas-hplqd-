"""
Motor de señales propio: decide CUÁNDO entrar mirando el precio, no a otros traders.

Cada módulo analiza las velas CERRADAS de una moneda (1 h o 4 h) y, en una vela concreta,
dice si hay entrada (LARGO o CORTO), con qué fuerza y con qué stop. La salida la gestiona
estrategia.SimSalida (stop, cobro parcial, stop dinámico, objetivo final y tiempo máximo).

Módulos:
  ruptura     Rompe el máximo / mínimo de las últimas N velas a favor de la tendencia de fondo
              y con la volatilidad despertando. Para mercados que arrancan a moverse.
  retroceso   En una tendencia clara, entra en el retroceso a la media rápida cuando el precio
              vuelve a girar a favor. Mejor precio de entrada que la ruptura.
  reversion   En mercado lateral (sin tendencia), va contra los excesos: RSI extremo y precio
              muy alejado de su media, cuando empieza a girar. Objetivos cortos.
  funding     Cuando el funding está en un extremo histórico (casi todos apostando en un
              sentido), se pone en contra en cuanto el precio empieza a girar.
  fuerza      Fuerza relativa: compara todas las monedas y entra cuando una pasa a estar entre las que
              más suben (largo) o más bajan (corto) en los últimos días.
  ballenas    Grandes cuentas aumentando con fuerza su posición (flujo, ver ballenas.py).

El laboratorio (laboratorio.py) prueba cada módulo con años de datos: elige sus parámetros con
una parte de la historia y los valida con otra que no ha visto. El bot solo usa con dinero real
los módulos que superan esa validación.
"""

# ================================================================== indicadores
def ema_serie(valores, n):
    k = 2 / (n + 1)
    out, e = [], None
    for v in valores:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def atr_serie(h, l, c, n=14):
    out, a = [], None
    for i in range(len(c)):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def rsi_serie(cierres, n=14):
    out, g, p = [], None, None
    for i, c in enumerate(cierres):
        if i == 0:
            out.append(50.0)
            continue
        d = c - cierres[i - 1]
        up, dn = max(d, 0.0), max(-d, 0.0)
        g = up if g is None else (g * (n - 1) + up) / n
        p = dn if p is None else (p * (n - 1) + dn) / n
        out.append(100.0 if p == 0 else 100 - 100 / (1 + g / p))
    return out


def media_movil(valores, n):
    out, s = [], 0.0
    for i, v in enumerate(valores):
        s += v
        if i >= n:
            s -= valores[i - n]
        out.append(s / min(i + 1, n))
    return out


def eficiencia_serie(c, n=20):
    """Ratio de eficiencia de Kaufman por vela: 1 = tendencia limpia, 0 = ruido lateral."""
    pasos = [0.0] + [abs(c[i] - c[i - 1]) for i in range(1, len(c))]
    acumulado = [0.0]
    for p in pasos[1:]:
        acumulado.append(acumulado[-1] + p)
    out = []
    for i in range(len(c)):
        if i < n:
            out.append(0.5)
            continue
        recorrido = acumulado[i] - acumulado[i - n]
        out.append(abs(c[i] - c[i - n]) / recorrido if recorrido else 0.0)
    return out


# ================================================================== contexto
class Contexto:
    """Velas cerradas de una moneda con sus indicadores calculados una sola vez.
    Los módulos miran la vela 'i' (en vivo, la última: i = len - 1)."""

    def __init__(self, velas):
        self.v = velas
        self.t = [int(x["t"]) / 1000 for x in velas]
        self.o = [float(x["o"]) for x in velas]
        self.h = [float(x["h"]) for x in velas]
        self.l = [float(x["l"]) for x in velas]
        self.c = [float(x["c"]) for x in velas]
        self.atr = atr_serie(self.h, self.l, self.c, 14)
        self.atr_medio = media_movil(self.atr, 50)
        self.er = eficiencia_serie(self.c, 20)
        self._ema, self._rsi = {}, {}

    def __len__(self):
        return len(self.c)

    def ema(self, n):
        if n not in self._ema:
            self._ema[n] = ema_serie(self.c, n)
        return self._ema[n]

    def rsi(self, n):
        if n not in self._rsi:
            self._rsi[n] = rsi_serie(self.c, n)
        return self._rsi[n]

    def rasgos(self, lado, i):
        """Rasgos de la situación del mercado en la vela i (para el aprendizaje)."""
        s = 1 if lado == "L" else -1
        c, a = self.c[i], self.atr[i] or 1e-9
        e50 = self.ema(50)
        return {
            "tendencia_atr": s * (c - e50[i]) / a,
            "pendiente_atr": s * (e50[i] - e50[max(0, i - 5)]) / a,
            "impulso_atr": s * (c - self.c[max(0, i - 3)]) / a,
            "rsi": self.rsi(14)[i] if s == 1 else 100 - self.rsi(14)[i],
            "eficiencia": self.er[i],
            "atr_pct": a / c * 100,
            "vol_relativa": a / (self.atr_medio[i] or a),
        }


# ================================================================== módulos
def _senal(lado, fuerza, sl_atr, ctx, i, motivo):
    return {"lado": lado, "fuerza": round(float(fuerza), 3),
            "sl_pct": sl_atr * ctx.atr[i] / ctx.c[i] * 100, "motivo": motivo}


def m_ruptura(ctx, i, p, extra=None):
    n = p["n"]
    if i < max(n, p["ema"]) + 5:
        return None
    c, a = ctx.c[i], ctx.atr[i]
    if a < p["vol_min"] * ctx.atr_medio[i]:
        return None
    maximo, minimo = max(ctx.h[i - n:i]), min(ctx.l[i - n:i])
    e = ctx.ema(p["ema"])
    if c > maximo and c > e[i] and e[i] > e[i - 3]:
        return _senal("L", (c - maximo) / a, p["sl_atr"], ctx, i, f"rompe el máximo de {n} velas a favor de la tendencia")
    if c < minimo and c < e[i] and e[i] < e[i - 3]:
        return _senal("S", (minimo - c) / a, p["sl_atr"], ctx, i, f"rompe el mínimo de {n} velas a favor de la tendencia")
    return None


def m_retroceso(ctx, i, p, extra=None):
    if i < p["lenta"] + 5 or ctx.er[i] < p["er_min"]:
        return None
    r, le = ctx.ema(p["rapida"]), ctx.ema(p["lenta"])
    c, a = ctx.c[i], ctx.atr[i]
    if r[i] > le[i] and r[i] > r[i - 3] and ctx.l[i - 1] <= r[i - 1] and c > r[i] and c > ctx.o[i]:
        return _senal("L", (r[i] - le[i]) / a, p["sl_atr"], ctx, i, "retroceso a la media en tendencia alcista")
    if r[i] < le[i] and r[i] < r[i - 3] and ctx.h[i - 1] >= r[i - 1] and c < r[i] and c < ctx.o[i]:
        return _senal("S", (le[i] - r[i]) / a, p["sl_atr"], ctx, i, "retroceso a la media en tendencia bajista")
    return None


def m_reversion(ctx, i, p, extra=None):
    if i < 60 or ctx.er[i] > p["er_max"]:
        return None
    rsi = ctx.rsi(p["rsi_n"])
    e = ctx.ema(50)
    c, a = ctx.c[i], ctx.atr[i]
    desvio = (c - e[i]) / a
    if rsi[i - 1] < p["rsi_bajo"] and desvio < -p["desvio_atr"] and c > ctx.o[i]:
        return _senal("L", -desvio, p["sl_atr"], ctx, i, f"exceso bajista (RSI {rsi[i - 1]:.0f}) que empieza a girar")
    if rsi[i - 1] > 100 - p["rsi_bajo"] and desvio > p["desvio_atr"] and c < ctx.o[i]:
        return _senal("S", desvio, p["sl_atr"], ctx, i, f"exceso alcista (RSI {rsi[i - 1]:.0f}) que empieza a girar")
    return None


def m_funding(ctx, i, p, extra=None):
    """extra = {"funding_medio": % por hora, "umbral_alto": ..., "umbral_bajo": ...}"""
    if not extra or extra.get("funding_medio") is None or extra.get("umbral_alto") is None or i < 30:
        return None
    f = extra["funding_medio"]
    r = ctx.ema(p["rapida"])
    c = ctx.c[i]
    if f >= extra["umbral_alto"] > 0 and c < r[i] and ctx.c[i - 1] >= r[i - 1]:
        return _senal("S", f / extra["umbral_alto"], p["sl_atr"], ctx, i, f"funding extremo positivo ({f:.4f}%/h) y el precio gira")
    if f <= extra["umbral_bajo"] < 0 and c > r[i] and ctx.c[i - 1] <= r[i - 1]:
        return _senal("L", f / extra["umbral_bajo"], p["sl_atr"], ctx, i, f"funding extremo negativo ({f:.4f}%/h) y el precio gira")
    return None


def m_fuerza(ctx, i, p, extra=None):
    """Fuerza relativa entre monedas (cartera "momentum"): cada 'rebalanceo' velas, va LARGO en las 'top'
    monedas que más han subido en las últimas 'lookback' velas (medido en ATR) y CORTO en las que más han
    bajado. Mantiene hasta el siguiente rebalanceo, con un stop amplio de seguridad y sin objetivo.
    extra = {"arriba": puesto desde la más fuerte (0 = la que más sube), "abajo": desde la más débil, "puntos"}."""
    if not extra or i < p["lookback"] + 5 or len(ctx.t) < 2:
        return None
    tf = ctx.t[1] - ctx.t[0]
    if int(ctx.t[i] // tf) % p["rebalanceo"]:
        return None
    k = p["top"]
    if extra["arriba"] < k and extra["puntos"] > 0:
        return _senal("L", extra["puntos"], p["sl_atr"], ctx, i,
                      f"entre las {k} monedas más fuertes de las últimas {p['lookback']} velas")
    if extra["abajo"] < k and extra["puntos"] < 0:
        return _senal("S", -extra["puntos"], p["sl_atr"], ctx, i,
                      f"entre las {k} monedas más débiles de las últimas {p['lookback']} velas")
    return None


def m_momentum(ctx, i, p, extra=None):
    """Momentum de serie temporal de varias semanas: en cada rebalanceo, va a favor de lo que ha hecho la propia
    moneda en las últimas 'lookback' velas, solo si el movimiento es claro (al menos 'umbral_atr' ATR) y la
    tendencia es limpia (eficiencia mínima). Mantiene hasta el siguiente rebalanceo, sin objetivo.
    El tamaño ya se ajusta a la volatilidad: el stop es 'sl_atr' ATR y el bot dimensiona por riesgo."""
    lb = p["lookback"]
    if i < lb + 5 or len(ctx.t) < 2 or not ctx.atr[i]:
        return None
    tf = ctx.t[1] - ctx.t[0]
    if int(ctx.t[i] // tf) % p["rebalanceo"]:
        return None
    movimiento = (ctx.c[i] - ctx.c[i - lb]) / ctx.atr[i]
    recorrido = sum(abs(ctx.c[k] - ctx.c[k - 1]) for k in range(i - lb + 1, i + 1))
    eficiencia = abs(ctx.c[i] - ctx.c[i - lb]) / recorrido if recorrido else 0.0
    if abs(movimiento) < p["umbral_atr"] or eficiencia < p["eficiencia_min"]:
        return None
    dias = lb * tf / 86400
    if movimiento > 0:
        return _senal("L", movimiento, p["sl_atr"], ctx, i, f"sube con claridad en los últimos {dias:.0f} días (momentum)")
    return _senal("S", -movimiento, p["sl_atr"], ctx, i, f"baja con claridad en los últimos {dias:.0f} días (momentum)")


def puntos_fuerza(ctx, i, lookback):
    """Cuánto ha subido (o bajado) una moneda en las últimas 'lookback' velas, en ATR (comparable entre monedas)."""
    if i < lookback or not ctx.atr[i]:
        return None
    return (ctx.c[i] - ctx.c[i - lookback]) / ctx.atr[i]


def rangos_fuerza(contextos, lookback, min_monedas=6):
    """contextos = [(moneda, Contexto)]. Devuelve {moneda: {i: extra}} alineando las velas por su hora.
    Solo hay ranking en las horas en que al menos 'min_monedas' tienen datos (si no, no es comparable)."""
    por_t = {}
    for moneda, ctx in contextos:
        for i, t in enumerate(ctx.t):
            pf = puntos_fuerza(ctx, i, lookback)
            if pf is not None:
                por_t.setdefault(t, []).append((pf, moneda, i))
    puesto = {}
    for t, filas in por_t.items():
        if len(filas) < min_monedas:
            continue
        filas.sort(reverse=True)
        n = len(filas)
        for pos, (pf, moneda, i) in enumerate(filas):
            puesto[(moneda, i)] = (pos, n - 1 - pos, pf)
    out = {}
    for moneda, ctx in contextos:
        filas = {}
        for i in range(1, len(ctx.t)):
            a, b = puesto.get((moneda, i)), puesto.get((moneda, i - 1))
            if a and b:
                filas[i] = {"arriba": a[0], "abajo": a[1], "puntos": a[2], "arriba_prev": b[0], "abajo_prev": b[1]}
        out[moneda] = filas
    return out


MODULOS = {
    "ruptura": {
        "version": "1",
        "funcion": m_ruptura,
        "defecto": {"n": 20, "ema": 50, "vol_min": 1.0, "sl_atr": 2.0},
        "rejilla": {"n": [20, 40], "ema": [50, 100], "vol_min": [0.8, 1.1], "sl_atr": [1.5, 2.5]},
        "salida": {"tp1_r": 1.5, "tp_final_r": 4.0, "trailing_r": 1.5},
        "barras_max": 30,
    },
    "retroceso": {
        "version": "1",
        "funcion": m_retroceso,
        "defecto": {"rapida": 20, "lenta": 50, "er_min": 0.3, "sl_atr": 1.5},
        "rejilla": {"rapida": [10, 20], "lenta": [50, 100], "er_min": [0.25, 0.4], "sl_atr": [1.2, 2.0]},
        "salida": {"tp1_r": 1.0, "tp_final_r": 3.0, "trailing_r": 1.0},
        "barras_max": 24,
    },
    "reversion": {
        "version": "1",
        "funcion": m_reversion,
        "defecto": {"rsi_n": 14, "rsi_bajo": 25, "er_max": 0.25, "desvio_atr": 2.0, "sl_atr": 1.5},
        "rejilla": {"rsi_n": [7, 14], "rsi_bajo": [20, 30], "er_max": [0.2, 0.35], "desvio_atr": [1.5, 2.5],
                    "sl_atr": [1.2, 2.0]},
        "salida": {"tp1_r": 0.8, "tp_final_r": 1.8, "trailing_r": 0.8},
        "barras_max": 10,
    },
    "funding": {
        "version": "1",
        "funcion": m_funding,
        "defecto": {"rapida": 10, "percentil": 95, "horas": 8, "sl_atr": 1.8},
        "rejilla": {"rapida": [10, 20], "percentil": [90, 97], "horas": [8, 24], "sl_atr": [1.5, 2.5]},
        "salida": {"tp1_r": 1.0, "tp_final_r": 3.0, "trailing_r": 1.2},
        "barras_max": 18,
    },
}

MODULOS["fuerza"] = {
    "version": "2",   # v2: cartera con rebalanceo periódico (v1: entrada al cambiar de puesto)
    "funcion": m_fuerza,
    "defecto": {"lookback": 42, "rebalanceo": 84, "top": 1, "sl_atr": 3.0},
    "rejilla": {"lookback": [42, 84, 180], "rebalanceo": [42, 84], "top": [1, 2], "sl_atr": [2.5, 3.5]},
    # Sin objetivo ni cobro parcial: se mantiene hasta el rebalanceo (o el stop de seguridad).
    "salida": {"parcial_activo": False, "tp1_r": 98, "tp_final_r": 99, "trailing_r": 99, "breakeven_tras_r": 99,
               "sin_objetivo": True},
    "barras_max": 83,
}


MODULOS["momentum"] = {
    "version": "1",
    "funcion": m_momentum,
    "defecto": {"lookback": 126, "rebalanceo": 42, "umbral_atr": 4.0, "eficiencia_min": 0.1, "sl_atr": 4.0},
    # En velas de 4 h: lookback 63/126/180 ≈ 10/21/30 días; rebalanceo 42 ≈ 7 días.
    "rejilla": {"lookback": [63, 126, 180], "rebalanceo": [21, 42], "umbral_atr": [3.0, 6.0],
                "eficiencia_min": [0.1], "sl_atr": [3.0, 5.0]},
    "salida": {"parcial_activo": False, "tp1_r": 98, "tp_final_r": 99, "trailing_r": 99, "breakeven_tras_r": 99,
               "sin_objetivo": True},
    "barras_max": 41,
    "intervalos": ["4h"],     # en velas de 1 h 'varias semanas' superaría la historia que se descarga en vivo
}


def barras_max(nombre, params):
    """Velas que puede durar una operación de ese módulo con esos parámetros."""
    if nombre in ("fuerza", "momentum"):
        return params["rebalanceo"] - 1   # se cierra justo antes del siguiente rebalanceo
    return MODULOS[nombre]["barras_max"]


INTERVALOS = {"1h": 60, "4h": 240}


def usa_intervalo(nombre, intervalo):
    """Algunos módulos solo tienen sentido en ciertos intervalos (p. ej. momentum de semanas, solo en 4 h)."""
    return intervalo in MODULOS[nombre].get("intervalos", (intervalo,))


def combinaciones(rejilla):
    total = [{}]
    for k, valores in rejilla.items():
        total = [dict(c, **{k: v}) for c in total for v in valores]
    return total


def detectar(nombre, ctx, i, params, extra=None):
    return MODULOS[nombre]["funcion"](ctx, i, params, extra)


# ================================================================== funding
def umbrales_funding(valores_pct_h, percentil):
    """Umbral alto (>0) y bajo (<0) del funding medio según su propia historia."""
    if len(valores_pct_h) < 100:
        return None, None
    s = sorted(valores_pct_h)
    alto = s[min(len(s) - 1, int(len(s) * percentil / 100))]
    bajo = s[max(0, int(len(s) * (100 - percentil) / 100))]
    return (alto if alto > 0 else None), (bajo if bajo < 0 else None)


class SerieFunding:
    """Funding histórico de una moneda (% por hora) con medias rápidas por ventana."""

    def __init__(self, registros):
        regs = sorted(registros, key=lambda x: x["time"])
        self.t = [x["time"] / 1000 for x in regs]
        self.f = [float(x["fundingRate"]) * 100 for x in regs]
        self.acum = [0.0]
        for v in self.f:
            self.acum.append(self.acum[-1] + v)

    def media(self, t, horas):
        import bisect
        j = bisect.bisect_right(self.t, t)
        i = bisect.bisect_right(self.t, t - horas * 3600)
        return (self.acum[j] - self.acum[i]) / (j - i) if j > i else None

    def suma(self, t0, t1):
        import bisect
        j = bisect.bisect_right(self.t, t1)
        i = bisect.bisect_right(self.t, t0)
        return self.acum[j] - self.acum[i]

    def medias_historicas(self, hasta_t, horas):
        """Medias de 'horas' muestreadas cada 'horas' hasta 'hasta_t' (para calcular percentiles sin mirar el futuro)."""
        out, t = [], (self.t[0] if self.t else hasta_t) + horas * 3600
        while t <= hasta_t:
            m = self.media(t, horas)
            if m is not None:
                out.append(m)
            t += horas * 3600
        return out
