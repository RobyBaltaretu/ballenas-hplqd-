"""
Laboratorio de estrategias: prueba con la historia real de Hyperliquid qué forma de entrar y salir
habría ganado dinero, y elige la configuración del bot con datos, no con intuición.

Qué hace:
  1. Elige las monedas más líquidas del momento (las mismas que usará el bot).
  2. Descarga sus velas de 1 h (~7 meses) y de 4 h (~2,3 años) y su funding. Con historia ampliada
     (laboratorio.historia_externa) añade ANTES de esas velas la historia pública de Binance/Bybit
     (por defecto 2 años en 1 h y 6 en 4 h), validada contra Hyperliquid (historia_externa.py).
  3. Para cada módulo de senales.py y cada intervalo, prueba una rejilla pequeña de parámetros con
     WALK-FORWARD (validacion.py):
       - varios tramos de prueba seguidos; en cada uno los parámetros se eligen SOLO con lo anterior,
         quitando (purga + embargo) las operaciones que seguían abiertas al empezar el tramo;
       - un HOLDOUT final que no se usa para elegir nada (y se cuenta cuántas veces se ha mirado);
       - corrección por número de variantes probadas, IC por bootstrap de bloques, líneas base
         (entrada aleatoria y siempre largo), estrés de costes ×1,5 y universo "en el momento"
         (solo monedas que ya eran líquidas cuando se habría operado).
     Cada operación se simula con la gestión de salida real del bot (stop, cobro parcial, stop
     dinámico, objetivo, tiempo máximo), con comisiones, deslizamiento y funding.
  4. Experimentos SOLO de investigación: filtro de régimen y momentum de serie temporal con tamaño por
     volatilidad. Mide también a las ballenas (flujo) con el historial disponible de sus órdenes.
  5. Escribe estrategia_elegida.json y el registro de la ejecución (registro/laboratorio_ejecuciones.jsonl)
     y AÑADE EVIDENCIA a promociones.json. El laboratorio NUNCA aprueba nada para dinero real ni sube
     límites: eso lo decide una persona con promocion.py. Con --guardar-memoria preentrena el aprendizaje.

Uso:
  python laboratorio.py                    # todo
  python laboratorio.py --guardar-memoria  # y preentrena el bot
  python laboratorio.py --rapido           # menos monedas y parámetros (para probar)
  python laboratorio.py --sin-cache        # vuelve a descargar todo

Avisos (también se imprimen al final):
  - El universo "en el momento" se elige dentro de las monedas que existen HOY: las que desaparecieron
    (delisted) no están en los datos de Hyperliquid, así que sigue habiendo algo de sesgo de supervivencia.
  - Dentro de una vela no se sabe qué pasó antes; si tocó stop y objetivo, se cuenta el stop.
  - Un resultado positivo en el pasado no garantiza el futuro. Por eso se exige que funcione en
    datos que el laboratorio no usó para elegir los parámetros.
"""

import argparse
import csv
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from ballenas import FlujoBallenas
from compat import consola_utf8, reemplazar
from datos import (
    MIN_INTERVALO,
    Api,
    descargar_fills,
    descargar_funding_cacheado,
    descargar_universo,
    descargar_velas,
)
import archivo
import configuracion as conf
import historia_externa
import validacion as vd
from estrategia import SimSalida
from riesgo import costes_excesivos
from senales import (
    INTERVALOS,
    MODULOS,
    Contexto,
    SerieFunding,
    barras_max,
    combinaciones,
    detectar,
    rangos_fuerza,
    umbrales_funding,
    usa_intervalo,
)

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_ARCHIVO = os.path.join(AQUI, "datos_archivo")
FRACCION_ENTRENAMIENTO = 0.7
DESLIZAMIENTO = 0.0002     # 0,02 % peor en cada entrada (mínimo; crece con la volatilidad, ver deslizamiento_vela)
FACTOR_DESL_ATR = 0.05     # + 5 % del ATR (en % del precio) de la vela anterior: más volátil -> peor ejecución


def deslizamiento_vela(ctx, j):
    """Deslizamiento SUPUESTO (fracción) para una orden a mercado en la vela j: un mínimo fijo más una parte del
    ATR relativo de la vela anterior. Es una estimación (no hay libro histórico): el estrés lo multiplica."""
    try:
        atr_rel = ctx.atr[j - 1] / ctx.c[j - 1]
    except (IndexError, TypeError, ZeroDivisionError):
        atr_rel = 0.0
    return DESLIZAMIENTO + FACTOR_DESL_ATR * (atr_rel or 0.0)
MIN_OPS_ENTRENAMIENTO = 30
CRITERIO = {"min_ops_validacion": 30, "r_medio_min": 0.05, "factor_beneficio_min": 1.1, "t_min": 1.0}


def fecha(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ================================================================ estadísticas
def estadisticas(trades):
    rs = [t["r"] for t in trades]
    if not rs:
        return {"n": 0}
    ganan, pierden = [x for x in rs if x > 0], [x for x in rs if x <= 0]
    acum = maximo = caida = 0.0
    for x in sorted(trades, key=lambda t: t["t"]):
        acum += x["r"]
        maximo = max(maximo, acum)
        caida = max(caida, maximo - acum)
    dias = max(1.0, (max(t["t"] for t in trades) - min(t["t"] for t in trades)) / 86400)
    media = sum(rs) / len(rs)
    desv = math.sqrt(sum((x - media) ** 2 for x in rs) / (len(rs) - 1)) if len(rs) > 1 else 0.0
    return {
        "n": len(rs), "ganan_pct": round(len(ganan) / len(rs) * 100, 1), "r_medio": round(sum(rs) / len(rs), 3),
        "r_total": round(sum(rs), 1),
        "factor_beneficio": round(sum(ganan) / -sum(pierden), 2) if pierden and sum(pierden) < 0 else 99.0,
        "caida_max_r": round(caida, 1), "ops_mes": round(len(rs) / dias * 30, 1),
        # t = media / error típico: cuánto se aleja la media de 0 frente a lo que daría el azar.
        # Por debajo de 1 es indistinguible de la suerte; por encima de 2, bastante sólido.
        "t": round(media / (desv / math.sqrt(len(rs))), 2) if desv else 0.0,
    }


def puntuacion(st):
    """Para elegir parámetros en entrenamiento: rentabilidad media ajustada por número de casos."""
    if st["n"] < MIN_OPS_ENTRENAMIENTO:
        return -1e9
    return st["r_medio"] * math.sqrt(st["n"])


def supera(st_val, st_ent):
    return (st_val.get("n", 0) >= CRITERIO["min_ops_validacion"] and st_val["r_medio"] >= CRITERIO["r_medio_min"]
            and st_val["factor_beneficio"] >= CRITERIO["factor_beneficio_min"] and st_val.get("t", 0) >= CRITERIO["t_min"]
            and st_ent.get("r_medio", -1) > 0)


# ================================================================ simulación
class Mercado:
    """Datos de una moneda en un intervalo, con índice por tiempo."""

    def __init__(self, moneda, intervalo, velas, funding):
        self.moneda, self.intervalo = moneda, intervalo
        self.min = INTERVALOS[intervalo]
        self.ctx = Contexto(velas)
        self.funding = funding
        self.idx_t = {t: i for i, t in enumerate(self.ctx.t)}


DURACION_MAX_H = None                # tope global de duración del bot (riesgo.duracion_max_global_h)
COSTE_CFG = None                     # config para el filtro de costes (el mismo que aplica el bot)
SL_MIN_PCT, SL_MAX_PCT = 0.25, 8.0   # los mismos que usa el bot (config motor.sl_min_pct / sl_max_pct)


def simular(mercado, nombre, params, salida, barras_max, fees, extra_fn=None, btc=None, flujo=None, rasgos=False):
    ctx, tf = mercado.ctx, mercado.min * 60
    fee_taker, fee_maker = fees
    trades, ocupado = [], -1
    for i in range(60, len(ctx) - 1):
        if i <= ocupado:
            continue
        s = detectar(nombre, ctx, i, params, extra_fn(i) if extra_fn else None)
        if not s or not SL_MIN_PCT <= s["sl_pct"] <= SL_MAX_PCT:
            continue
        if COSTE_CFG is not None and costes_excesivos(COSTE_CFG, s["sl_pct"]):
            continue                 # el bot la descartaría: stop demasiado corto para lo que cuesta operar
        j, lado = i + 1, s["lado"]
        sg = 1 if lado == "L" else -1
        res = simular_entrada(mercado, j, lado, s["sl_pct"], salida, barras_max, fees)
        if res is None:
            break
        r, sim, k, entrada = res
        t = {"t": ctx.t[j], "moneda": mercado.moneda, "lado": lado, "r": round(r, 3), "cierre_ts": sim.cierre_ts,
             "salida": sim.motivo, "fuerza": s["fuerza"], "sl_pct": s["sl_pct"], "precio": entrada,
             "funding_r": round(sim.funding_r, 4), "deslizamiento_pct": round(sim.deslizamiento_pct, 4)}
        if rasgos:
            ra = ctx.rasgos(lado, i)
            ra["fuerza"] = s["fuerza"]
            ra["lado"] = sg
            if btc is not None and mercado.moneda != "BTC":
                bi = btc.idx_t.get(ctx.t[i])
                ra["btc_mov"] = sg * (btc.ctx.c[bi] / btc.ctx.c[bi - 4] - 1) * 100 if bi and bi >= 4 else 0.0
            ra["funding_contra"] = sg * (mercado.funding.media(ctx.t[i] + tf, 8) or 0.0) if mercado.funding else 0.0
            ra["flujo_ballenas"] = flujo.flujo(mercado.moneda, lado, ctx.t[i] + tf, 3600) / 1e5 if flujo else 0.0
            h = datetime.fromtimestamp(ctx.t[j], timezone.utc)
            ra["hora_sin"], ra["hora_cos"] = math.sin(2 * math.pi * h.hour / 24), math.cos(2 * math.pi * h.hour / 24)
            t["rasgos"] = ra
        trades.append(t)
        ocupado = k
    return trades


def simular_entrada(mercado, j, lado, sl_pct, salida, barras_max, fees):
    """Entrada en la apertura de la vela j con la gestión real del bot. Devuelve (r, sim, k_cierre, entrada)
    o None si la operación no llega a cerrarse con los datos disponibles."""
    if DURACION_MAX_H:   # el bot cierra por tiempo a lo que llegue antes: la duración de la estrategia o el tope global
        barras_max = min(barras_max, int(DURACION_MAX_H * 60 // mercado.min))
    ctx, tf = mercado.ctx, mercado.min * 60
    fee_taker, fee_maker = fees
    sg = 1 if lado == "L" else -1
    desl = deslizamiento_vela(ctx, j)
    entrada = ctx.o[j] * (1 + sg * desl)
    # Entrada supuesta a mercado (taker): más prudente que la ALO del bot. Salidas a mercado con deslizamiento.
    sim = SimSalida(lado, entrada, sl_pct, salida, ctx.t[j] + barras_max * tf, fee_taker, fee_taker, fee_maker,
                    desl_mercado=desl)
    k = j
    while k < len(ctx):
        if sim.paso(ctx.h[k], ctx.l[k], ctx.c[k], ctx.t[k] + tf, ctx.o[k] if k > j else None):
            break
        k += 1
    if not sim.cerrada:
        return None
    r = sim.resultado_r()
    sim.funding_r = 0.0
    if mercado.funding:  # funding pagado/cobrado mientras estuvo abierta
        sim.funding_r = sg * mercado.funding.suma(ctx.t[j], sim.cierre_ts) / sl_pct
        r -= sim.funding_r
    sim.deslizamiento_pct = desl * 100
    return r, sim, k, entrada


def simulador_base(mercados, salida, barras, fees):
    """Para las líneas base: simular_en(moneda, t, lado, sl_pct) -> R con la misma gestión, o None."""
    import bisect
    por_moneda = {m.moneda: m for m in mercados}

    def simular_en(moneda, t, lado, sl_pct):
        m = por_moneda.get(moneda)
        if m is None:
            return None
        j = bisect.bisect_left(m.ctx.t, t)
        if j < 60 or j >= len(m.ctx) - 1:
            return None
        res = simular_entrada(m, j, lado, sl_pct, salida, barras, fees)
        # Mismos datos, horizonte, costes, stop y salida que la estrategia; devuelve también cuándo cerró para que
        # la línea base no use precios de otra partición.
        return {"r": round(res[0], 3), "cierre_ts": res[1].cierre_ts} if res else None
    return simular_en


def extra_funding(mercado, params, umbrales):
    if not mercado.funding or umbrales.get(mercado.moneda) is None:
        return None
    alto, bajo = umbrales[mercado.moneda]
    tf = mercado.min * 60

    def fn(i):
        return {"funding_medio": mercado.funding.media(mercado.ctx.t[i] + tf, params["horas"]),
                "umbral_alto": alto, "umbral_bajo": bajo}
    return fn


def extra_fuerza(mercados, params):
    """Fuerza relativa: en cada vela, posición de cada moneda en el ranking de todas por su subida (o bajada)
    de las últimas 'lookback' velas, medida en ATR. Devuelve {moneda: fn(i)}."""
    tabla = rangos_fuerza([(m.moneda, m.ctx) for m in mercados], params["lookback"])

    def hacer(moneda):
        filas = tabla.get(moneda, {})

        def fn(i):
            return filas.get(i)
        return fn
    return {m.moneda: hacer(m.moneda) for m in mercados}


def extras_modulo(nombre, params, mercados, umbrales_por_param):
    """{moneda: función que da los datos extra de la vela i, o None si ese módulo no puede usarse en esa moneda}."""
    if nombre == "funding":
        u = umbrales_por_param(params)
        return {m.moneda: extra_funding(m, params, u) for m in mercados}
    if nombre == "fuerza":
        return extra_fuerza(mercados, params)
    return {m.moneda: (lambda i: None) for m in mercados}


def probar_modulo(nombre, intervalo, mercados, corte, c, fees, rapido, umbrales_por_param):
    mod = MODULOS[nombre]
    salida = dict(c["salida"], **mod["salida"])
    combos = combinaciones(mod["rejilla"])
    if rapido:
        combos = combos[::4]
    resultados = []
    for params in combos:
        trades = []
        extras = extras_modulo(nombre, params, mercados, umbrales_por_param)
        for m in mercados:
            fn = extras.get(m.moneda)
            if fn is None:
                continue
            trades += simular(m, nombre, params, salida, barras_max(nombre, params), fees, fn)
        ent = [t for t in trades if t["t"] < corte]
        val = [t for t in trades if t["t"] >= corte]
        resultados.append((puntuacion(estadisticas(ent)), params, ent, val))
    resultados.sort(key=lambda x: x[0], reverse=True)
    _, params, ent, val = resultados[0]
    return params, salida, estadisticas(ent), estadisticas(val), ent, val


def simular_combos(nombre, mercados, c, fees, rapido, umbrales_por_param, filtro_universo=None):
    """Todas las variantes de la rejilla del módulo: [(params, trades)]. Cada trade se marca con si su moneda
    estaba en el universo líquido EN ESE MOMENTO; las que no, se descartan."""
    mod = MODULOS[nombre]
    salida = dict(c["salida"], **mod["salida"])
    combos = combinaciones(mod["rejilla"])
    if rapido:
        combos = combos[::4]
    out = []
    for params in combos:
        trades = []
        extras = extras_modulo(nombre, params, mercados, umbrales_por_param)
        for m in mercados:
            fn = extras.get(m.moneda)
            if fn is None:
                continue
            trades += simular(m, nombre, params, salida, barras_max(nombre, params), fees, fn)
        if filtro_universo:
            trades = [t for t in trades if filtro_universo(t["moneda"], t["t"])]
        out.append((params, sorted(trades, key=lambda t: t["t"])))
    return salida, out


# ---------------------------------------------------------------- simulación en paralelo (todos los núcleos)
_TRABAJO = {}


def _umbrales(ms, limite, params):
    """Umbrales de funding con datos ANTERIORES al primer tramo de prueba (sin mirar el futuro)."""
    out = {}
    for m in ms:
        if m.funding:
            hist = m.funding.medias_historicas(limite, params["horas"])
            alto, bajo = umbrales_funding(hist, params["percentil"])
            out[m.moneda] = (alto, bajo) if alto and bajo else None
    return out


def _iniciar_trabajador(ms, c, limite):
    configurar_globales(c)
    _TRABAJO.update(ms=ms, c=c, limite=limite)


def _simular_modulo(tarea):
    nombre, fees, rapido = tarea
    ms, c, limite = _TRABAJO["ms"], _TRABAJO["c"], _TRABAJO["limite"]
    return nombre, simular_combos(nombre, ms, c, fees, rapido, lambda p: _umbrales(ms, limite, p), None)


def _copia_ligera(m):
    """Mercado sin las velas originales (los procesos solo necesitan las series): mucho menos que copiar."""
    import copy
    n = copy.copy(m)
    n.ctx = copy.copy(m.ctx)
    n.ctx.v = None
    return n


def simular_todos(nombres, ms, c, fees, rapido, limite, filtro_universo=None, procesos=0):
    """simular_combos de varios módulos repartidos entre los núcleos del ordenador. Mismo resultado que en serie
    (cada módulo es independiente); si el reparto falla por lo que sea, se hace en serie."""
    if procesos <= 0:
        procesos = max(1, min(6, (os.cpu_count() or 2) - 1))
    res = {}
    if procesos > 1 and len(nombres) > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor
            ligeros = [_copia_ligera(m) for m in ms]
            with ProcessPoolExecutor(max_workers=min(procesos, len(nombres)), initializer=_iniciar_trabajador,
                                     initargs=(ligeros, c, limite)) as ex:
                for nombre, r in ex.map(_simular_modulo, [(n, fees, rapido) for n in nombres]):
                    res[nombre] = r
        except Exception as e:  # noqa: BLE001
            print(f"  (Reparto entre núcleos no disponible: {type(e).__name__}; sigo en serie)")
            res = {}
    for n in nombres:
        if n not in res:
            res[n] = simular_combos(n, ms, c, fees, rapido, lambda p: _umbrales(ms, limite, p), None)
    if filtro_universo:
        res = {n: (sal, [(p, [t for t in tr if filtro_universo(t["moneda"], t["t"])]) for p, tr in combos])
               for n, (sal, combos) in res.items()}
    return res


def walk_forward(combos_trades, particion, embargo_seg):
    """Elige parámetros en cada tramo SOLO con datos anteriores (purgados) y junta lo que pasó después."""
    def elegir(inicio):
        return max(combos_trades, key=lambda pt: puntuacion(estadisticas(vd.entrenamiento_purgado(pt[1], inicio, embargo_seg))))
    oos, elecciones = [], []
    for ini, fin in particion["pliegues"]:
        params, trades = elegir(ini)
        # Purga + embargo: la operación debe abrir y CERRAR dentro del tramo (antes del embargo del final).
        tramo = vd.en_tramo_purgado(trades, ini, fin, embargo_seg)
        oos += tramo
        elecciones.append({"desde": fecha(ini), "hasta": fecha(fin), "params": params, "n": len(tramo)})
    ini_h, fin_h = particion["holdout"]
    params_finales, trades_finales = elegir(ini_h)
    return params_finales, oos, vd.en_tramo_purgado(trades_finales, ini_h, float("inf")), elecciones, trades_finales


def regimen_btc(btc):
    """Régimen de mercado en t según BTC: tendencia (precio frente a su media de 200 velas) y volatilidad
    (ATR frente a su media). Devuelve f(t) -> 'alcista/bajista · vol alta/baja' o None."""
    import bisect
    if btc is None:
        return lambda t: None
    ctx = btc.ctx
    ema = ctx.ema(200)

    def f(t):
        i = bisect.bisect_left(ctx.t, t) - 1
        if i < 200 or not ctx.atr_medio[i]:
            return None
        tend = "alcista" if ctx.c[i] > ema[i] else "bajista"
        vol = "vol alta" if ctx.atr[i] > ctx.atr_medio[i] else "vol baja"
        return f"{tend} · {vol}"
    return f


def experimento_regimen(trades_finales, oos, inicio_pruebas, embargo_seg, f_regimen):
    """Filtro de régimen SIN mirar el futuro: con las operaciones de entrenamiento (antes del primer tramo de
    prueba) se eligen los regímenes con media positiva; luego se aplica a lo de fuera de muestra."""
    ent = vd.entrenamiento_purgado(trades_finales, inicio_pruebas, embargo_seg)
    por = {}
    for t in ent:
        por.setdefault(f_regimen(t["t"]), []).append(t["r"])
    buenos = {k for k, v in por.items() if k is not None and len(v) >= 10 and sum(v) / len(v) > 0}
    filtrado = [t for t in oos if f_regimen(t["t"]) in buenos]
    return {"regimenes_elegidos": sorted(buenos), "sin_filtro": estadisticas(oos), "con_filtro": estadisticas(filtrado),
            "desglose_fuera_de_muestra": vd.desglose(oos, lambda t: f_regimen(t["t"])),
            "nota": "solo investigación: no se aplica en el bot"}


def carry_cubierto(mercados, fees, particion, embargo_seg, comision_spot_pct=0.07):
    """Carry de funding CUBIERTO (SOLO investigación, resultado MODELADO).
    Idea: cuando el funding es muy alto y positivo, ponerse corto en el perpetuo y largo en spot (o al revés si es muy
    negativo): el precio se neutraliza y se cobra el funding. Cada 8 h se mira la media del funding de las últimas
    24 h; se entra si, anualizada, supera 'umbral_anual_pct' y se sale cuando baja de la mitad o cambia de signo.
    Resultado de cada operación = funding cobrado − comisiones de 4 órdenes (2 en el perpetuo, 2 en spot),
    en % del tamaño. NO incluye: diferencias de precio entre spot y perpetuo (base), que no haya spot de esa moneda,
    préstamos para la pata corta en spot, ni el margen inmovilizado. Por eso nunca pasa de investigación."""
    rejilla = {"umbral_anual_pct": [10, 20, 40]}
    combos = combinaciones(rejilla)
    coste = 2 * fees[0] * 100 + 2 * comision_spot_pct
    resultados = []
    for p in combos:
        trades = []
        for m in mercados:
            fs = m.funding
            if not fs or len(fs.t) < 48:
                continue
            t, fin = fs.t[0] + 24 * 3600, fs.t[-1]
            abierta = None
            while t <= fin:
                media = fs.media(t, 24)
                anual = media * 24 * 365 if media is not None else None
                if abierta is None and anual is not None and abs(anual) >= p["umbral_anual_pct"]:
                    abierta = (t, 1 if anual > 0 else -1)          # 1: cobra con funding positivo (corto perpetuo)
                elif abierta is not None and (anual is None or anual * abierta[1] < p["umbral_anual_pct"] / 2):
                    cobrado = abierta[1] * fs.suma(abierta[0], t)  # % del tamaño
                    trades.append({"t": abierta[0], "cierre_ts": t, "moneda": m.moneda, "lado": "S" if abierta[1] > 0 else "L",
                                   "r": round(cobrado - coste, 4), "sl_pct": 1.0, "dias": round((t - abierta[0]) / 86400, 1)})
                    abierta = None
                t += 8 * 3600
        resultados.append((p, sorted(trades, key=lambda x: x["t"])))
    params, oos, ho, elecciones, _ = walk_forward(resultados, particion, embargo_seg)
    st_o, st_h = estadisticas(oos), estadisticas(ho)
    return {"params_finales": params, "fuera_de_muestra_pct": st_o, "holdout_pct": st_h, "elecciones": elecciones,
            "coste_ida_vuelta_pct": round(coste, 3), "unidad": "% del tamaño por operación (no R)",
            "solo_investigacion": True,
            "nota": "modelo simplificado: sin riesgo de base, sin comprobar que exista spot ni coste de préstamo"}

SPOT_EN_HYPERLIQUID = ("BTC", "ETH", "SOL", "HYPE")   # supuesto: pares de contado líquidos en Hyperliquid (revisar)


def descargar_spot(monedas, fin_ms, lab):
    """Velas de 8 h del CONTADO de Binance (con caché) para el estudio de carry. Solo lectura pública."""
    hx = dict(historia_externa.DEFECTOS, **(lab.get("historia_externa") or {}))
    fuente = historia_externa.BinanceSpot(historia_externa.Cliente(por_minuto=150))
    desde = fin_ms - int(hx["anos_4h"] * 365.25 * 86400_000)
    out = {}
    for mon in monedas:
        for sim in historia_externa.simbolos_candidatos(mon):
            try:
                if sim in fuente.simbolos():
                    out[mon] = historia_externa.velas_cacheadas(fuente, sim, "8h", desde, fin_ms)
                    break
            except Exception:  # noqa: BLE001
                break
    return out


def carry_realista(mercados_4h, spot, fees, particion, embargo_seg, inicio_hl, comision_spot_pct=0.07):
    """Carry de funding CUBIERTO, versión REALISTA (solo investigación):
      - solo funding POSITIVO: corto en el perpetuo + largo en contado (no hace falta pedir prestado);
      - incluye el riesgo de BASE: lo que se gana o pierde porque el perpetuo y el contado no se mueven igual
        (precio del perpetuo de Hyperliquid frente al contado de Binance, cada 8 h);
      - 4 comisiones (2 perpetuo + 2 contado) y rentabilidad sobre el capital de las DOS patas;
      - informa aparte: monedas con contado en Hyperliquid y solo el periodo con funding de Hyperliquid.
    Cada operación: 'r' = resultado en % del tamaño de una pata. spot: {moneda: [velas 8h de contado]}."""
    import bisect
    rejilla = {"umbral_anual_pct": [10, 20, 40]}
    coste = 2 * fees[0] * 100 + 2 * comision_spot_pct
    por_m = {m.moneda: m for m in mercados_4h}
    resultados = []
    for p in combinaciones(rejilla):
        trades = []
        for mon, vs in spot.items():
            m = por_m.get(mon)
            if m is None or not m.funding or len(vs) < 50:
                continue
            fs, ctx = m.funding, m.ctx
            st = [int(v["t"]) / 1000 for v in vs]
            sc = [float(v["c"]) for v in vs]

            def base(t):
                i = bisect.bisect_right(ctx.t, t - 4 * 3600) - 1         # vela de 4 h cerrada en t
                j = bisect.bisect_right(st, t - 8 * 3600) - 1           # vela de 8 h cerrada en t
                if i < 0 or j < 0 or abs(st[j] + 8 * 3600 - t) > 1 or abs(ctx.t[i] + 4 * 3600 - t) > 1:
                    return None
                return ctx.c[i] / sc[j] - 1
            muestra = [abs(x) for x in (base(tt) for tt in st[10:60]) if x is not None]
            if not muestra or sorted(muestra)[len(muestra) // 2] > 0.02:
                continue                       # el contado no es la misma moneda o la escala no cuadra: fuera
            t, fin = max(fs.t[0] + 24 * 3600, st[0] + 8 * 3600), min(fs.t[-1], st[-1] + 8 * 3600)
            t = t - t % (8 * 3600) + 8 * 3600
            abierta, peor = None, 0.0
            while t <= fin:
                media = fs.media(t, 24)
                anual = media * 24 * 365 if media is not None else None
                b = base(t)
                if abierta is None and anual is not None and anual >= p["umbral_anual_pct"] and b is not None:
                    abierta, peor = (t, b), 0.0
                elif abierta is not None:
                    if b is not None:
                        peor = min(peor, (abierta[1] - b) * 100)
                    if (anual is None or anual < p["umbral_anual_pct"] / 2) and b is not None:
                        cobrado = fs.suma(abierta[0], t)                   # % del tamaño (corto cobra positivo)
                        r = cobrado + (abierta[1] - b) * 100 - coste
                        trades.append({"t": abierta[0], "cierre_ts": t, "moneda": mon, "lado": "S", "r": round(r, 4),
                                       "funding_pct": round(cobrado, 4), "base_pct": round((abierta[1] - b) * 100, 4),
                                       "peor_base_pct": round(peor, 4), "dias": round((t - abierta[0]) / 86400, 1),
                                       "solo_hl": abierta[0] >= inicio_hl.get(mon, float("inf"))})
                        abierta = None
                t += 8 * 3600
        resultados.append((p, sorted(trades, key=lambda x: x["t"])))
    params, oos, ho, elecciones, _ = walk_forward(resultados, particion, embargo_seg)

    def resumen(ts):
        st = estadisticas(ts)
        if ts:
            dias = sum(x["dias"] for x in ts) or 1
            st.update(funding_medio_pct=round(sum(x["funding_pct"] for x in ts) / len(ts), 4),
                      base_media_pct=round(sum(x["base_pct"] for x in ts) / len(ts), 4),
                      peor_base_pct=round(min(x["peor_base_pct"] for x in ts), 3),
                      rentab_anual_capital_pct=round(sum(x["r"] for x in ts) / 2 / dias * 365, 2))
        return st
    hl = [x for x in oos if x["moneda"] in SPOT_EN_HYPERLIQUID]
    return {"params_finales": params, "elecciones": elecciones, "coste_ida_vuelta_pct": round(coste, 3),
            "fuera_de_muestra": resumen(oos), "holdout": resumen(ho),
            "solo_monedas_con_contado_en_hyperliquid": resumen(hl),
            "solo_funding_de_hyperliquid": resumen([x for x in oos if x["solo_hl"]]),
            "unidad": "r en % del tamaño de una pata; rentabilidad anual sobre el capital de las dos patas",
            "solo_investigacion": True,
            "nota": ("contado de Binance como referencia de la pata spot; operarlo exige contado en Hyperliquid "
                     f"(supuesto: {', '.join(SPOT_EN_HYPERLIQUID)}) y vigilar el margen del corto")}


def probar_libro(mercados_1h, fees):
    """SOLO investigación: ¿el desequilibrio del libro (compradores frente a vendedores en los 5 mejores niveles)
    anticipa la vela de 1 h siguiente? Usa las fotos del libro que el bot archiva. Sin evaluar en el pasado
    (Hyperliquid no tiene histórico del libro): hace falta acumular al menos MIN_DIAS_LIBRO días."""
    import bisect
    fotos = archivo.leer_libro(DIR_ARCHIVO)
    dias = len({archivo._dia(f["t"]) for f in fotos})
    if dias < archivo.MIN_DIAS_LIBRO:
        return {"estado": "no_evaluable", "dias": dias,
                "motivo": f"hay {dias} días de libro archivado; hacen falta {archivo.MIN_DIAS_LIBRO}"}
    por_m = {m.moneda: m for m in mercados_1h}
    ultima = {}
    for f in fotos:                       # una foto por moneda y vela: la última antes de que empiece la vela
        m = por_m.get(f.get("m"))
        d = archivo.desequilibrio(f)
        if m is None or d is None:
            continue
        j = bisect.bisect_right(m.ctx.t, f["t"])
        if j < len(m.ctx):
            ultima[(f["m"], j)] = (f["t"], d, (m.ctx.c[j] / m.ctx.o[j] - 1) * 1e4)
    filas = sorted(ultima.values())
    if len(filas) < 200:
        return {"estado": "no_evaluable", "dias": dias, "motivo": f"solo {len(filas)} observaciones"}
    corte = int(len(filas) * 0.6)         # umbrales con el 60 % antiguo, evaluación con el 40 % reciente
    ds = sorted(x[1] for x in filas[:corte])
    alto, bajo = ds[int(len(ds) * 0.8)], ds[int(len(ds) * 0.2)]
    prueba = filas[corte:]
    sube = [r for _, d, r in prueba if d >= alto]
    baja = [r for _, d, r in prueba if d <= bajo]
    xs, ys = [d for _, d, _ in prueba], [r for _, _, r in prueba]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx, vy = math.sqrt(sum((x - mx) ** 2 for x in xs)), math.sqrt(sum((y - my) ** 2 for y in ys))
    return {"estado": "investigacion", "dias": dias, "observaciones": len(filas), "evaluadas": len(prueba),
            "correlacion": round(cov / (vx * vy), 4) if vx and vy else None,
            "ret_1h_tras_desequilibrio_comprador_bps": round(sum(sube) / len(sube), 2) if sube else None,
            "ret_1h_tras_desequilibrio_vendedor_bps": round(sum(baja) / len(baja), 2) if baja else None,
            "coste_ida_vuelta_bps": round(2 * fees[0] * 1e4, 2),
            "nota": "si la diferencia entre ambos grupos no supera con holgura el coste, no hay nada aprovechable"}


def probar_externo(mercados_1h, fees, carpeta=None):
    """SOLO investigación: ¿los datos de Binance/Bybit que el bot archiva en vivo (contexto_externo.py) anticipan
    la vela de 1 h siguiente de Hyperliquid? Dos rasgos: diferencia de funding Hyperliquid − externo (%/h) y
    cambio del interés abierto externo en 1 h. Umbrales con el 60 % antiguo, evaluación en el 40 % reciente.
    Hace falta acumular al menos MIN_DIAS_LIBRO días (no hay histórico gratuito de interés abierto)."""
    import bisect

    import contexto_externo as cx
    carpeta = carpeta or os.path.join(AQUI, "datos_mercado")
    filas_arch = cx.leer_archivo(carpeta)
    dias = len({time.strftime("%Y-%m-%d", time.gmtime(f.get("t", 0))) for f in filas_arch})
    if dias < archivo.MIN_DIAS_LIBRO:
        return {"estado": "no_evaluable", "dias": dias,
                "motivo": f"hay {dias} días de datos de Binance/Bybit archivados; hacen falta {archivo.MIN_DIAS_LIBRO}"}
    por_m = {m.moneda: m for m in mercados_1h}
    out = {"estado": "investigacion", "dias": dias, "coste_ida_vuelta_bps": round(2 * fees[0] * 1e4, 2),
           "nota": "si la diferencia entre grupos no supera con holgura el coste, no hay nada aprovechable"}
    for rasgo in ("dif_funding_h_pct", "oi_externo_cambio_1h_pct"):
        ultima = {}
        for f in filas_arch:
            m, x = por_m.get(f.get("m")), f.get(rasgo)
            if m is None or x is None:
                continue
            j = bisect.bisect_right(m.ctx.t, f["t"])          # la vela que empieza DESPUÉS del dato
            if j < len(m.ctx):
                ultima[(f["m"], j)] = (f["t"], x, (m.ctx.c[j] / m.ctx.o[j] - 1) * 1e4)
        filas = sorted(ultima.values())
        if len(filas) < 200:
            out[rasgo] = {"estado": "no_evaluable", "motivo": f"solo {len(filas)} observaciones"}
            continue
        corte = int(len(filas) * 0.6)
        xs0 = sorted(x for _, x, _ in filas[:corte])
        alto, bajo = xs0[int(len(xs0) * 0.8)], xs0[int(len(xs0) * 0.2)]
        prueba = filas[corte:]
        arriba = [r for _, x, r in prueba if x >= alto]
        abajo = [r for _, x, r in prueba if x <= bajo]
        xs, ys = [x for _, x, _ in prueba], [r for _, _, r in prueba]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        vx, vy = math.sqrt(sum((a - mx) ** 2 for a in xs)), math.sqrt(sum((b - my) ** 2 for b in ys))
        out[rasgo] = {"observaciones": len(filas), "evaluadas": len(prueba),
                      "correlacion": round(cov / (vx * vy), 4) if vx and vy else None,
                      "ret_1h_quintil_alto_bps": round(sum(arriba) / len(arriba), 2) if arriba else None,
                      "ret_1h_quintil_bajo_bps": round(sum(abajo) / len(abajo), 2) if abajo else None}
    return out


def evaluar_cerebro(iv, d, ms, oos_por_clave, holdout_por_clave, part, embargo_seg, reg_ho, n_variantes, lab, c):
    """Fase 2: reúne las candidatas de TODAS las estrategias de este intervalo (fuera de muestra y holdout visible,
    con los parámetros elegidos sin mirar el futuro), les calcula los rasgos de la vista unificada y compara:
    todas · cerebro solo con Hyperliquid · cerebro con Binance/Bybit. Solo informe y evidencia."""
    import cerebro as cb
    import mercado_unificado as mu
    por_m = {m.moneda: m for m in ms}
    ext = d.get("externo") or {}
    series = {m: mu.SerieExterna(v) for (m, i), v in (ext.get("series") or {}).items() if i == iv}
    fund = {m: mu.FundingHorario(v) for m, v in (ext.get("funding") or {}).items()}
    cand = []
    for clave in oos_por_clave:
        if not clave.endswith("_" + iv) or clave.startswith("ballenas"):
            continue
        for t in oos_por_clave[clave] + holdout_por_clave.get(clave, []):
            m = por_m.get(t["moneda"])
            j = m.idx_t.get(t["t"]) if m else None
            if j is None or "cierre_ts" not in t:
                continue
            cand.append(dict(t, estrategia=clave, rasgos=mu.rasgos(m.ctx, j - 1, t["lado"], m.funding,
                                                                   series.get(t["moneda"]), fund.get(t["moneda"]))))
    cand.sort(key=lambda x: x["t"])
    print(f"\n================ CEREBRO ({iv}): {len(cand)} candidatas de todas las estrategias ================")
    if len(cand) < cb.MIN_ENTRENO:
        return {"estado": "no_evaluable", "motivo": f"solo {len(cand)} candidatas"}
    con_ext = sum(1 for x in cand if x["rasgos"].get("flujo1") is not None)
    # «Solo Hyperliquid» de verdad: rasgos HL Y candidatas/etiquetas solo de cuando ya había velas de Hyperliquid
    # (la historia anterior viene de Binance/Bybit y no prueba nada sobre la ejecución en HL).
    ini_hl = (d.get("inicio_hyperliquid") or {}).get(iv, {})
    cand_hl = [x for x in cand if x["t"] >= ini_hl.get(x["moneda"], float("inf"))]
    comp = cb.comparar(cand, part, embargo_seg, estadisticas)
    comp.pop("cerebro_solo_hyperliquid", None)
    comp_hl = cb.comparar(cand_hl, part, embargo_seg, estadisticas) if len(cand_hl) >= cb.MIN_ENTRENO else {}
    filas_comp = [("todas_las_candidatas", comp["todas_las_candidatas"], comp["todas_las_candidatas"]),
                  ("cerebro_con_todo", comp["cerebro_con_todo"], comp["todas_las_candidatas"])]
    if comp_hl:
        filas_comp += [("todas_solo_historia_hl", comp_hl["todas_las_candidatas"], None),
                       ("cerebro_solo_hyperliquid", comp_hl["cerebro_solo_hyperliquid"], comp_hl["todas_las_candidatas"])]
    out = {"candidatas": len(cand), "candidatas_solo_historia_hl": len(cand_hl), "candidatas_con_datos_externos": con_ext,
           "umbral_r": cb.UMBRAL_R}
    for nombre, r, base in filas_comp:
        fila = {k: v for k, v in r.items() if not k.startswith("_")}
        if "_oos" in r:
            oos, ho = r["_oos"], r["_holdout"]
            clave = f"cerebro_{nombre}_{iv}"
            # Línea base: TODAS las candidatas del mismo periodo fuera de muestra (el cerebro debe mejorar a
            # «operarlo todo»), y cartera con los límites reales del bot (una por moneda, máximo de posiciones...).
            pl = list(part["pliegues"])
            base_oos = [x for x in (cand_hl if nombre.endswith("hyperliquid") else cand)
                        if pl and pl[0][0] <= x["t"] < pl[-1][1]]
            fresco_total = reg_ho.fresco(ho, clave, iv)
            ho_vis, fresco, sello = vd.sellar_holdout(ho, fresco_total, vd.min_holdout())
            ver = vd.veredicto(oos, ho_vis, n_variantes, lab, {"operar todas las candidatas": base_oos}, None, None,
                               holdout_fresco=fresco,
                               escenarios=vd.escenarios_estres(oos, c["comisiones"]["taker_pct"], DESLIZAMIENTO * 100))
            cart = vd.cartera_produccion([dict(x, fuerza=x.get("prediccion", 0)) for x in oos], c["riesgo"])
            ver["comprobaciones"].append({"regla": "cartera con los límites del bot con resultado > 0",
                                          "cumple": cart["resultado_pct"] > 0, "valor": cart})
            if ver["estado"] in ("supera", "exploratorio") and cart["resultado_pct"] <= 0:
                ver.update(estado="no_supera", motivo="con los límites reales del bot (cartera) el resultado no es positivo")
            if sello["abierto"]:
                reg_ho.marcar(clave, part["holdout"][1])
            fila.update(veredicto=ver, estado=ver["estado"], holdout_reservado=sello, cartera=cart)
        out[nombre] = fila
        print(linea(nombre.replace("_", " ")[:22], fila["fuera_de_muestra"]) + (f"  · {fila['estado']}" if "estado" in fila else ""))
        if fila.get("cartera"):
            ct = fila["cartera"]
            print(f"      cartera con límites del bot: {ct['operaciones']} ops · {ct['resultado_pct']:+.2f} % · "
                  f"caída {ct['caida_max_pct']:.2f} % · descartadas {ct['descartadas']}")
    # Modelo final para el bot: entrenado con TODAS las candidatas ya cerradas (el bot lo usa en vivo).
    # Variante para el bot: la que SUPERA el laboratorio (si hay varias, la de mayor t); si ninguna, «con todo»
    # (en simulado/shadow sirve para acumular evidencia; a real no pasa nada sin aprobación humana).
    variantes = {"cerebro_solo_hyperliquid": mu.RASGOS_HL, "cerebro_con_todo": mu.RASGOS}
    supera = [k for k in variantes if (out.get(k) or {}).get("estado") == "supera"]
    elegida = max(supera, key=lambda k: (out[k]["fuera_de_muestra"] or {}).get("t") or 0) if supera else "cerebro_con_todo"
    base_ent = cand_hl if elegida == "cerebro_solo_hyperliquid" else cand      # el mismo universo que se validó
    final = cb.Modelo(variantes[elegida], {x["estrategia"] for x in base_ent}).entrenar(
        [(x["rasgos"], x["estrategia"], x["r"]) for x in base_ent])
    out["_modelo"] = dict(final.a_dict(), intervalo=iv, entrenado_hasta=max(x["cierre_ts"] for x in base_ent),
                          variante=elegida, estado_laboratorio=(out.get(elegida) or {}).get("estado"))
    print(f"  modelo para el bot: {elegida.replace('_', ' ')} ({out['_modelo']['estado_laboratorio'] or 'sin veredicto'})")
    pes = (out.get("cerebro_con_todo") or {}).get("pesos") or {}
    if pes:
        print("  pesos (R por desviación típica): " + ", ".join(f"{k} {v:+.3f}" for k, v in pes.items()))
    print(f"  {con_ext} de {len(cand)} candidatas tienen datos de Binance/Bybit. Solo evidencia: no activa nada.")
    return out


def cartera(trades, max_pos=2, riesgo=0.01):
    """Combina operaciones de varios módulos como lo haría el bot: máx. 2 a la vez, 1 por moneda, 1 % de riesgo."""
    abiertas, saldo, maximo, caida, hechas = [], 1.0, 1.0, 0.0, 0
    # A la misma hora, primero las más fuertes (como hace el bot).
    for t in sorted(trades, key=lambda x: (x["t"], -x.get("fuerza", 0))):
        for a in [a for a in abiertas if a["cierre_ts"] <= t["t"]]:
            abiertas.remove(a)
            saldo *= 1 + riesgo * a["r"]
            maximo = max(maximo, saldo)
            caida = max(caida, (maximo - saldo) / maximo)
        if len(abiertas) >= max_pos or any(a["moneda"] == t["moneda"] for a in abiertas):
            continue
        abiertas.append(t)
        hechas += 1
    for a in abiertas:
        saldo *= 1 + riesgo * a["r"]
    return {"operaciones": hechas, "resultado_pct": round((saldo - 1) * 100, 1), "caida_max_pct": round(caida * 100, 1)}


def con_aprendizaje(clave, todos, corte, c):
    """Simula el filtro de aprendizaje del bot sin mirar el futuro: cada operación de validación se decide
    con la memoria de las operaciones ANTERIORES ya cerradas (entrenamiento + validación previa)."""
    from aprendizaje import Memoria
    mem = Memoria(None, c["aprendizaje"], c["salida"], (0, 0, 0))
    mem.datos["registros"] = [{"tipo": "backtest", "moneda": t["moneda"], "lado": t["lado"], "modulo": clave,
                               "rasgos": t["rasgos"], "ballenas": [], "resultado_r": t["r"], "cierre_ts": t["cierre_ts"]}
                              for t in todos]
    filtradas = []
    for t in todos:
        if t["t"] >= corte and mem.valorar(t["moneda"], t["rasgos"], ahora=t["t"], modulo=clave)["abrir"]:
            filtradas.append(t)
    return filtradas


# ================================================================ ballenas
HORIZONTE_BALLENAS = 4      # velas de 1 h que se miran después de que una ballena cargue
MIN_EVENTOS_BALLENA = 8


def descargar_ballenas(api, c, inicio_ms, fin_ms):
    """Un grupo AMPLIO de ballenas candidatas (más que las que sigue el bot) con su historial de órdenes."""
    import requests

    from seleccion import URL_LEADERBOARD, elegir_ballenas, monedas_liquidas

    def post(cuerpo, peso):
        return api.post(cuerpo, peso, cache=False)

    def ranking():
        return requests.get(URL_LEADERBOARD, timeout=30).json().get("leaderboardRows", [])
    monedas = monedas_liquidas(post, c)
    actuales = elegir_ballenas(c, post, ranking, monedas, None, os.path.join(AQUI, "ballenas.json"))
    print(f"Buscando hasta {c['laboratorio'].get('ballenas_candidatas', 30)} ballenas candidatas (tarda unos minutos)...")
    pool = elegir_ballenas(c, post, ranking, monedas, None, None, numero=c["laboratorio"].get("ballenas_candidatas", 30))
    pool = list(dict.fromkeys(actuales + pool))
    if not pool:
        raise RuntimeError("no se encontraron ballenas")
    flujo = FlujoBallenas(retener_seg=10 ** 10)
    print(f"Descargando el historial de {len(pool)} ballenas...")
    for i, b in enumerate(pool, 1):
        fills = descargar_fills(api, b, inicio_ms, fin_ms)
        flujo.desde_fills(b, fills)
        print(f"  [{i}/{len(pool)}] {b[:10]}  {len(fills)} órdenes")
    flujo.preparar_indice()
    return flujo, pool


def eventos_ballena(flujo, ballena, mercados_1h, umbral_usd):
    """Horas en que una ballena cambió su posición en ≥ umbral en una moneda, con lo que hizo el precio
    en las HORIZONTE_BALLENAS horas siguientes, en ATR y a favor de lo que hizo la ballena."""
    out = []
    for m in mercados_1h:
        cola = flujo.eventos.get(m.moneda)
        if not cola:
            continue
        por_hora = defaultdict(float)
        for t, b, d, _ in cola:
            if b == ballena:
                por_hora[int(t // 3600 * 3600)] += d
        ctx = m.ctx
        for h, neto in por_hora.items():
            if abs(neto) < umbral_usd:
                continue
            i = m.idx_t.get(h)
            if i is None or i + HORIZONTE_BALLENAS >= len(ctx) or not ctx.atr[i]:
                continue
            s = 1 if neto > 0 else -1
            out.append({"t": h + 3600, "moneda": m.moneda,
                        "mov_atr": s * (ctx.c[i + HORIZONTE_BALLENAS] - ctx.c[i]) / ctx.atr[i]})
    return out


def puntuar(eventos):
    xs = [e["mov_atr"] for e in eventos]
    if len(xs) < 2:
        return {"n": len(xs), "media": round(xs[0], 3) if xs else 0.0, "t": 0.0}
    media = sum(xs) / len(xs)
    desv = math.sqrt(sum((x - media) ** 2 for x in xs) / (len(xs) - 1))
    return {"n": len(xs), "media": round(media, 3), "t": round(media / (desv / math.sqrt(len(xs))), 2) if desv else 0.0}


def es_predictiva(p):
    return p["n"] >= MIN_EVENTOS_BALLENA and p["media"] >= 0.05 and p["t"] >= 1.0


def simular_flujo(flujo, c, mercados_1h, fees, solo=None):
    """Opera la señal de flujo de las ballenas (todas, o solo las de 'solo') con la gestión real del bot."""
    cfg = c["ballenas"]
    salida = dict(c["salida"], **cfg["salida"])
    trades = []
    for m in mercados_1h:
        ctx, tf = m.ctx, 3600
        ocupado = -1
        for i in range(60, len(ctx) - 1):
            if i <= ocupado:
                continue
            s = flujo.senal(m.moneda, ctx.t[i] + tf, cfg, solo)
            if not s:
                continue
            j, lado = i + 1, s["lado"]
            sg = 1 if lado == "L" else -1
            sl = cfg["sl_atr"] * ctx.atr[i] / ctx.c[i] * 100
            if not SL_MIN_PCT <= sl <= SL_MAX_PCT:
                continue
            entrada = ctx.o[j] * (1 + sg * DESLIZAMIENTO)
            sim = SimSalida(lado, entrada, sl, salida, ctx.t[j] + cfg["barras_max"] * tf, fees[0], fees[0], fees[1])
            k = j
            while k < len(ctx):
                if sim.paso(ctx.h[k], ctx.l[k], ctx.c[k], ctx.t[k] + tf, ctx.o[k] if k > j else None):
                    break
                k += 1
            if not sim.cerrada:
                break
            ra = ctx.rasgos(lado, i)
            ra.update(fuerza=s["total_usd"] / 1e5, lado=sg, flujo_ballenas=s["total_usd"] / 1e5)
            trades.append({"t": ctx.t[j], "moneda": m.moneda, "lado": lado, "r": sim.resultado_r(), "cierre_ts": sim.cierre_ts,
                           "salida": sim.motivo, "sl_pct": sl, "precio": entrada, "rasgos": ra, "ballenas": s["ballenas"],
                           "fuerza": s["total_usd"] / 1e5})
            ocupado = k
    return trades


def probar_ballenas(flujo, pool, c, mercados_1h, fees):
    """Puntúa cada ballena por si sus cargas ANTICIPAN el precio (no por cuánto gana) y comprueba, con la
    segunda mitad del periodo, si seguir solo a las que puntúan bien funciona mejor que seguirlas a todas."""
    umbral = c["ballenas"]["aumento_min_usd"]
    ev = {b: eventos_ballena(flujo, b, mercados_1h, umbral) for b in pool}
    tiempos = [e["t"] for x in ev.values() for e in x]
    if not tiempos:
        raise RuntimeError("las ballenas no han operado en las monedas vigiladas")
    corte = min(tiempos) + 0.5 * (max(tiempos) - min(tiempos))
    punt_ent = {b: puntuar([e for e in x if e["t"] < corte]) for b, x in ev.items()}
    elegidas_ent = {b for b, p in punt_ent.items() if es_predictiva(p)}
    punt_todo = {b: puntuar(x) for b, x in ev.items()}
    predictivas = sorted((b for b, p in punt_todo.items() if es_predictiva(p)), key=lambda b: -punt_todo[b]["t"])

    print(f"\nPuntuación (movimiento del precio en las {HORIZONTE_BALLENAS} h siguientes a que carguen, en ATR):")
    for b in sorted(pool, key=lambda b: -punt_todo[b]["t"])[:12]:
        p = punt_todo[b]
        if p["n"]:
            print(f"  {b[:10]}  {p['n']:4d} cargas | media {p['media']:+.3f} ATR | t {p['t']:5.2f}"
                  f"{'  ✓ predictiva' if b in predictivas else ''}")
    print(f"  ... {len(predictivas)} de {len(pool)} ballenas anticipan el precio (media ≥ +0,05 ATR, t ≥ 1, "
          f"≥ {MIN_EVENTOS_BALLENA} cargas)")

    todas = simular_flujo(flujo, c, mercados_1h, fees)
    sel = simular_flujo(flujo, c, mercados_1h, fees, elegidas_ent) if elegidas_ent else []
    res = {
        "corte": corte,
        "todas": (estadisticas([t for t in todas if t["t"] < corte]), estadisticas([t for t in todas if t["t"] >= corte])),
        "predictivas": (estadisticas([t for t in sel if t["t"] < corte]), estadisticas([t for t in sel if t["t"] >= corte])),
        "n_elegidas_entrenamiento": len(elegidas_ent),
        "predictivas_finales": predictivas,
        "puntuaciones": {b: p for b, p in punt_todo.items() if p["n"]},
        "trades": todas,
        "trades_val_predictivas": [t for t in sel if t["t"] >= corte],
    }
    return res


# ================================================================ mercado (grabado por el bot)
MIN_DIAS_GRABADOS = 14


def _valor_en(serie, ts, t, idx):
    """Último valor de la serie (tuplas con t en [0], tiempos en 'ts') con t <= t_objetivo, en la posición idx."""
    import bisect
    k = bisect.bisect_right(ts, t) - 1
    return serie[k][idx] if k >= 0 and t - serie[k][0] <= 300 else None


def _neto_entre(acum, ts, t0, t1):
    import bisect
    return acum[bisect.bisect_left(ts, t1)] - acum[bisect.bisect_left(ts, t0)]


def probar_mercado(c, mercados_1h, fees):
    """Con lo que el bot ha grabado en datos_mercado/: ¿las cascadas de liquidaciones y el flujo de las
    operaciones grandes de todo el mercado anticipan el precio?"""
    from mercado import cargar_grabado, dias_grabados
    carpeta = os.path.join(AQUI, "datos_mercado")
    ctx_g, flujo_g = cargar_grabado(carpeta, "ctx"), cargar_grabado(carpeta, "flujo")
    dias = max((dias_grabados(v) for v in ctx_g.values()), default=0)
    print("\n================ MERCADO (grabado por el bot: interés abierto y operaciones grandes) ================")
    if dias < MIN_DIAS_GRABADOS:
        print(f"  Hay {dias:.1f} días grabados; hacen falta al menos {MIN_DIAS_GRABADOS}. "
              f"Deja el bot en marcha y repite el laboratorio más adelante.")
        return {}
    cfg = c["mercado"]["liquidaciones"]
    salida = dict(c["salida"], **cfg.get("salida", {}))
    trades, eventos = [], []
    for m in mercados_1h:
        serie = ctx_g.get(m.moneda)
        if not serie:
            continue
        ts_ctx = [x[0] for x in serie]
        fl = flujo_g.get(m.moneda) or []
        ts_fl, acum = [x[0] for x in fl], [0.0]
        for x in fl:
            acum.append(acum[-1] + x[1] - x[2])
        ctx, tf = m.ctx, 3600
        ocupado = -1
        for i in range(60, len(ctx) - 1):
            t_fin = ctx.t[i] + tf
            oi1, oi0 = _valor_en(serie, ts_ctx, t_fin, 1), _valor_en(serie, ts_ctx, t_fin - cfg["ventana_min"] * 60, 1)
            if not oi1 or not oi0:
                continue
            # Flujo de operaciones grandes en esa hora y movimiento posterior (4 h, en ATR)
            if fl and i + 4 < len(ctx) and ctx.atr[i]:
                neto = _neto_entre(acum, ts_fl, ctx.t[i], t_fin)
                if abs(neto) >= 500_000:
                    s_ = 1 if neto > 0 else -1
                    eventos.append(s_ * (ctx.c[i + 4] - ctx.c[i]) / ctx.atr[i])
            if i <= ocupado:
                continue
            oi = (oi1 / oi0 - 1) * 100
            mov = (ctx.c[i] / ctx.c[i - 1] - 1) * 100
            atr_pct = ctx.atr[i] / ctx.c[i] * 100
            if oi > -cfg["caida_oi_pct"] or abs(mov) < cfg["movimiento_atr"] * atr_pct:
                continue
            lado = "L" if mov < 0 else "S"
            sg = 1 if lado == "L" else -1
            sl = cfg["sl_atr"] * atr_pct
            if not SL_MIN_PCT <= sl <= SL_MAX_PCT:
                continue
            j = i + 1
            entrada = ctx.o[j] * (1 + sg * DESLIZAMIENTO)
            sim = SimSalida(lado, entrada, sl, salida, ctx.t[j] + cfg["barras_max"] * tf, fees[0], fees[0], fees[1])
            k = j
            while k < len(ctx):
                if sim.paso(ctx.h[k], ctx.l[k], ctx.c[k], ctx.t[k] + tf, ctx.o[k] if k > j else None):
                    break
                k += 1
            if not sim.cerrada:
                break
            trades.append({"t": ctx.t[j], "moneda": m.moneda, "lado": lado, "r": sim.resultado_r(), "cierre_ts": sim.cierre_ts,
                           "salida": sim.motivo, "sl_pct": sl, "precio": entrada, "fuerza": -oi})
            ocupado = k
    print(f"  {dias:.0f} días grabados.")
    if not trades:
        print("  Liquidaciones: ninguna cascada en ese periodo.")
        return {}
    trades.sort(key=lambda t: t["t"])
    corte = trades[0]["t"] + 0.5 * (trades[-1]["t"] - trades[0]["t"])
    st_e, st_v = estadisticas([t for t in trades if t["t"] < corte]), estadisticas([t for t in trades if t["t"] >= corte])
    print(linea("liquidaciones, 1ª mitad", st_e))
    print(linea("liquidaciones, 2ª mitad", st_v))
    if eventos:
        media = sum(eventos) / len(eventos)
        print(f"  Operaciones grandes (≥ 500k netos en 1 h): {len(eventos)} casos, el precio se movió {media:+.3f} ATR "
              f"a su favor en las 4 h siguientes.")
    return {"liquidaciones_1h": {"modulo": "liquidaciones", "intervalo": "1h", "params": cfg, "entrenamiento": st_e,
                                 "validacion": st_v, "supera": supera(st_v, st_e), "salida": salida,
                                 "barras_max": cfg["barras_max"], "dias_grabados": round(dias, 1)}}


# ================================================================ acciones (HIP-3)
def probar_acciones(api, c, fees, fin_ms, rapido):
    """Acciones, índices y materias primas que Hyperliquid ofrece como perpetuos en mercados de terceros
    (HIP-3, p. ej. "xyz:TSLA"). Solo informe: el bot todavía no las opera."""
    dexes = c["laboratorio"].get("dex_acciones") or []
    if not dexes:
        try:
            dexes = [d["name"] for d in api.post({"type": "perpDexs"}, peso=20, cache=False) if d and d.get("name")]
        except Exception as e:  # noqa: BLE001
            print(f"  No pude leer los mercados de terceros: {e}")
            return {}
    activos = []
    for dex in dexes:
        try:
            meta, ctxs = api.post({"type": "metaAndAssetCtxs", "dex": dex}, peso=20, cache=False)
        except Exception as e:  # noqa: BLE001
            print(f"  {dex}: no disponible ({e})")
            continue
        for a, x in zip(meta["universe"], ctxs):
            if a.get("isDelisted"):
                continue
            nombre = a["name"] if ":" in a["name"] else f"{dex}:{a['name']}"
            vol = float(x.get("dayNtlVlm") or 0)
            if vol >= c["laboratorio"].get("volumen_min_acciones", 1_000_000):
                activos.append((vol, nombre))
    activos = [n for _, n in sorted(activos, reverse=True)[: (6 if rapido else c["laboratorio"].get("max_acciones", 20))]]
    if not activos:
        print("  Ningún mercado de acciones con volumen suficiente.")
        return {}
    print("  " + ", ".join(activos))
    salida = {}
    for iv in ("4h", "1h"):
        ms = []
        for nombre in activos:
            try:
                velas = descargar_velas(api, nombre, iv, fin_ms - 5000 * MIN_INTERVALO[iv] * 60_000, fin_ms)
            except Exception as e:  # noqa: BLE001
                print(f"  {nombre} {iv}: sin velas ({e})")
                continue
            velas = [v for v in velas if int(v["t"]) + MIN_INTERVALO[iv] * 60_000 <= fin_ms]
            if len(velas) > 300:
                ms.append(Mercado(nombre, iv, velas, None))
        if not ms:
            continue
        t0, t1 = min(m.ctx.t[0] for m in ms), max(m.ctx.t[-1] for m in ms)
        corte = t0 + FRACCION_ENTRENAMIENTO * (t1 - t0)
        print(f"\n  --- velas de {iv} · {len(ms)} mercados · entrenamiento {fecha(t0)} → {fecha(corte)} · "
              f"validación {fecha(corte)} → {fecha(t1)}")
        for nombre in MODULOS:
            if nombre == "funding" or not usa_intervalo(nombre, iv):
                continue
            params, _, st_e, st_v, _, _ = probar_modulo(nombre, iv, ms, corte, c, fees, rapido, lambda p: {})
            ok = supera(st_v, st_e)
            salida[f"acciones_{nombre}_{iv}"] = {"modulo": nombre, "intervalo": iv, "params": params, "entrenamiento": st_e,
                                                  "validacion": st_v, "supera": ok, "mercados": [m.moneda for m in ms]}
            print(f"  {'✅' if ok else '❌'} {nombre.upper()} ({iv})  {params}")
            print(linea("  validación", st_v))
    return salida


# ================================================================ informe
def registrar_evidencias(c, resultados, run_id):
    """Añade la evidencia de esta ejecución a promociones.json. NUNCA cambia la etapa de una estrategia."""
    import logging

    from motor import Motor
    from promocion import Registro, huella_segura
    reg = Registro()
    motor = Motor(c, logging.getLogger("laboratorio"), os.path.join(AQUI, "estrategia_elegida.json"))
    for d in motor.todas_las_estrategias():
        r = resultados.get(d["clave"])
        if not r:
            continue
        fm = r.get("fuera_de_muestra") or r.get("validacion") or {}
        reg.registrar_evidencia(d["clave"], "laboratorio", huella_segura(d, c), run_id, {
            "estado": r.get("estado_evaluacion", "supera" if r.get("supera") else "no_supera"),
            "n": fm.get("n"), "r_medio": fm.get("r_medio"), "t": fm.get("t"),
            "holdout": (r.get("holdout") or {}).get("r_medio")})
    reg.guardar()
    print("Evidencia añadida a promociones.json (no cambia ninguna etapa; ver: python promocion.py estado).")


def linea(nombre, st):
    if not st.get("n"):
        return f"  {nombre:<22} sin operaciones"
    return (f"  {nombre:<22} {st['n']:5d} ops | {st['ganan_pct']:4.0f}% ganan | media {st['r_medio']:+.3f} R | "
            f"FB {st['factor_beneficio']:5.2f} | t {st.get('t', 0):5.2f} | caída {st['caida_max_r']:5.1f} R | "
            f"{st['ops_mes']:5.1f} ops/mes")


def preparar_datos(c, api, fin_ms, rapido=False):
    """Descarga (con caché) y prepara los datos que usan el laboratorio y el departamento de investigación.
    Devuelve dict con monedas, mercados {"1h", "4h"}, btc, veteranas y filtro_universo."""
    lab = c["laboratorio"]
    hx = dict(historia_externa.DEFECTOS, **(lab.get("historia_externa") or {}))
    ampliar = hx["activa"] and not rapido
    # ---------------------------------------------------------------- monedas
    u = c["universo"]
    print("Eligiendo las monedas más líquidas...")
    # Con historia ampliada, el grupo descargado incluye monedas con menos volumen HOY: muchas lo tuvieron en el
    # pasado. Si cada operación cuenta o no lo decide después el universo "en el momento" (con el mínimo de volumen).
    vol_pool = min(u["volumen_min_24h_usd"], hx["volumen_min_pool_usd"]) if ampliar else u["volumen_min_24h_usd"]
    universo = [x for x in descargar_universo(api) if x["volumen_24h"] >= vol_pool
                and x["moneda"] not in u["excluir"]]
    # Se descarga un grupo MÁS AMPLIO que el que usa el bot; cada operación solo cuenta si su moneda estaba
    # entre las más líquidas EN ESE MOMENTO (universo en el tiempo), no según el volumen de hoy.
    monedas = [x["moneda"] for x in universo[: (8 if rapido else max(u["max_monedas"], lab["monedas_pool"]))]]
    for m in u["incluir_siempre"]:
        if m not in monedas:
            monedas.append(m)
    print("  " + ", ".join(monedas))

    # ---------------------------------------------------------------- datos
    print("Descargando velas de 1 h y 4 h y funding (la primera vez tarda unos minutos)...")
    mercados = {"1h": [], "4h": []}
    extra_archivo = {}
    funding = {}
    inicio_hl = {"1h": {}, "4h": {}}      # primera vela de Hyperliquid (o del archivo propio) por moneda, en s
    amp = None
    if ampliar:
        print(f"  Historia ampliada con datos públicos de {' y '.join(hx['fuentes'])} "
              f"({hx['anos_4h']} años en 4 h, {hx['anos_1h']} en 1 h; Hyperliquid manda donde tiene datos)")
        amp = historia_externa.Ampliador(hx, historia_externa.crear_fuentes(hx["fuentes"]), fin_ms)
    for n, m in enumerate(monedas, 1):
        cerradas = {}
        for iv in ("4h", "1h"):             # 4 h primero: valida la fuente externa con más velas en común
            velas = descargar_velas(api, m, iv, fin_ms - 5000 * MIN_INTERVALO[iv] * 60_000, fin_ms)
            velas = [v for v in velas if int(v["t"]) + MIN_INTERVALO[iv] * 60_000 <= fin_ms]  # solo cerradas
            # Velas que el bot ha ido archivando: alargan la historia más allá de las 5000 que da Hyperliquid.
            propias = archivo.leer_velas(DIR_ARCHIVO, m, iv)
            if propias:
                antes = len(velas)
                velas = archivo.combinar(velas, [v for v in propias if int(v["t"]) + MIN_INTERVALO[iv] * 60_000 <= fin_ms])
                extra_archivo[iv] = extra_archivo.get(iv, 0) + len(velas) - antes
            if velas:
                inicio_hl[iv][m] = int(velas[0]["t"]) / 1000
            if amp is not None and velas:
                velas = amp.velas(m, iv, velas)
            cerradas[iv] = velas
        fr = descargar_funding_cacheado(api, m, fin_ms - c["laboratorio"]["dias_funding"] * 86400_000, fin_ms)
        if amp is not None:
            fr = amp.funding(m, fr)
        funding[m] = SerieFunding(fr) if fr else None
        for iv in ("1h", "4h"):
            if len(cerradas[iv]) > 300:
                mercados[iv].append(Mercado(m, iv, cerradas[iv], funding[m]))
        ext = (amp.informe.get(m) or {}) if amp else {}
        nota = (f" · +{ext.get('4h', {}).get('velas_externas', 0)} velas 4 h y "
                f"+{ext.get('1h', {}).get('velas_externas', 0)} de 1 h de {ext['fuente']}") if ext.get("fuente") else ""
        print(f"  [{n}/{len(monedas)}] {m}{nota}")
    if extra_archivo:
        print("  Velas añadidas desde el archivo propio del bot: " +
              ", ".join(f"{iv}: {n}" for iv, n in extra_archivo.items()))
    if amp is not None:
        con = [m for m, x in amp.informe.items() if x.get("fuente")]
        print(f"  Historia externa aceptada en {len(con)} de {len(monedas)} monedas.")
        for m, x in amp.informe.items():
            if not x.get("fuente") and x.get("rechazos"):
                print(f"    {m}: sin historia externa ({x['rechazos'][-1]})")
    btc = {iv: next((x for x in mercados[iv] if x.moneda == "BTC"), None) for iv in mercados}
    # Monedas "veteranas": con historia casi completa. Las que entraron hace poco en el top de volumen suelen
    # haber subido mucho (por eso están ahí): si una estrategia solo gana con ellas, es sesgo de supervivencia.
    largo = max((len(m.ctx) for m in mercados["4h"]), default=0)
    veteranas = {m.moneda for m in mercados["4h"] if len(m.ctx) >= 0.9 * largo}
    print(f"  Monedas veteranas (historia completa): {', '.join(sorted(veteranas)) or '—'}")

    # ---------------------------------------------------------------- universo en el tiempo
    filtro_universo = {}
    for iv in ("1h", "4h"):
        vols = {m.moneda: [(t, float(v.get("v") or 0) * cl) for t, v, cl in zip(m.ctx.t, m.ctx.v, m.ctx.c)]
                for m in mercados[iv]}
        n_univ = min(u["max_monedas"], len(vols)) if not rapido else len(vols)
        # Igual que el bot en vivo: además de estar entre las n de más volumen, la moneda debía superar el
        # volumen mínimo diario EN ESE MOMENTO (media de los 30 días anteriores).
        vol_min = u["volumen_min_24h_usd"] * 30 if ampliar else 0.0
        filtro_universo[iv] = vd.universo_en_el_tiempo(vols, n_univ, 30 * 86400, vol_min_ventana=vol_min)

    return {"monedas": monedas, "mercados": mercados, "btc": btc, "veteranas": veteranas,
            "filtro_universo": filtro_universo, "u": u, "inicio_hyperliquid": inicio_hl,
            "historia_externa": (amp.informe if amp else {}),
            "externo": {"series": amp.series, "funding": amp.funding_ext} if amp else {"series": {}, "funding": {}}}


def configurar_globales(c):
    """Mismos límites que el bot: stop mínimo/máximo, filtro de costes y duración máxima."""
    global SL_MIN_PCT, SL_MAX_PCT, COSTE_CFG, DURACION_MAX_H
    SL_MIN_PCT, SL_MAX_PCT = c["riesgo"]["sl_min_pct"], c["riesgo"]["sl_max_pct"]
    COSTE_CFG = c
    DURACION_MAX_H = c["riesgo"]["duracion_max_global_h"]


def main():
    consola_utf8()
    ap = argparse.ArgumentParser(description="Laboratorio de estrategias con datos reales de Hyperliquid")
    ap.add_argument("--guardar-memoria", action="store_true", help="preentrena aprendizaje.json con los casos")
    ap.add_argument("--rapido", action="store_true", help="menos monedas y parámetros")
    ap.add_argument("--sin-cache", action="store_true")
    ap.add_argument("--sin-ballenas", action="store_true", help="no descarga el historial de las ballenas")
    ap.add_argument("--presupuesto", type=int, default=1000,
                    help="peso de peticiones por minuto (Hyperliquid admite 1200 en total; con el bot en marcha usa ~300)")
    ap.add_argument("--acciones", action="store_true", help="prueba también acciones e índices (mercados HIP-3, solo informe)")
    args = ap.parse_args()

    c, _ = conf.migrar(conf.cargar(os.path.join(AQUI, "config.json")))
    c = conf.validar(c, entorno={})          # el laboratorio no opera: solo necesita una configuración coherente
    lab = c["laboratorio"]
    configurar_globales(c)
    api = Api(usar_cache=not args.sin_cache, presupuesto=max(50, min(1100, args.presupuesto)))
    fees = (c["comisiones"]["taker_pct"] / 100, c["comisiones"]["maker_pct"] / 100)
    run_id = "lab-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fin = int(time.time() // 3600 * 3600)
    fin_ms = fin * 1000

    d = preparar_datos(c, api, fin_ms, args.rapido)
    monedas, mercados, btc, veteranas, filtro_universo = (d["monedas"], d["mercados"], d["btc"], d["veteranas"],
                                                         d["filtro_universo"])

    # ---------------------------------------------------------------- módulos (walk-forward)
    embargo = {iv: lab["embargo_barras"] * MIN_INTERVALO[iv] * 60 for iv in ("1h", "4h")}
    n_esta = sum(len(combinaciones(MODULOS[n]["rejilla"])[:: (4 if args.rapido else 1)])
                 for iv in mercados if mercados[iv] for n in MODULOS if usa_intervalo(n, iv))
    # Número EFECTIVO de variantes: todas las probadas desde siempre (laboratorio + investigación nocturna).
    reg_var = vd.RegistroVariantes(os.path.join(AQUI, "laboratorio_variantes.json"))
    n_variantes = reg_var.añadir([
        vd.RegistroVariantes.id_variante("lab", {"modulo": n, "intervalo": iv, "version": MODULOS[n].get("version"),
                                                 "params": p})
        for iv in mercados if mercados[iv] for n in MODULOS if usa_intervalo(n, iv)
        for p in combinaciones(MODULOS[n]["rejilla"])[:: (4 if args.rapido else 1)]])
    reg_var.guardar()
    reg_ho = vd.RegistroHoldout(os.path.join(AQUI, "laboratorio_holdout.json"))
    print(f"\nVariantes en esta ejecución: {n_esta} · probadas desde siempre (lab + investigación): {n_variantes}. "
          f"El umbral de t se corrige por el total (Student, una cola; con 60 operaciones: "
          f"t ≥ {vd.t_requerido(n_variantes, n_ops=60):.2f}).")
    resultados, elegidos, casos_memoria, todos_val = {}, [], [], []
    oos_por_clave, regimen_por_iv, holdout_por_clave = {}, {}, {}
    particiones = {}
    for iv in ("4h", "1h"):
        ms = mercados[iv]
        if not ms:
            continue
        t0, t1 = min(m.ctx.t[0] for m in ms), max(m.ctx.t[-1] for m in ms)
        part = vd.particionar(t0, t1, lab["pliegues"], lab["fraccion_holdout"])
        particiones[iv] = part
        inicio_pruebas = part["pliegues"][0][0]

        def umbrales_por_param(params, ms=ms, limite=inicio_pruebas):
            out = {}
            for m in ms:
                if m.funding:
                    # Umbrales con datos ANTERIORES al primer tramo de prueba: sin mirar el futuro.
                    hist = m.funding.medias_historicas(limite, params["horas"])
                    alto, bajo = umbrales_funding(hist, params["percentil"])
                    out[m.moneda] = (alto, bajo) if alto and bajo else None
            return out

        print(f"\n================ VELAS DE {iv.upper()} · {fecha(t0)} → {fecha(t1)} · {lab['pliegues']} tramos de "
              f"prueba desde {fecha(inicio_pruebas)} · holdout {fecha(part['holdout'][0])} → {fecha(t1)} ================")
        f_reg = regimen_btc(btc[iv])
        t_sim = time.time()
        simulados = simular_todos([n for n in MODULOS if usa_intervalo(n, iv)], ms, c, fees, args.rapido,
                                  inicio_pruebas, filtro_universo[iv], int(lab.get("procesos", 0)))
        print(f"  Simulación de todas las variantes en {time.time() - t_sim:.0f} s.")
        for nombre in MODULOS:
            if not usa_intervalo(nombre, iv):
                continue
            clave = f"{nombre}_{iv}"
            salida, combos = simulados[nombre]
            params, oos, holdout, elecciones, trades_finales = walk_forward(combos, part, embargo[iv])
            bm = barras_max(nombre, params)
            sim_base = simulador_base(ms, salida, bm, fees)
            fin_oos = part["holdout"][0] - embargo[iv]
            baselines = {"entrada aleatoria": vd.linea_base_aleatoria(oos, sim_base, lab["semilla"], inicio_pruebas, fin_oos),
                         "siempre largo": vd.linea_base_largo(oos, sim_base, fin_oos)}
            escenarios = vd.escenarios_estres(oos, c["comisiones"]["taker_pct"], DESLIZAMIENTO * 100)
            vet = [t for t in oos if t["moneda"] in veteranas]
            ini_hl = d.get("inicio_hyperliquid", {}).get(iv, {})
            solo_hl = [t for t in oos if t["t"] >= ini_hl.get(t["moneda"], 0)]
            # Holdout ACUMULATIVO: la parte aún no mirada queda SELLADA (ni se ve ni se marca) hasta que reúne
            # suficientes operaciones; solo entonces se abre, cuenta como confirmación independiente y se marca.
            fresco_total = reg_ho.fresco(holdout, clave, iv)
            holdout, fresco, sello = vd.sellar_holdout(holdout, fresco_total, vd.min_holdout())
            if not sello["abierto"]:
                corte = reg_ho.visto_hasta(clave, iv) or part["holdout"][0]
                trades_finales = [t for t in trades_finales if t["t"] < corte]
            ver = vd.veredicto(oos, holdout, n_variantes, lab, baselines, None, vet, holdout_fresco=fresco,
                               escenarios=escenarios)
            if sello["abierto"]:
                reg_ho.marcar(clave, part["holdout"][1])      # mirado: deja de ser independiente
            elif ver["estado"] == "exploratorio":
                ver["motivo"] = (f"cumple lo demás; el holdout nuevo está SELLADO acumulando datos "
                                 f"({sello['n_reservado']} de {sello['minimo']} operaciones). Se abrirá solo al llegar al "
                                 f"mínimo; hasta entonces es exploratorio y no cuenta como evidencia.")
            oos_por_clave[clave] = oos
            holdout_por_clave[clave] = holdout
            regimen_por_iv[iv] = f_reg
            ok = ver["estado"] == "supera"
            st_oos, st_ho = estadisticas(oos), estadisticas(holdout)
            # Memoria de aprendizaje: rasgos de las operaciones con los parámetros finales (solo para preentrenar).
            todos = []
            if args.guardar_memoria or c["aprendizaje"].get("activo", True):
                extras = extras_modulo(nombre, params, ms, umbrales_por_param)
                for m in ms:
                    fn = extras.get(m.moneda)
                    if fn is not None:
                        todos += [dict(t, modulo=clave) for t in
                                  simular(m, nombre, params, salida, bm, fees, fn, btc[iv], None, True)]
                todos.sort(key=lambda t: t["t"])
                if not sello["abierto"]:
                    todos = [t for t in todos if t["t"] < corte]      # lo sellado tampoco entra en la memoria
            val_ap = con_aprendizaje(clave, todos, inicio_pruebas, c) if todos else []
            val_ap = [t for t in val_ap if t["t"] < part["holdout"][0]]
            resultados[clave] = {
                "modulo": nombre, "intervalo": iv, "version": MODULOS[nombre].get("version"), "params": params,
                "salida": salida, "barras_max": bm, "variantes": len(combos),
                "elecciones_por_tramo": elecciones, "fuera_de_muestra": st_oos, "holdout": st_ho,
                "validacion": st_oos,   # nombre antiguo (el panel y el motor lo leen)
                "entrenamiento": estadisticas(vd.entrenamiento_purgado(trades_finales, part["holdout"][0], embargo[iv])),
                "validacion_con_aprendizaje": estadisticas(val_ap),
                "validacion_veteranas": estadisticas(vet),
                "fuera_de_muestra_solo_hyperliquid": estadisticas(solo_hl),
                "lineas_base": {k: estadisticas(v) for k, v in baselines.items()},
                "escenarios_estres": {k: dict(estadisticas(v["trades"]), descripcion=v["descripcion"])
                                      for k, v in escenarios.items()},
                "exposicion": vd.exposicion_lados(oos),
                "estabilidad": vd.estabilidad(oos),
                "holdout_independiente": {"n": len(fresco), "estadisticas": estadisticas(fresco)},
                "holdout_reservado": sello,
                "por_mercado": vd.desglose(oos, lambda t: t["moneda"]),
                "por_lado": vd.desglose(oos, lambda t: t["lado"]),
                "por_regimen": vd.desglose(oos, lambda t: f_reg(t["t"])),
                "experimento_regimen": experimento_regimen(trades_finales, oos, inicio_pruebas, embargo[iv], f_reg),
                "veredicto": ver, "supera": ok, "estado_evaluacion": ver["estado"],
            }
            marca = {"supera": "✅", "no_supera": "❌", "no_evaluable": "⚪", "exploratorio": "🔎"}[ver["estado"]]
            print(f"\n{marca} {nombre.upper()} ({iv})  parámetros finales: {params}  · {ver['estado'].replace('_', ' ')}")
            print(linea("fuera de muestra", st_oos))
            print(linea("holdout final", st_ho))
            if sello["abierto"]:
                print(linea("holdout no consultado antes", estadisticas(fresco)))
            else:
                print(f"  {'holdout reservado':<22} {sello['n_reservado']} operaciones nuevas SELLADAS (se abrirá con "
                      f"{sello['minimo']}; no se mira su resultado)")
            for k, v in escenarios.items():
                print(linea(f"estrés: {v['descripcion']}", estadisticas(v["trades"])))
            for k, v in baselines.items():
                print(linea(f"base: {k}", estadisticas(v)))
            for comp in ver.get("comprobaciones", []):
                if not comp["cumple"]:
                    print(f"      ✗ {comp['regla']} ({comp['valor']})")
            if ver["estado"] in ("no_evaluable", "exploratorio"):
                print(f"      {ver['motivo']}")
            if ok:
                elegidos.append(resultados[clave])
                todos_val += [dict(t, modulo=clave) for t in oos]
            if args.guardar_memoria:
                casos_memoria += todos

    # ---------------------------------------------------------------- cerebro: un solo modelo con todos los datos
    reg_var.añadir(["cerebro:v1:solo_hyperliquid", "cerebro:v1:con_todo"])
    reg_var.guardar()
    informe_cerebro = {}
    for iv in particiones:
        try:
            informe_cerebro[iv] = evaluar_cerebro(iv, d, mercados[iv], oos_por_clave, holdout_por_clave, particiones[iv],
                                                  embargo[iv], reg_ho, reg_var.total(), lab, c)
        except Exception as e:  # noqa: BLE001  (el cerebro es informe: nunca tumba el laboratorio)
            informe_cerebro[iv] = {"error": f"{type(e).__name__}: {e}"}
            print(f"\n(No se pudo evaluar el cerebro en {iv}: {e})")

    # ---------------------------------------------------------------- estudios comparativos (solo informe)
    estudios_lab = {}
    try:
        import estudios
        btc_d = {}
        if btc.get("4h") is not None:
            ctx = btc["4h"].ctx
            cierres = {}
            for t, cl in zip(ctx.t, ctx.c):
                cierres[estudios._dia(t)] = cl
            dias = sorted(cierres)
            btc_d = {d: math.log(cierres[d] / cierres[p]) for p, d in zip(dias, dias[1:])}
        reg = regimen_por_iv.get("4h") or regimen_por_iv.get("1h")
        tendencia = {k: v for k, v in oos_por_clave.items() if k.startswith(("fuerza_", "momentum_"))}
        estudios_lab["fuerza_y_momentum"] = estudios.aportacion(tendencia, btc_d, reg)
        estudios_lab["todas"] = estudios.aportacion(oos_por_clave, btc_d, reg)
        estudios_lab["reversion_por_regimen"] = {k: estudios.resumen(v, btc_d, reg).get("por_regimen")
                                                 for k, v in oos_por_clave.items() if k.startswith("reversion_")}
        print("\n================ APORTACIÓN Y SOLAPAMIENTO (fuera de muestra; solo informe) ================")
        for k, r in estudios_lab["fuerza_y_momentum"]["estrategias"].items():
            m = r.get("marginal") or {}
            print(f"  {k:<16} {r['n']:>4} ops · {r['r_total']:+.1f} R · caída {r['caida_max_r']:.1f} R · "
                  f"corr. BTC {r.get('correlacion_con_btc')} · aporta {m.get('r_total_aportado', '—')} R a la combinación · "
                  f"solapamiento {r['solapamiento']}")
        for k, pr in estudios_lab["reversion_por_regimen"].items():
            print(f"  {k} por régimen de BTC: " + ", ".join(f"{g}: {v['n']} ops {v['r_medio']:+.3f} R" for g, v in (pr or {}).items()))
    except Exception as e:  # noqa: BLE001
        print(f"\n(No se pudieron calcular los estudios comparativos: {e})")

    # ---------------------------------------------------------------- experimentos de investigación
    investigacion = {}
    if mercados["4h"] and "4h" in particiones:
        try:
            investigacion["carry_cubierto"] = carry_cubierto(mercados["4h"], fees, particiones["4h"], embargo["4h"])
            r = investigacion["carry_cubierto"]
            print("\n================ INVESTIGACIÓN: carry de funding cubierto (modelado, % del tamaño) ================")
            print(linea("fuera de muestra", r["fuera_de_muestra_pct"]).replace(" R", " %"))
            print(linea("holdout", r["holdout_pct"]).replace(" R", " %"))
            print(f"  umbral elegido: {r['params_finales']['umbral_anual_pct']} % anual · coste por operación "
                  f"{r['coste_ida_vuelta_pct']} % · {r['nota']}")
        except Exception as e:  # noqa: BLE001
            print(f"\n(No se pudo probar el carry de funding: {e})")
        try:
            spot = descargar_spot([m.moneda for m in mercados["4h"]], fin_ms, lab)
            ini_hl = {m: t for m, t in (d.get("inicio_hyperliquid") or {}).get("4h", {}).items()}
            r = carry_realista(mercados["4h"], spot, fees, particiones["4h"], embargo["4h"], ini_hl)
            investigacion["carry_realista"] = r
            print("\n================ INVESTIGACIÓN: carry de funding REALISTA (con base y contado) ================")
            for k in ("fuera_de_muestra", "holdout", "solo_monedas_con_contado_en_hyperliquid", "solo_funding_de_hyperliquid"):
                x = r[k]
                print(linea(k.replace("_", " ")[:22], x).replace(" R", " %") +
                      (f" · funding {x['funding_medio_pct']} % · base {x['base_media_pct']} % · peor base "
                       f"{x['peor_base_pct']} % · {x['rentab_anual_capital_pct']} %/año s/capital" if x.get("n") else ""))
            print(f"  {r['nota']}")
        except Exception as e:  # noqa: BLE001
            print(f"\n(No se pudo probar el carry realista: {e})")
    if mercados["1h"]:
        investigacion["libro_1h"] = probar_libro(mercados["1h"], fees)
        r = investigacion["libro_1h"]
        print("\n================ INVESTIGACIÓN: desequilibrio del libro de órdenes ================")
        print(f"  {r['motivo']}" if r.get("motivo") else
              f"  correlación {r['correlacion']} · tras desequilibrio comprador {r['ret_1h_tras_desequilibrio_comprador_bps']} pb, "
              f"vendedor {r['ret_1h_tras_desequilibrio_vendedor_bps']} pb · coste ida y vuelta {r['coste_ida_vuelta_bps']} pb")
    if mercados["1h"]:
        investigacion["binance_bybit_1h"] = probar_externo(mercados["1h"], fees)
        r = investigacion["binance_bybit_1h"]
        print("\n================ INVESTIGACIÓN: datos de Binance y Bybit (funding e interés abierto) ================")
        if r.get("motivo"):
            print(f"  {r['motivo']}")
        else:
            for k in ("dif_funding_h_pct", "oi_externo_cambio_1h_pct"):
                x = r.get(k) or {}
                print(f"  {k}: " + (x.get("motivo") or f"correlación {x.get('correlacion')} · quintil alto "
                                     f"{x.get('ret_1h_quintil_alto_bps')} pb, bajo {x.get('ret_1h_quintil_bajo_bps')} pb"))
            print(f"  coste ida y vuelta {r['coste_ida_vuelta_bps']} pb")
    investigacion["pendientes"] = {
        "carry_funding": "modelado como investigación (carry_cubierto); falta medir el riesgo de base y qué monedas "
                         "tienen spot en Hyperliquid antes de poder operarlo",
        "flujo_de_ordenes_y_libro": "se evalúa con el libro que archiva el bot (datos_archivo/); hasta acumular "
                                    "14 días queda como no evaluable",
        "pares": "prioridad baja: pendiente",
    }

    # ---------------------------------------------------------------- ballenas
    predictivas, puntuaciones = [], {}
    if not args.sin_ballenas and mercados["1h"]:
        try:
            flujo, pool = descargar_ballenas(api, c, fin_ms - c["laboratorio"]["dias_ballenas"] * 86400_000, fin_ms)
            print("\n================ BALLENAS (flujo de grandes cuentas, historial reciente) ================")
            rb = probar_ballenas(flujo, pool, c, mercados["1h"], fees)
            (_, v_all), (e_sel, v_sel) = rb["todas"], rb["predictivas"]
            print(f"\nCopiar su flujo · periodo de validación desde {fecha(rb['corte'])}:")
            print(linea("todas las ballenas", v_all))
            print(linea(f"solo predictivas ({rb['n_elegidas_entrenamiento']})", v_sel))
            # Criterio antiguo (débil): como mucho 'exploratorio'. Las ballenas siguen en INVESTIGACIÓN hasta tener
            # evidencia suficiente fuera de muestra y en paper (historial de pocas semanas, sin holdout propio).
            prometedor = supera(v_sel, e_sel)
            ok = False
            predictivas, puntuaciones = rb["predictivas_finales"], rb["puntuaciones"]
            resultados["ballenas_1h"] = {
                "modulo": "ballenas", "intervalo": "1h", "params": c["ballenas"], "entrenamiento": e_sel,
                "validacion": v_sel, "validacion_todas": v_all, "supera": ok,
                "estado_evaluacion": "exploratorio" if prometedor else "no_supera",
                "salida": dict(c["salida"], **c["ballenas"]["salida"]), "barras_max": c["ballenas"]["barras_max"],
                "nota": "historial de las ballenas de pocas semanas: evidencia más débil que la de los módulos"}
            print(("🔎 " if prometedor else "❌ ") + "Seguir solo a las ballenas predictivas: " +
                  ("prometedor pero EXPLORATORIO (sigue en investigación)." if prometedor else
                   "no supera la validación (sigue en investigación)."))
            if ok:
                elegidos.append(resultados["ballenas_1h"])
                todos_val += [dict(t, modulo="ballenas_1h") for t in rb["trades_val_predictivas"]]
            if args.guardar_memoria:
                casos_memoria += [dict(t, modulo="ballenas_1h") for t in rb["trades"]]
            # ¿Sirven como veto? Operaciones validadas de los módulos con las ballenas (predictivas) en contra.
            solo = set(predictivas) or None
            cerca = [t for t in todos_val if t["moneda"] in flujo.eventos and t["t"] >= rb["corte"] - 30 * 86400]
            if cerca:
                en_contra = [t for t in cerca if flujo.flujo(t["moneda"], t["lado"], t["t"], 3600, solo)
                             <= -c["ballenas"]["veto_usd"]]
                resto = [t for t in cerca if t not in en_contra]
                print(linea("módulos, ballenas en contra", estadisticas(en_contra)))
                print(linea("módulos, resto", estadisticas(resto)))
        except Exception as e:  # noqa: BLE001
            print(f"\n(No se pudo analizar a las ballenas: {e})")

    # ---------------------------------------------------------------- mercado grabado
    if mercados["1h"]:
        try:
            for clave, r in probar_mercado(c, mercados["1h"], fees).items():
                # Liquidaciones (inferidas) con pocos días grabados: como mucho exploratorio, nunca 'supera'.
                r["estado_evaluacion"] = "exploratorio" if r.get("supera") else "no_supera"
                r["supera"] = False
                resultados[clave] = r
        except Exception as e:  # noqa: BLE001
            print(f"\n(No se pudo analizar lo grabado del mercado: {e})")

    # ---------------------------------------------------------------- acciones
    acciones = {}
    if args.acciones:
        print("\n================ ACCIONES E ÍNDICES (mercados HIP-3 de Hyperliquid · solo informe) ================")
        try:
            acciones = probar_acciones(api, c, fees, fin_ms, args.rapido)
        except Exception as e:  # noqa: BLE001
            print(f"  (No se pudieron probar las acciones: {e})")

    # ---------------------------------------------------------------- resumen
    print("\n================ RESUMEN ================")
    cart = None
    if elegidos:
        cart = vd.cartera_produccion(todos_val, c["riesgo"])
        print(f"Módulos que superan la validación: {', '.join(e['modulo'] + '_' + e['intervalo'] for e in elegidos)}")
        print(f"Juntos fuera de muestra con los límites del bot (máx. {c['riesgo']['max_posiciones_abiertas']} a la vez, "
              f"{c['riesgo']['riesgo_por_operacion_pct']} % de riesgo por operación): {cart['operaciones']} operaciones, "
              f"{cart['resultado_pct']:+.1f}% y peor caída -{cart['caida_max_pct']:.1f}%. Descartes: {cart['descartadas']}")
        print("Superar el laboratorio es EVIDENCIA, no permiso: el paso a real lo decide una persona (promocion.py).")
    else:
        mejor = max((r for r in resultados.values() if r.get("validacion", {}).get("n")),
                    key=lambda r: r["validacion"]["r_medio"], default=None)
        print("Ningún módulo ha superado la validación. Nada cambia: el bot sigue en simulado/shadow acumulando datos.")
        if mejor:
            print(f"El más prometedor: {mejor['modulo']} ({mejor['intervalo']}), {mejor['validacion']['r_medio']:+.3f} R "
                  f"de media fuera de muestra (no basta para afirmar ventaja).")

    # ---------------------------------------------------------------- reproducibilidad y holdout
    from promocion import hash_codigo
    hash_dat = vd.hash_datos(mercados["1h"] + mercados["4h"])
    reg_ho.guardar()
    usos = reg_ho.d["experimentos"]
    ejecucion = {"run_id": run_id, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "hash_config": conf.hash_config(c),
                 "hash_codigo": hash_codigo(), "semilla": lab["semilla"], "hash_datos": hash_dat,
                 "universo_descargado": monedas, "veteranas": sorted(veteranas), "variantes": n_variantes,
                 "particiones": {iv: {"pliegues": [[fecha(a), fecha(b)] for a, b in p["pliegues"]],
                                      "holdout": [fecha(p["holdout"][0]), fecha(p["holdout"][1])]}
                                 for iv, p in particiones.items()},
                 "holdout_visto": {k: v for k, v in usos.items()},
                 "variantes_esta_ejecucion": n_esta,
                 "historia_externa": d.get("historia_externa") or {},
                 "limitaciones": ["universo reconstruido SOLO con monedas que existen hoy: faltan las retiradas "
                                  "(sesgo de supervivencia; la reconstrucción del universo histórico NO es completa)",
                                  "dentro de una vela no se conoce el orden de precios (si tocó stop y objetivo, se "
                                  "cuenta el stop)", "sin cola ni libro histórico: fills modelados"]
                 + (["antes de que cada moneda cotizara en Hyperliquid se usan precios, volumen (escalado) y funding de "
                     "Binance/Bybit: aproximan a Hyperliquid, no son idénticos (ver 'fuera_de_muestra_solo_hyperliquid')"]
                    if d.get("historia_externa") else []),
                 "resultados": {k: {"estado": r.get("estado_evaluacion", "supera" if r.get("supera") else "no_supera"),
                                    "n_fuera_de_muestra": (r.get("fuera_de_muestra") or r.get("validacion") or {}).get("n"),
                                    "r_medio": (r.get("fuera_de_muestra") or r.get("validacion") or {}).get("r_medio")}
                                for k, r in resultados.items()}}

    salida_json = {"generado": datetime.now(timezone.utc).isoformat(timespec="seconds"), "monedas": monedas,
                   "run_id": run_id, "ejecucion": ejecucion, "cartera": cart, "investigacion": investigacion,
                   "estudios": estudios_lab,
                   "cerebro": {iv: {k: v for k, v in r.items() if k != "_modelo"} for iv, r in informe_cerebro.items()},
                   "criterio": {"walk_forward": True, "pliegues": lab["pliegues"], "holdout": lab["fraccion_holdout"],
                                "embargo_barras": lab["embargo_barras"],
                                "t_requerido_60_ops": round(vd.t_requerido(n_variantes, n_ops=60), 2),
                                "variantes_efectivas": n_variantes,
                                "min_ops_fuera_de_muestra": 30, "anterior": CRITERIO},
                   "elegidos": elegidos, "resultados": resultados,
                   "ballenas_predictivas": predictivas, "puntuacion_ballenas": puntuaciones, "acciones": acciones}
    salida_json["veteranas"] = sorted(veteranas)
    modelos = {iv: r["_modelo"] for iv, r in informe_cerebro.items() if isinstance(r, dict) and r.get("_modelo")}
    if modelos:
        import hashlib
        doc = {"version": "cerebro-" + run_id, "generado": salida_json["generado"], "modelos": modelos}
        doc["huella"] = hashlib.sha256(json.dumps(modelos, sort_keys=True).encode()).hexdigest()[:16]
        with open(os.path.join(AQUI, "cerebro_modelo.json.tmp"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        reemplazar(os.path.join(AQUI, "cerebro_modelo.json.tmp"), os.path.join(AQUI, "cerebro_modelo.json"))
        print(f"Modelo del cerebro guardado (cerebro_modelo.json · {doc['version']} · huella {doc['huella']}).")
    for nombre, datos in (("estrategia_elegida.json", salida_json),):
        ruta = os.path.join(AQUI, nombre)
        with open(ruta + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(datos, fh, indent=2, ensure_ascii=False)
        reemplazar(ruta + ".tmp", ruta)
    print("\nGuardado en estrategia_elegida.json (el bot en marcha lo recarga solo en unos minutos).")
    os.makedirs(os.path.join(AQUI, "registro"), exist_ok=True)
    with open(os.path.join(AQUI, "registro", "laboratorio_ejecuciones.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ejecucion, ensure_ascii=False, default=str) + "\n")
    registrar_evidencias(c, resultados, run_id)
    if todos_val:
        with open(os.path.join(AQUI, "laboratorio_validacion.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["t", "fecha", "modulo", "moneda", "lado", "r", "salida"])
            w.writeheader()
            for t in sorted(todos_val, key=lambda x: x["t"]):
                w.writerow({"t": int(t["t"]), "fecha": fecha(t["t"]), "modulo": t["modulo"], "moneda": t["moneda"],
                            "lado": t["lado"], "r": t["r"], "salida": t["salida"]})

    if args.guardar_memoria and casos_memoria:
        from aprendizaje import ruta_lab
        ruta = ruta_lab(os.path.join(AQUI, "aprendizaje.json"))
        casos_memoria.sort(key=lambda x: x["t"])
        casos_memoria = casos_memoria[-c["laboratorio"]["max_casos_memoria"]:]
        nuevos = [{"id": f"lab{n}", "tipo": "backtest", "moneda": t["moneda"], "lado": t["lado"], "modulo": t["modulo"],
                   "ts": t["t"], "rasgos": t.get("rasgos", {}), "ballenas": t.get("ballenas", []), "motivos_descarte": [],
                   "entrada": t["precio"], "sl_pct": t["sl_pct"], "resultado_r": t["r"], "cierre": t["salida"],
                   "cierre_ts": t["cierre_ts"], "sim": None} for n, t in enumerate(casos_memoria)]
        with open(ruta + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"generado": salida_json["generado"], "registros": nuevos}, fh)
        reemplazar(ruta + ".tmp", ruta)
        print(f"Memoria preentrenada con {len(nuevos)} casos históricos (aprendizaje_lab.json; el bot la recarga solo).")

    print("\nAVISOS:")
    print(" - Universo 'en el momento' dentro de las monedas que existen hoy: las retiradas (delisted) no tienen datos,")
    print("   así que queda algo de sesgo de supervivencia: el pasado puede parecer mejor de lo que fue.")
    print(" - Dentro de una vela no se sabe el orden de los precios; si tocó stop y objetivo, se cuenta el stop.")
    print(" - Solo cuenta lo que funciona FUERA DE MUESTRA y en el holdout. Aun así, el futuro puede ser distinto,")
    print("   y superar el laboratorio no autoriza nada: el paso a real lo aprueba una persona con promocion.py.")


if __name__ == "__main__":
    main()
