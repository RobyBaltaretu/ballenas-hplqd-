"""
Piezas comunes: las usan igual el bot (dinero real), la memoria (operaciones en papel)
y el laboratorio (historia). Así lo que se prueba es exactamente lo que se opera.

Contiene:
  - redondeos de Hyperliquid, correlación, costes,
  - gestión de capital (factor_capital: SOLO reduce tamaño; los límites duros están en riesgo.py),
  - SimSalida: gestión de la salida (stop, cierre parcial, stop sin pérdidas,
    stop dinámico, objetivo final y tiempo máximo) aplicada vela a vela,
  (El tamaño de cada operación se calcula en riesgo.dimensionar, igual para bot, shadow y laboratorio.)
"""

import math

NOTIONAL_MINIMO_HL = 10.0  # Hyperliquid rechaza órdenes de menos de 10 USD


# ================================================================ redondeos
def redondear_precio(px, sz_decimals):
    """Hyperliquid: máx. 5 cifras significativas y (6 - szDecimals) decimales en perps."""
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


def redondear_tamano(sz, sz_decimals):
    factor = 10 ** sz_decimals
    return math.floor(sz * factor + 1e-9) / factor


# ============================================================== indicadores
def ema(valores, n):
    k = 2 / (n + 1)
    e = valores[0]
    serie = [e]
    for v in valores[1:]:
        e = v * k + e * (1 - k)
        serie.append(e)
    return serie


def atr_pct(velas, n=14):
    """ATR medio de las últimas n velas, como fracción del precio."""
    trs = []
    for prev, v in zip(velas, velas[1:]):
        h, l, pc = float(v["h"]), float(v["l"]), float(prev["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    return (sum(trs[-n:]) / n) / float(velas[-1]["c"])


def correlacion(velas_a, velas_b, n=60):
    """Correlación de los rendimientos de dos series de velas del mismo intervalo (alineadas por hora)."""
    ca = {v["t"]: float(v["c"]) for v in velas_a}
    cb = {v["t"]: float(v["c"]) for v in velas_b}
    tiempos = sorted(set(ca) & set(cb))[-(n + 1):]
    if len(tiempos) < 20:
        return None
    ra = [math.log(ca[t2] / ca[t1]) for t1, t2 in zip(tiempos, tiempos[1:])]
    rb = [math.log(cb[t2] / cb[t1]) for t1, t2 in zip(tiempos, tiempos[1:])]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)


# =================================================================== perfil
def costes(c):
    """Comisiones (fracción) de entrada, salida a mercado y maker, desde el bloque único "comisiones".
    Para estimar el RIESGO se usa riesgo.costes_fraccion (supone lo peor: entrada a mercado)."""
    com = c["comisiones"]
    maker, taker = com["maker_pct"] / 100, com["taker_pct"] / 100
    entrada = maker if c["ordenes"]["entrada_limite"] else taker
    return entrada, taker, maker


def factor_capital(saldo, saldo_maximo, ultimos_r, cfg, saldo_inicio_dia=None, saldo_inicio_semana=None,
                   horas_sin_operar=0.0):
    """
    Gestión de capital para crecer de forma constante SIN dejar de operar:
    en vez de parar el bot cuando algo va mal, reduce el tamaño, y lo recupera al mejorar.
      - Cuenta en caída desde su máximo          -> más pequeño (escalonado).
      - Últimas operaciones reales malas         -> "modo defensa".
      - Pérdida del día o de la semana excesiva  -> más pequeño hasta el día / semana siguiente.
      - Objetivo diario conseguido               -> más pequeño para proteger lo ganado.
      - Muchas horas sin operar                  -> filtros algo más flexibles, con tamaño reducido.
    Devuelve (factor 0-1, motivos, relajar_filtros).
    """
    factor, motivos = 1.0, []

    def aplicar(f, motivo):
        nonlocal factor
        factor *= f
        motivos.append(motivo)

    if saldo_maximo and saldo < saldo_maximo:
        caida = (saldo_maximo - saldo) / saldo_maximo * 100
        for umbral, f in sorted(cfg["reducir_en_caida"], reverse=True):
            if caida >= umbral:
                aplicar(f, f"cuenta {caida:.1f}% por debajo de su máximo")
                break
    d = cfg["modo_defensa"]
    if len(ultimos_r) >= d["ultimas_operaciones"]:
        recientes = ultimos_r[-d["ultimas_operaciones"]:]
        media = sum(recientes) / len(recientes)
        if media < d["r_medio_minimo"]:
            aplicar(d["factor"], f"modo defensa: últimas {len(recientes)} operaciones {media:+.2f} R de media")
    if saldo_inicio_dia:
        cambio_dia = (saldo / saldo_inicio_dia - 1) * 100
        if cambio_dia <= -cfg["perdida_diaria_pct"]:
            aplicar(cfg["factor_tras_perdida_diaria"], f"hoy {cambio_dia:.1f}%")
        elif cfg.get("objetivo_diario_pct") and cambio_dia >= cfg["objetivo_diario_pct"]:
            aplicar(cfg["factor_tras_objetivo"], f"objetivo diario conseguido ({cambio_dia:+.1f}%): protejo lo ganado")
    if saldo_inicio_semana:
        cambio_sem = (saldo / saldo_inicio_semana - 1) * 100
        if cambio_sem <= -cfg["perdida_semanal_pct"]:
            aplicar(cfg["factor_tras_perdida_semanal"], f"esta semana {cambio_sem:.1f}%")
    relajar = horas_sin_operar >= cfg["horas_sin_operar_para_relajar"]
    if relajar:
        aplicar(cfg["factor_relajado"], f"{horas_sin_operar:.0f} h sin operar: filtros más flexibles")
    return factor, motivos, relajar


# ============================================================ simulador salida
class SimSalida:
    """
    Reproduce la gestión de salida vela a vela (o precio a precio):
      1. Stop-loss inicial a 1 R.
      2. Si parcial_activo: al tocar tp1_r cierra parcial_pct % y sube el stop a sin pérdidas;
         desde ahí el stop persigue al precio a trailing_r de distancia.
         Si no: al avanzar breakeven_tras_r sube el stop a sin pérdidas.
      3. Objetivo final a tp_final_r cierra el resto.
      4. Al llegar al tiempo máximo cierra lo que quede.
    Criterio conservador: si en una misma vela se tocan stop y objetivo, cuenta el stop.
    Resultado en R (1 R = distancia al stop), con comisiones descontadas.
    """

    def __init__(self, lado, entrada, sl_pct, cfg_salida, limite_ts, fee_entrada, fee_taker, fee_maker,
                 desl_mercado=0.0):
        self.s = 1 if lado == "L" else -1
        # Deslizamiento (fracción) de las salidas A MERCADO (stop y tiempo máximo): un stop no se ejecuta
        # exactamente a su precio. Los objetivos son órdenes límite y salen a su precio (maker).
        self.desl_mercado = desl_mercado
        self.entrada = entrada
        self.sl_pct = sl_pct
        self.cfg = cfg_salida
        self.limite_ts = limite_ts
        self.fee_entrada, self.fee_taker, self.fee_maker = fee_entrada, fee_taker, fee_maker
        self.r_dist = entrada * sl_pct / 100
        s = self.s
        self.sl = entrada - s * self.r_dist
        self.tp1 = entrada + s * cfg_salida["tp1_r"] * self.r_dist
        self.tpf = entrada + s * cfg_salida["tp_final_r"] * self.r_dist
        self.mejor = entrada
        self.restante = 1.0
        self.realizado_r = 0.0
        self.tp1_hecho = False
        self.en_breakeven = False
        self.cerrada = False
        self.motivo = None
        self.cierre_ts = None

    # --- utilidades
    def _fee_r(self, fee):
        return fee * 100 / self.sl_pct

    def precio_breakeven(self):
        return self.entrada + self.s * self.entrada * (self.fee_entrada + self.fee_taker) * 1.2

    def _realizar(self, fraccion, precio, fee_salida):
        mov_r = self.s * (precio - self.entrada) / self.r_dist
        self.realizado_r += fraccion * (mov_r - self._fee_r(self.fee_entrada) - self._fee_r(fee_salida))
        self.restante -= fraccion

    def _cerrar(self, precio, fee, motivo, ts):
        self._realizar(self.restante, precio, fee)
        self.restante = 0.0
        self.cerrada, self.motivo, self.cierre_ts = True, motivo, ts

    def resultado_r(self):
        return round(self.realizado_r, 3)

    # --- paso
    def _toca_stop(self, adverso):
        return self.s * (adverso - self.sl) <= 0

    def _cerrar_en_stop(self, apertura, ts):
        precio = self.sl
        if apertura is not None and self.s * (apertura - self.sl) < 0:
            precio = apertura  # el precio abrió ya más allá del stop (hueco): se ejecuta peor
        precio *= 1 - self.s * getattr(self, "desl_mercado", 0.0)     # salida a mercado: algo peor que el trigger
        motivo = "stop dinámico" if self.tp1_hecho and self.sl != self.precio_breakeven() else \
            ("stop sin pérdidas" if self.en_breakeven else "stop-loss")
        self._cerrar(precio, self.fee_taker, motivo, ts)

    def paso(self, alto, bajo, cierre, ts, apertura=None):
        """Procesa una vela (o un precio: alto=bajo=cierre). Devuelve True si se ha cerrado.
        Criterio conservador: dentro de una vela se asume que lo malo pasa primero."""
        if self.cerrada:
            return True
        s, cfg = self.s, self.cfg
        adverso = bajo if s == 1 else alto
        favorable = alto if s == 1 else bajo

        if self._toca_stop(adverso):
            self._cerrar_en_stop(apertura, ts)
            return True

        if cfg["parcial_activo"] and not self.tp1_hecho and s * (favorable - self.tp1) >= 0:
            self._realizar(cfg["parcial_pct"] / 100, self.tp1, self.fee_maker)
            self.tp1_hecho = self.en_breakeven = True
            self.sl = self.precio_breakeven()
            if self._toca_stop(adverso):  # cobró el 1er objetivo y en la misma vela volvió atrás
                self._cerrar_en_stop(None, ts)
                return True

        if s * (favorable - self.tpf) >= 0:
            self._cerrar(self.tpf, self.fee_maker, "objetivo final", ts)
            return True

        if not self.en_breakeven and s * (favorable - self.entrada) / self.r_dist >= cfg["breakeven_tras_r"]:
            self.en_breakeven = True
            self.sl = self.precio_breakeven()
            if self._toca_stop(adverso):
                self._cerrar_en_stop(None, ts)
                return True

        # El stop dinámico se calcula con lo visto hasta ahora y se aplica desde la vela siguiente,
        # moviéndolo solo si mejora al menos mover_stop_min_r (igual que el bot real).
        if s * (favorable - self.mejor) > 0:
            self.mejor = favorable
        if self.tp1_hecho:
            nuevo = self.mejor - s * cfg["trailing_r"] * self.r_dist
            if s * (nuevo - self.sl) >= cfg.get("mover_stop_min_r", 0) * self.r_dist:
                self.sl = nuevo

        if ts >= self.limite_ts:
            self._cerrar(cierre * (1 - s * getattr(self, "desl_mercado", 0.0)), self.fee_taker, "tiempo máximo", ts)
            return True
        return False

    def cerrar_ya(self, precio, motivo, ts):
        if not self.cerrada:
            self._cerrar(precio, self.fee_taker, motivo, ts)

    # --- guardar / cargar (para las operaciones en papel)
    def a_dict(self):
        return dict(self.__dict__)

    @classmethod
    def desde_dict(cls, d):
        obj = cls.__new__(cls)
        obj.__dict__.update(d)
        return obj
