"""
Departamento de investigación: busca estrategias nuevas por su cuenta, con un examen muy duro.

Cómo funciona (cada ejecución, normalmente una vez al día en el servidor):
  1. GENERA ideas combinando piezas sencillas: un disparador (ruptura, cruce de medias, momentum, RSI, bandas,
     compresión de volatilidad, vela grande), 0–2 filtros (tendencia, eficiencia, volatilidad, régimen de BTC),
     una forma de salir y un stop. Nunca repite una idea ya probada (las fallidas van al CEMENTERIO).
  2. EXAMINA cada idea con datos históricos reales:
       - etapa 1, fuera de muestra (todo lo anterior al tramo final reservado, sin el arranque inicial):
         suficientes operaciones, media > 0, t por encima del umbral CORREGIDO POR TODAS LAS IDEAS PROBADAS
         DESDE EL PRINCIPIO (no solo las de hoy), intervalo de confianza > 0 y positiva con costes ×1,5;
       - etapa 2, solo para las que pasan: holdout final, líneas base (azar y siempre largo) y monedas veteranas.
     Cuantas más ideas se prueban, más exigente es el listón: así no se "encuentra" nada por pura suerte.
  3. Las que aprueban pasan a ASPIRANTES: el bot las sigue en vivo, sin órdenes ni dinero (como shadow), con
     datos que nadie ha visto. Con 30 operaciones: si fallan, al cementerio; si aguantan, quedan "listas para
     revisión" y TÚ decides si se convierten en una estrategia del bot (nada se activa solo).
  4. Escribe un informe (registro/investigacion_informe.json y .md) y avisa por Telegram solo si hay novedades.

Uso:
  python investigacion.py                        # una ronda (por defecto 60 ideas, máx. 90 min)
  python investigacion.py --ideas 20 --rapido    # prueba corta
"""

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA_REGISTRO = os.path.join(AQUI, "investigacion.json")
DIR_REGISTRO = os.path.join(AQUI, "registro")
MAX_ASPIRANTES = 10
MIN_OPS_ETAPA1 = 40
OPS_JUICIO_SHADOW = 30

# ============================================================ gramática de ideas
DISPARADORES = {
    "ruptura": {"n": [10, 20, 40, 60]},
    "cruce_medias": {"rapida": [5, 10, 20], "lenta": [30, 50, 100, 200]},
    "momentum": {"lookback": [12, 24, 48, 96, 168], "umbral_atr": [1.0, 2.0, 3.0, 5.0]},
    "rsi": {"n": [7, 14], "nivel": [20, 25, 30], "modo": ["revertir", "seguir"]},
    "banda": {"ema": [20, 50], "k": [1.5, 2.0, 3.0], "modo": ["revertir", "seguir"]},
    "compresion": {"ratio": [0.6, 0.8], "n": [10, 20]},
    "vela_grande": {"k": [1.5, 2.0, 3.0], "modo": ["revertir", "seguir"]},
}
FILTROS = {
    "tendencia": {"ema": [100, 200]},
    "eficiencia_alta": {"min": [0.3, 0.4]},
    "eficiencia_baja": {"max": [0.2, 0.3]},
    "volatilidad_alta": {"min": [1.2]},
    "volatilidad_baja": {"max": [0.8]},
    "a_favor_btc": {},
}
SALIDAS = {
    "corta": {"salida": {"tp1_r": 1.0, "tp_final_r": 2.0, "trailing_r": 0.8}, "barras": 12},
    "media": {"salida": {"tp1_r": 1.5, "tp_final_r": 3.0, "trailing_r": 1.0}, "barras": 30},
    "larga": {"salida": {"tp1_r": 2.0, "tp_final_r": 4.0, "trailing_r": 1.5}, "barras": 60},
    "sin_objetivo": {"salida": {"parcial_activo": False, "tp1_r": 98, "tp_final_r": 99, "trailing_r": 99,
                                "breakeven_tras_r": 99, "sin_objetivo": True}, "barras": 40},
}
SL_ATR = [1.5, 2.0, 3.0]
INTERVALOS = ["1h", "4h"]


def id_idea(d):
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]


def generar(rng, ya_probadas, n):
    """n ideas nuevas al azar dentro de la gramática (sin repetir ninguna ya probada)."""
    out, intentos = [], 0
    while len(out) < n and intentos < n * 50:
        intentos += 1
        nombre = rng.choice(sorted(DISPARADORES))
        disp = {"tipo": nombre, **{k: rng.choice(v) for k, v in DISPARADORES[nombre].items()}}
        if nombre == "cruce_medias" and disp["rapida"] >= disp["lenta"]:
            continue
        filtros = []
        for f in rng.sample(sorted(FILTROS), rng.choice([0, 1, 1, 2])):
            filtros.append({"tipo": f, **{k: rng.choice(v) for k, v in FILTROS[f].items()}})
        tipos = {f["tipo"] for f in filtros}
        if {"eficiencia_alta", "eficiencia_baja"} <= tipos or {"volatilidad_alta", "volatilidad_baja"} <= tipos:
            continue
        d = {"disparador": disp, "filtros": sorted(filtros, key=lambda f: f["tipo"]),
             "salida": rng.choice(sorted(SALIDAS)), "sl_atr": rng.choice(SL_ATR), "intervalo": rng.choice(INTERVALOS)}
        k = id_idea(d)
        if k in ya_probadas or any(x["id"] == k for x in out):
            continue
        out.append({"id": k, "def": d})
    return out


def describir(d):
    """Descripción legible de una idea."""
    p = d["disparador"]
    t = p["tipo"]
    base = {
        "ruptura": f"rompe el máximo/mínimo de {p.get('n')} velas",
        "cruce_medias": f"cruce de medias {p.get('rapida')}/{p.get('lenta')}",
        "momentum": f"se ha movido más de {p.get('umbral_atr')} ATR en {p.get('lookback')} velas (a favor)",
        "rsi": f"RSI({p.get('n')}) sale de {p.get('nivel')}/{100 - p.get('nivel', 0)} ({p.get('modo')})",
        "banda": f"cierra a más de {p.get('k')} ATR de la media {p.get('ema')} ({p.get('modo')})",
        "compresion": f"volatilidad comprimida (<{p.get('ratio')}× la media) y rompe {p.get('n')} velas",
        "vela_grande": f"vela de más de {p.get('k')} ATR ({p.get('modo')})",
    }[t]
    fil = {"tendencia": lambda f: f"a favor de la media {f['ema']}", "eficiencia_alta": lambda f: f"tendencia limpia (≥{f['min']})",
           "eficiencia_baja": lambda f: f"mercado lateral (≤{f['max']})", "volatilidad_alta": lambda f: "volatilidad alta",
           "volatilidad_baja": lambda f: "volatilidad baja", "a_favor_btc": lambda f: "a favor de la tendencia de BTC"}
    extras = [fil[f["tipo"]](f) for f in d["filtros"]]
    return (f"[{d['intervalo']}] {base}" + (" · " + ", ".join(extras) if extras else "") +
            f" · salida {d['salida']} · stop {d['sl_atr']} ATR")


# ============================================================ detección (la usan el examen y el bot en vivo)
def _disparo(p, ctx, i):
    t = p["tipo"]
    c, a = ctx.c[i], ctx.atr[i]
    if not a:
        return None
    if t == "ruptura":
        n = p["n"]
        if i < n + 1:
            return None
        if c > max(ctx.h[i - n:i]):
            return "L"
        if c < min(ctx.l[i - n:i]):
            return "S"
    elif t == "cruce_medias":
        r, le = ctx.ema(p["rapida"]), ctx.ema(p["lenta"])
        if r[i - 1] <= le[i - 1] and r[i] > le[i]:
            return "L"
        if r[i - 1] >= le[i - 1] and r[i] < le[i]:
            return "S"
    elif t == "momentum":
        lb = p["lookback"]
        if i < lb:
            return None
        mov = (c - ctx.c[i - lb]) / a
        mov_prev = (ctx.c[i - 1] - ctx.c[i - 1 - lb]) / ctx.atr[i - 1] if ctx.atr[i - 1] else 0
        if mov >= p["umbral_atr"] > mov_prev:            # solo cuando CRUZA el umbral (no en todas las velas)
            return "L"
        if mov <= -p["umbral_atr"] < mov_prev:
            return "S"
    elif t == "rsi":
        rsi = ctx.rsi(p["n"])
        bajo, alto = p["nivel"], 100 - p["nivel"]
        sube = rsi[i - 1] < bajo <= rsi[i]
        baja = rsi[i - 1] > alto >= rsi[i]
        if p["modo"] == "revertir":
            return "L" if sube else "S" if baja else None
        return "S" if sube else "L" if baja else None
    elif t == "banda":
        e = ctx.ema(p["ema"])[i]
        fuera_arriba = c > e + p["k"] * a and ctx.c[i - 1] <= e + p["k"] * ctx.atr[i - 1]
        fuera_abajo = c < e - p["k"] * a and ctx.c[i - 1] >= e - p["k"] * ctx.atr[i - 1]
        if p["modo"] == "seguir":
            return "L" if fuera_arriba else "S" if fuera_abajo else None
        return "S" if fuera_arriba else "L" if fuera_abajo else None
    elif t == "compresion":
        n = p["n"]
        if i < n + 1 or not ctx.atr_medio[i - 1] or ctx.atr[i - 1] >= p["ratio"] * ctx.atr_medio[i - 1]:
            return None
        if c > max(ctx.h[i - n:i]):
            return "L"
        if c < min(ctx.l[i - n:i]):
            return "S"
    elif t == "vela_grande":
        cuerpo = c - ctx.o[i]
        if abs(cuerpo) < p["k"] * a:
            return None
        lado = "L" if cuerpo > 0 else "S"
        return lado if p["modo"] == "seguir" else ("S" if lado == "L" else "L")
    return None


def _pasa_filtros(filtros, ctx, i, lado, btc_alcista):
    for f in filtros:
        t = f["tipo"]
        if t == "tendencia":
            e = ctx.ema(f["ema"])[i]
            if (lado == "L") != (ctx.c[i] > e):
                return False
        elif t == "eficiencia_alta" and ctx.er[i] < f["min"]:
            return False
        elif t == "eficiencia_baja" and ctx.er[i] > f["max"]:
            return False
        elif t in ("volatilidad_alta", "volatilidad_baja"):
            if not ctx.atr_medio[i]:
                return False
            r = ctx.atr[i] / ctx.atr_medio[i]
            if (t == "volatilidad_alta" and r < f["min"]) or (t == "volatilidad_baja" and r > f["max"]):
                return False
        elif t == "a_favor_btc":
            if btc_alcista is None or (lado == "L") != btc_alcista:
                return False
    return True


def detectar(defn, ctx, i, btc_alcista=None):
    """Señal de una idea en la vela i: {"lado", "sl_pct", "fuerza", "motivo"} o None."""
    if i < 205:
        return None
    lado = _disparo(defn["disparador"], ctx, i)
    if not lado or not _pasa_filtros(defn["filtros"], ctx, i, lado, btc_alcista):
        return None
    return {"lado": lado, "sl_pct": defn["sl_atr"] * ctx.atr[i] / ctx.c[i] * 100, "fuerza": 1.0,
            "motivo": "aspirante: " + describir(defn)}


def salida_de(defn, salida_base):
    s = SALIDAS[defn["salida"]]
    return dict(salida_base, **s["salida"]), s["barras"]


def btc_alcista_en(btc_ctx, t):
    """¿BTC estaba por encima de su media de 200 velas en el instante t? (None si no se sabe)."""
    if btc_ctx is None:
        return None
    j = bisect.bisect_right(btc_ctx.t, t) - 1
    if j < 200:
        return None
    return btc_ctx.c[j] > btc_ctx.ema(200)[j]


# ============================================================ examen
def simular_idea(defn, mercados, btc, c, fees, filtro_universo=None):
    import laboratorio as lab
    from riesgo import costes_excesivos
    salida, barras = salida_de(defn, c["salida"])
    trades = []
    for m in mercados:
        ctx = m.ctx
        ocupado = -1
        for i in range(205, len(ctx) - 1):
            if i <= ocupado:
                continue
            s = detectar(defn, ctx, i, btc_alcista_en(btc.ctx if btc else None, ctx.t[i]))
            if not s or not lab.SL_MIN_PCT <= s["sl_pct"] <= lab.SL_MAX_PCT or costes_excesivos(c, s["sl_pct"]):
                continue
            if filtro_universo and not filtro_universo(m.moneda, ctx.t[i + 1]):
                continue
            res = lab.simular_entrada(m, i + 1, s["lado"], s["sl_pct"], salida, barras, fees)
            if res is None:
                break
            r, sim, k, entrada = res
            trades.append({"t": ctx.t[i + 1], "cierre_ts": sim.cierre_ts, "moneda": m.moneda, "lado": s["lado"],
                           "r": round(r, 3), "sl_pct": s["sl_pct"]})
            ocupado = k
    return sorted(trades, key=lambda x: x["t"]), salida, barras


def examinar(defn, datos, c, fees, n_total, lab_cfg, reg_holdout=None):
    """Devuelve (estado, metricas): estado 'aspirante' | 'cementerio'.
    n_total: variantes probadas desde siempre (laboratorio + investigación). reg_holdout: RegistroHoldout para
    saber qué parte del holdout de la investigación ya se ha mirado (no se presenta como confirmación)."""
    import laboratorio as lab
    import validacion as vd
    iv = defn["intervalo"]
    ms = datos["mercados"][iv]
    if not ms:
        return "cementerio", {"motivo": "sin datos (no evaluable)", "no_evaluable": True}
    btc = datos["btc"][iv]
    trades, salida, barras = simular_idea(defn, ms, btc, c, fees, datos["filtro_universo"][iv])
    t0, t1 = min(m.ctx.t[0] for m in ms), max(m.ctx.t[-1] for m in ms)
    part = vd.particionar(t0, t1, lab_cfg["pliegues"], lab_cfg["fraccion_holdout"])
    ini, ho_ini = part["pliegues"][0][0], part["holdout"][0]
    embargo = lab_cfg["embargo_barras"] * lab.MIN_INTERVALO[iv] * 60
    # Purga + embargo: solo operaciones que abren y CIERRAN antes del holdout (sin usar sus precios).
    oos = vd.en_tramo_purgado(trades, ini, ho_ini, embargo)
    rs = [x["r"] for x in oos]
    met = {"n": len(rs), "r_medio": round(sum(rs) / len(rs), 4) if rs else None}
    if len(rs) < MIN_OPS_ETAPA1:
        return "cementerio", dict(met, motivo=f"pocas operaciones ({len(rs)}): no evaluable", no_evaluable=True)
    t = vd.estadistico_t(rs)
    req = vd.t_requerido(n_total, n_ops=len(rs))
    ic = vd.bootstrap_bloques(rs, lab_cfg["bloque_bootstrap"], 300, lab_cfg["semilla"])
    esc = vd.escenarios_estres(oos, c["comisiones"]["taker_pct"], lab.DESLIZAMIENTO * 100)
    estres = [x["r"] for x in esc["costes_x2"]["trades"]]
    met.update(t=round(t, 2), t_requerido=round(req, 2), ic=ic,
               r_medio_estres=round(sum(estres) / len(estres), 4) if estres else None)
    if met["r_medio"] <= 0:
        return "cementerio", dict(met, motivo="media fuera de muestra ≤ 0")
    if t < req:
        return "cementerio", dict(met, motivo=f"t {t:.2f} < {req:.2f} (Student, corregido por {n_total} variantes probadas)")
    if not ic or ic["inferior"] <= 0:
        return "cementerio", dict(met, motivo="intervalo de confianza incluye 0")
    if not estres or met["r_medio_estres"] <= 0:
        return "cementerio", dict(met, motivo="pierde con costes al doble")
    # Etapa 2: holdout, líneas base comparables, escenarios de estrés y veteranas (solo las que pasan la etapa 1)
    holdout = vd.en_tramo_purgado(trades, ho_ini, float("inf"))
    sim_base = lab.simulador_base(ms, salida, barras, fees)
    bases = {"entrada aleatoria": vd.linea_base_aleatoria(oos, sim_base, lab_cfg["semilla"], ini, ho_ini - embargo),
             "siempre largo": vd.linea_base_largo(oos, sim_base, ho_ini - embargo)}
    vet = [x for x in oos if x["moneda"] in datos["veteranas"]]
    fresco = reg_holdout.fresco(holdout, f"investigacion:{iv}") if reg_holdout else holdout
    ver = vd.veredicto(oos, holdout, n_total, lab_cfg, bases, None, vet, MIN_OPS_ETAPA1, holdout_fresco=fresco,
                       escenarios=esc)
    met.update(etapa2=ver, n_holdout=len(holdout), holdout_independiente=ver.get("holdout_independiente"))
    if ver["estado"] not in ("supera", "exploratorio"):
        fallo = next((x["regla"] for x in ver["comprobaciones"] if not x["cumple"]), ver.get("motivo"))
        return "cementerio", dict(met, motivo=f"etapa 2: {fallo}", no_evaluable=ver["estado"] == "no_evaluable")
    # El holdout de la investigación se consulta cada noche: casi nunca es independiente. Por eso una aspirante
    # es EXPLORATORIA por definición; su confirmación independiente es el seguimiento en vivo (30 operaciones).
    return "aspirante", met


# ============================================================ registro
def cargar_registro(ruta=RUTA_REGISTRO):
    try:
        with open(ruta, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"version": 1, "probadas_total": 0, "ideas": {}, "ejecuciones": []}


def guardar_registro(reg, ruta=RUTA_REGISTRO):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)


def aspirantes(reg):
    return {k: v for k, v in reg["ideas"].items() if v["estado"] in ("aspirante", "lista_revision")}


def resultados_shadow(registros_memoria, clave):
    rs = [r["resultado_r"] for r in registros_memoria
          if r.get("tipo") == "aspirante" and r.get("modulo") == clave and r.get("resultado_r") is not None]
    if not rs:
        return {"n": 0, "r_medio": None, "t": None}
    m = sum(rs) / len(rs)
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1)) if len(rs) > 1 else 0
    return {"n": len(rs), "r_medio": round(m, 3), "t": round(m / (sd / math.sqrt(len(rs))), 2) if sd else None}


def juzgar_aspirantes(reg, registros_memoria, ahora=None):
    """Aspirantes con 30 operaciones en vivo (shadow): al cementerio si fallan; 'lista_revision' si aguantan."""
    cambios = []
    for k, v in aspirantes(reg).items():
        res = resultados_shadow(registros_memoria, "inv:" + k)
        v["shadow"] = res
        if res["n"] < OPS_JUICIO_SHADOW or v["estado"] == "lista_revision":
            continue
        # Confirmación INDEPENDIENTE (datos que el examen no vio). Se corrige por las aspirantes que se vigilan a la
        # vez: t de Student de una cola al 5 % / MAX_ASPIRANTES.
        import validacion as vd
        req = vd.t_requerido(MAX_ASPIRANTES, n_ops=res["n"])
        v["t_vivo_requerido"] = round(req, 2)
        if res["r_medio"] <= 0 or res["t"] is None or res["t"] < req:
            v.update(estado="cementerio", motivo=f"falló en vivo (shadow): {res['n']} ops, {res['r_medio']} R de media",
                     retirada_ts=ahora or time.time())
            cambios.append(f"❌ {describir(v['def'])}: retirada tras {res['n']} operaciones en vivo ({res['r_medio']:+.2f} R).")
        else:
            v["estado"] = "lista_revision"
            cambios.append(f"⭐ {describir(v['def'])}: aguanta en vivo ({res['n']} ops, {res['r_medio']:+.2f} R, t {res['t']} ≥ {req:.2f}). "
                           f"Lista para que la revises.")
    return cambios


# ============================================================ ejecución
def ronda(n_ideas=60, max_minutos=90, rapido=False, semilla=None, presupuesto=300, avisar=True):
    import laboratorio as lab
    import configuracion as conf
    from datos import Api
    c, _ = conf.migrar(conf.cargar(os.path.join(AQUI, "config.json")))
    c = conf.validar(c, entorno={})
    lab.configurar_globales(c)
    fees = (c["comisiones"]["taker_pct"] / 100, c["comisiones"]["maker_pct"] / 100)
    inicio = time.time()
    reg = cargar_registro()
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rng = random.Random(semilla if semilla is not None else hoy)
    ideas = generar(rng, set(reg["ideas"]), n_ideas)
    print(f"Investigación {hoy}: {len(ideas)} ideas nuevas · llevamos {reg['probadas_total']} probadas en total.")
    import validacion as vd
    # Variantes probadas desde siempre (compartido con el laboratorio) y holdout ya mirado.
    reg_var = vd.RegistroVariantes(os.path.join(AQUI, "laboratorio_variantes.json"))
    reg_var.añadir(["inv:" + k for k in reg["ideas"]])
    reg_ho = vd.RegistroHoldout(os.path.join(AQUI, "laboratorio_holdout.json"))
    api = Api(usar_cache=True, presupuesto=presupuesto)
    datos = lab.preparar_datos(c, api, int(time.time() // 3600 * 3600) * 1000, rapido)
    nuevas_asp, probadas = [], 0
    for idea in ideas:
        if time.time() - inicio > max_minutos * 60:
            print("Tiempo agotado: el resto queda para la próxima ronda.")
            break
        reg["probadas_total"] += 1                 # cuenta ANTES de examinar: el listón sube con cada idea
        probadas += 1
        n_total = reg_var.añadir(["inv:" + idea["id"]])
        try:
            estado, met = examinar(idea["def"], datos, c, fees, n_total, c["laboratorio"], reg_ho)
        except Exception as e:  # noqa: BLE001
            estado, met = "cementerio", {"motivo": f"error al examinar: {type(e).__name__}"}
        reg["ideas"][idea["id"]] = {"def": idea["def"], "descripcion": describir(idea["def"]), "estado": estado,
                                    "fecha": hoy, "metricas": met, "motivo": met.get("motivo")}
        if estado == "aspirante":
            nuevas_asp.append(idea["id"])
        print(f"  {'⭐' if estado == 'aspirante' else '·'} {describir(idea['def'])} -> {met.get('motivo') or 'ASPIRANTE'}")
    ahora_ts = time.time()
    for iv in ("1h", "4h"):
        reg_ho.marcar(f"investigacion:{iv}", ahora_ts)     # lo mirado esta noche ya no es holdout independiente
    reg_ho.guardar()
    reg_var.guardar()
    # Máximo de aspirantes a la vez: se quedan las de mejor t
    asp = aspirantes(reg)
    if len(asp) > MAX_ASPIRANTES:
        orden = sorted(asp, key=lambda k: (asp[k]["estado"] == "lista_revision", asp[k]["metricas"].get("t") or 0), reverse=True)
        for k in orden[MAX_ASPIRANTES:]:
            reg["ideas"][k].update(estado="cementerio", motivo="sustituida por aspirantes más sólidas")
    # Juicio de las aspirantes con lo que llevan en vivo
    try:
        with open(os.path.join(AQUI, "aprendizaje.json"), encoding="utf-8") as fh:
            memoria = json.load(fh).get("registros", [])
    except (OSError, ValueError):
        memoria = []
    cambios = juzgar_aspirantes(reg, memoria)
    resumen = {"fecha": hoy, "probadas_hoy": probadas, "probadas_total": reg["probadas_total"],
               "variantes_totales": reg_var.total(),
               "t_requerido": round(vd.t_requerido(reg_var.total(), n_ops=60), 2),
               "nuevas_aspirantes": nuevas_asp, "cambios": cambios, "minutos": round((time.time() - inicio) / 60, 1),
               "aspirantes": {k: {"descripcion": v["descripcion"], "estado": v["estado"], "shadow": v.get("shadow"),
                                  "t_lab": v["metricas"].get("t"), "r_lab": v["metricas"].get("r_medio")}
                              for k, v in aspirantes(reg).items()},
               "cementerio": sum(1 for v in reg["ideas"].values() if v["estado"] == "cementerio")}
    reg["ejecuciones"] = (reg.get("ejecuciones") or [])[-200:] + [{k: resumen[k] for k in ("fecha", "probadas_hoy", "probadas_total",
                                                                                          "nuevas_aspirantes", "minutos")}]
    guardar_registro(reg)
    escribir_informe(resumen, reg)
    if avisar and (nuevas_asp or cambios):
        texto = [f"🔬 Investigación: {probadas} ideas examinadas hoy ({reg['probadas_total']} en total; listón t ≥ "
                 f"{resumen['t_requerido']})."]
        texto += [f"⭐ Nueva aspirante (se sigue en vivo sin dinero): {reg['ideas'][k]['descripcion']}" for k in nuevas_asp]
        texto += cambios
        texto.append("Nada se activa solo: las aspirantes solo se observan.")
        try:
            sys.path.insert(0, os.path.join(AQUI, "despliegue"))
            from avisos import enviar
            enviar("\n".join(texto))
        except Exception:  # noqa: BLE001
            pass
    return resumen


def escribir_informe(resumen, reg):
    os.makedirs(DIR_REGISTRO, exist_ok=True)
    with open(os.path.join(DIR_REGISTRO, "investigacion_informe.json"), "w", encoding="utf-8") as fh:
        json.dump(resumen, fh, ensure_ascii=False, indent=1)
    lineas = [f"# Investigación — {resumen['fecha']}", "",
              f"- Ideas examinadas hoy: **{resumen['probadas_hoy']}** · en total: **{resumen['probadas_total']}** "
              f"(listón actual: t ≥ {resumen['t_requerido']})",
              f"- En el cementerio: {resumen['cementerio']}", "", "## Aspirantes (se siguen en vivo, sin dinero)", ""]
    if resumen["aspirantes"]:
        for k, a in resumen["aspirantes"].items():
            sh = a.get("shadow") or {}
            lineas.append(f"- `{k}` {a['descripcion']} · laboratorio t {a['t_lab']}, {a['r_lab']} R · en vivo: "
                          f"{sh.get('n', 0)}/{OPS_JUICIO_SHADOW} ops, {sh.get('r_medio')} R · **{a['estado']}**")
    else:
        lineas.append("- Ninguna. Es lo normal: casi todas las ideas fallan el examen.")
    if resumen["cambios"]:
        lineas += ["", "## Cambios", ""] + [f"- {x}" for x in resumen["cambios"]]
    recientes = sorted(((k, v) for k, v in reg["ideas"].items() if v["estado"] == "cementerio"),
                       key=lambda kv: kv[1].get("fecha", ""), reverse=True)[:15]
    lineas += ["", "## Últimas descartadas", ""] + [f"- {v['descripcion']} → {v.get('motivo')}" for _, v in recientes]
    with open(os.path.join(DIR_REGISTRO, "investigacion_informe.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lineas) + "\n")


def main(argv=None):
    from compat import consola_utf8
    consola_utf8()
    ap = argparse.ArgumentParser(description="Departamento de investigación del bot (solo evidencia; no activa nada)")
    ap.add_argument("--ideas", type=int, default=60)
    ap.add_argument("--minutos", type=int, default=90)
    ap.add_argument("--rapido", action="store_true")
    ap.add_argument("--presupuesto", type=int, default=300, help="peso de peticiones/minuto a Hyperliquid")
    ap.add_argument("--sin-aviso", action="store_true")
    a = ap.parse_args(argv)
    aqui = os.path.dirname(os.path.abspath(__file__))
    bloqueo = os.path.join(aqui, "investigacion.lock")
    for ruta, que in ((bloqueo, "una ronda de investigación"), (os.path.join(aqui, "laboratorio_semanal.lock"), "el laboratorio semanal")):
        try:
            if time.time() - os.path.getmtime(ruta) < 6 * 3600:
                print(f"Ya hay {que} en marcha: no empiezo otra (para no cargar la conexión del bot).")
                return 0
        except OSError:
            pass
    with open(bloqueo, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    try:
        r = ronda(a.ideas, a.minutos, a.rapido, presupuesto=a.presupuesto, avisar=not a.sin_aviso)
    finally:
        try:
            os.remove(bloqueo)
        except OSError:
            pass
    print(f"\nHecho en {r['minutos']} min: {r['probadas_hoy']} ideas, {len(r['nuevas_aspirantes'])} aspirantes nuevas. "
          f"Informe: registro/investigacion_informe.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
