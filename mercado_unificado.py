"""
Vista de mercado UNIFICADA: Hyperliquid + Binance + Bybit en un solo registro por moneda y vela.

Una única función (rasgos) calcula, para una posible entrada, todo lo que el «cerebro» (cerebro.py) puede usar.
La usan el laboratorio (con historia) y, en la fase de prueba en vivo, el bot: mismas definiciones en los dos
sitios, para que lo que se prueba sea exactamente lo que se usa.

Rasgos (los direccionales van con signo: positivo = a favor del lado de la operación):
  Hyperliquid  tendencia (precio vs media de 50 en ATR), impulso de 6 velas (en ATR), volatilidad relativa,
               funding de las últimas 8 h (positivo = la operación lo COBRA)
  Externos     diferencia de funding Hyperliquid − Binance/Bybit (8 h), flujo comprador agresivo de Binance
               (última vela y 6 velas), diferencia de precio Hyperliquid − Binance (base)
Un rasgo que no se puede calcular vale None (el cerebro lo trata como «sin dato», nunca se inventa).
"""

import bisect

RASGOS_HL = ("tendencia", "impulso6", "volatilidad", "funding8h")
RASGOS_EXT = ("dif_funding8h", "flujo1", "flujo6", "base_pct")
RASGOS = RASGOS_HL + RASGOS_EXT


class SerieExterna:
    """Velas externas de una moneda (con 'tb' = volumen comprador agresivo si la fuente lo da), por tiempo."""

    def __init__(self, velas):
        velas = sorted(velas or [], key=lambda v: int(v["t"]))
        self.t = [int(v["t"]) / 1000 for v in velas]
        self.c = [float(v["c"]) for v in velas]
        self.flujo = []
        for v in velas:
            vol, tb = float(v.get("v") or 0), v.get("tb")
            self.flujo.append((2 * float(tb) / vol - 1) if tb is not None and vol > 0 else None)

    def indice(self, t):
        """Índice de la vela que empieza en t (o None)."""
        j = bisect.bisect_left(self.t, t)
        return j if j < len(self.t) and abs(self.t[j] - t) < 1 else None


class FundingHorario:
    """Registros horarios {'time' ms, 'fundingRate'} -> media de las últimas h horas antes de t, en %/h."""

    def __init__(self, regs):
        regs = sorted(regs or [], key=lambda x: x["time"])
        self.t = [x["time"] / 1000 for x in regs]
        self.acum = [0.0]
        for x in regs:
            self.acum.append(self.acum[-1] + float(x["fundingRate"]) * 100)

    def media(self, t, horas=8):
        j = bisect.bisect_right(self.t, t)
        i = bisect.bisect_right(self.t, t - horas * 3600)
        return (self.acum[j] - self.acum[i]) / (j - i) if j > i else None


def rasgos(ctx, i, lado, funding_hl=None, externa=None, funding_ext=None):
    """Rasgos de una entrada decidida al CIERRE de la vela i (solo datos hasta i). ctx: senales.Contexto."""
    sg = 1 if lado in ("L", "largo") else -1
    out = dict.fromkeys(RASGOS)
    if i < 50 or i >= len(ctx):
        return out
    c, atr = ctx.c[i], ctx.atr[i]
    if atr and c:
        out["tendencia"] = sg * (c - ctx.ema(50)[i]) / atr
        out["impulso6"] = sg * (c - ctx.c[i - 6]) / atr
        if ctx.atr_medio[i]:
            out["volatilidad"] = atr / ctx.atr_medio[i]
    t_fin = ctx.t[i] + (ctx.t[i] - ctx.t[i - 1])            # cierre de la vela i
    fh = funding_hl.media(t_fin, 8) if funding_hl is not None else None
    if fh is not None:
        out["funding8h"] = -sg * fh                           # el largo PAGA funding positivo
    fe = funding_ext.media(t_fin, 8) if funding_ext is not None else None
    if fh is not None and fe is not None:
        out["dif_funding8h"] = -sg * (fh - fe)                # HL más caro que fuera para el largo = en contra
    if externa is not None:
        j = externa.indice(ctx.t[i])
        if j is not None:
            out["flujo1"] = sg * externa.flujo[j] if externa.flujo[j] is not None else None
            ult = [x for x in externa.flujo[max(0, j - 5): j + 1] if x is not None]
            out["flujo6"] = sg * sum(ult) / len(ult) if len(ult) >= 4 else None
            if externa.c[j]:
                out["base_pct"] = -sg * (c / externa.c[j] - 1) * 100  # HL caro frente a Binance = en contra del largo
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out.items()}
