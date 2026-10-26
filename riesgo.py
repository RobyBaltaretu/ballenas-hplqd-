"""
Riesgo, tamaño y límites. Lo usan IGUAL el bot (real/simulado/shadow) y el laboratorio (cartera), para que
no haya motores distintos que diverjan en silencio.

Conceptos (todos en USD salvo que se diga):
  riesgo de la operación  = lo que se perdería si salta el stop: tamaño × distancia al stop + costes estimados
                            (comisión de entrada y salida a precio taker, media horquilla, deslizamiento y un
                            colchón de estrés). El tamaño se calcula para que esto no supere el % permitido.
  notional                = tamaño × precio (lo que "mueve" la posición).
  margen inicial          = notional / apalancamiento (lo que se bloquea de la cuenta).
  margen de mantenimiento = el mínimo antes de liquidar; en Hyperliquid es la mitad del margen inicial al
                            apalancamiento MÁXIMO del mercado (mmr = 1 / (2 × apal_max_mercado)).
  exposición bruta / neta = suma de notionals / suma con signo (largos − cortos), frente al valor de cuenta.

El apalancamiento NO sustituye al cálculo de riesgo: solo decide cuánto margen se bloquea. Se usa el MENOR
apalancamiento que permite la operación dentro del margen máximo, porque deja la liquidación más lejos.

Un stop NO garantiza que no haya liquidación: se dispara con el precio de marca (mark) y se ejecuta a
mercado, con posible deslizamiento. Por eso se exige que la liquidación quede bastante más lejos que el stop.
"""

import math
import time
from datetime import datetime, timezone

NOTIONAL_MINIMO_HL = 10.0   # Hyperliquid rechaza órdenes de menos de 10 USD de valor


def redondear_tamano_abajo(sz, sz_decimales):
    f = 10 ** sz_decimales
    return math.floor(sz * f + 1e-9) / f


def redondear_tamano_arriba(sz, sz_decimales):
    f = 10 ** sz_decimales
    return math.ceil(sz * f - 1e-9) / f


def costes_fraccion(cfg, spread_pct=0.0):
    """Costes previstos de ida y vuelta, en fracción del notional. Se asume lo PEOR razonable: entrada que
    acaba a mercado (taker) y salida por stop a mercado (taker) con deslizamiento."""
    com = cfg["comisiones"]
    r = cfg["riesgo"]
    taker = com["taker_pct"] / 100
    partes = {
        "comision_entrada": taker,
        "comision_salida": taker,
        "media_horquilla": max(0.0, spread_pct) / 100 / 2,
        "deslizamiento": 2 * r["deslizamiento_estimado_pct"] / 100,
        "estres": r["margen_estres_pct"] / 100,
    }
    return sum(partes.values()), partes


def funding_adverso_frac(funding_contra_pct_h, duracion_min, minimo_pct_h=0.00125, estres=2.0):
    """Funding que se puede pagar mientras dura la operación, en fracción del notional. Se toma el mayor entre el
    funding actual en contra y el básico de Hyperliquid (0,01 %/8 h = 0,00125 %/h), multiplicado por un estrés,
    porque el funding cambia cada hora."""
    tasa = max(funding_contra_pct_h or 0.0, minimo_pct_h) * estres
    return tasa / 100 * max(0.0, duracion_min or 0) / 60


def coste_esperado_pct(cfg, spread_pct=0.0):
    """Coste ESPERADO de ida y vuelta en % del precio (no el peor caso): entrada maker si se entra con orden
    límite (taker si no), salida taker, media horquilla y un deslizamiento."""
    com, r = cfg["comisiones"], cfg["riesgo"]
    entrada = com["maker_pct"] if cfg["ordenes"].get("entrada_limite", True) else com["taker_pct"]
    return entrada + com["taker_pct"] + max(0.0, spread_pct) / 2 + r["deslizamiento_estimado_pct"]


def costes_excesivos(cfg, sl_pct, spread_pct=0.0):
    """Motivo de descarte si los costes esperados se comen demasiado del riesgo (stop muy corto), o None.
    riesgo.coste_max_fraccion_riesgo = 0 lo desactiva."""
    maximo = cfg["riesgo"].get("coste_max_fraccion_riesgo", 0)
    if not maximo or not sl_pct or sl_pct <= 0:
        return None
    frac = coste_esperado_pct(cfg, spread_pct) / sl_pct
    if frac > maximo:
        return (f"costes esperados {frac * 100:.0f}% del riesgo (máximo {maximo * 100:.0f}%): stop de {sl_pct:.2f}% "
                f"demasiado corto para lo que cuesta entrar y salir")
    return None


def distancia_liquidacion_pct(apal, apal_max_mercado):
    """Distancia aproximada (en %) desde la entrada hasta la liquidación con margen aislado.
    = (1/apal − mmr) × 100, con mmr = 1/(2·apal_max_mercado). Es una aproximación: no incluye comisiones
    ni funding acumulados; si el exchange da liquidationPx, se usa ese valor."""
    mmr = 1 / (2 * max(apal_max_mercado, 1))
    return max(0.0, (1 / max(apal, 1e-9) - mmr) * 100)


def dimensionar(equity, libre, precio, sl_pct, sz_decimales, apal_max_mercado, cfg, spread_pct=0.0, factor=1.0,
                minimo=NOTIONAL_MINIMO_HL, limites=None, funding_frac=0.0):
    """Tamaño de una operación. Devuelve dict con ok=True/False y 'motivo' si se descarta.
    'limites' (opcional) sustituye a cfg["riesgo"] (p. ej. los efectivos del piloto real).
    'factor' (0-1) solo REDUCE el riesgo (gestión de capital, aprendizaje); nunca lo aumenta.
    'funding_frac': funding adverso previsto durante la duración de la operación (fracción del notional)."""
    r = limites or cfg["riesgo"]
    if not (equity and equity > 0 and precio and precio > 0 and sl_pct and sl_pct > 0):
        return {"ok": False, "motivo": "datos insuficientes para calcular el tamaño (saldo, precio o stop desconocidos)"}
    if not (r["sl_min_pct"] <= sl_pct <= r["sl_max_pct"]):
        return {"ok": False, "motivo": f"stop {sl_pct:.2f}% fuera de los límites ({r['sl_min_pct']}–{r['sl_max_pct']}%)"}
    factor = min(1.0, max(0.0, factor))
    riesgo_permitido = equity * r["riesgo_por_operacion_pct"] / 100 * factor
    coste, partes = costes_fraccion(cfg, spread_pct)
    coste += max(0.0, funding_frac)
    perdida_por_usd = sl_pct / 100 + coste
    notional = riesgo_permitido / perdida_por_usd
    apal_tope = max(1.0, min(r["apalancamiento_max"], apal_max_mercado))
    margen_max = equity * r["margen_max_por_operacion_pct"] / 100
    notional = min(notional, margen_max * apal_tope, max(libre, 0.0) * 0.95 * apal_tope)
    tam = redondear_tamano_abajo(notional / precio, sz_decimales)
    notional_r = tam * precio
    if notional_r < minimo:
        tam_min = redondear_tamano_arriba(minimo * 1.002 / precio, sz_decimales)
        riesgo_min = tam_min * precio * perdida_por_usd
        if riesgo_min > riesgo_permitido + 1e-12:
            return {"ok": False, "motivo": (f"tamaño mínimo excede riesgo: la orden mínima ({tam_min * precio:.2f} USD) "
                                            f"arriesgaría {riesgo_min:.2f} USD y el máximo permitido es "
                                            f"{riesgo_permitido:.2f} USD"),
                    "riesgo_minimo_usd": riesgo_min, "riesgo_permitido_usd": riesgo_permitido}
        return {"ok": False, "motivo": "margen o saldo libre insuficientes para la orden mínima"}
    # Menor apalancamiento que cabe en el margen máximo -> liquidación lo más lejos posible
    apal = max(1, min(int(apal_tope), math.ceil(notional_r / margen_max - 1e-9) if margen_max > 0 else int(apal_tope)))
    apal = max(apal, math.ceil(notional_r / max(libre * 0.95, 1e-9) - 1e-9)) if libre > 0 else apal
    if apal > apal_tope:
        return {"ok": False, "motivo": "margen insuficiente: haría falta más apalancamiento del permitido"}
    dist_liq = distancia_liquidacion_pct(apal, apal_max_mercado)
    if dist_liq < sl_pct * r["distancia_liquidacion_min_multiplo_stop"]:
        return {"ok": False, "motivo": (f"la liquidación (~{dist_liq:.1f}%) quedaría demasiado cerca del stop "
                                        f"({sl_pct:.1f}%)")}
    return {
        "ok": True, "tam": tam, "notional": notional_r, "apalancamiento": apal, "margen": notional_r / apal,
        "riesgo_usd": notional_r * perdida_por_usd, "riesgo_stop_usd": notional_r * sl_pct / 100,
        "costes_estimados_usd": notional_r * coste, "costes_detalle": {k: notional_r * v for k, v in partes.items()},
        "riesgo_permitido_usd": riesgo_permitido, "distancia_liquidacion_pct": dist_liq, "factor": factor,
    }


def riesgo_tras_fill(tam, precio_medio, stop, cfg, spread_pct=0.0):
    """Riesgo REAL con lo ejecutado (tras fills parciales, deslizamiento o paso de límite a mercado)."""
    if not tam or not precio_medio or stop is None:
        return None
    coste, _ = costes_fraccion(cfg, spread_pct)
    return tam * abs(precio_medio - stop) + tam * precio_medio * coste


# ============================================================ impacto y beta
def impacto_pct(niveles, notional_usd):
    """Impacto estimado (en % frente al mejor precio) de ejecutar 'notional_usd' recorriendo los niveles del libro
    del lado contrario [(px, sz), ...]. None si el libro no tiene profundidad suficiente."""
    if not niveles or notional_usd <= 0:
        return None
    mejor = niveles[0][0]
    resto, coste, cant = notional_usd, 0.0, 0.0
    for px, sz in niveles:
        toma = min(resto, px * sz)
        coste += toma
        cant += toma / px
        resto -= toma
        if resto <= 1e-9:
            break
    if resto > 1e-9 or not cant:
        return None
    medio = coste / cant
    return abs(medio - mejor) / mejor * 100


def beta(cierres_moneda, cierres_btc, n=24 * 30):
    """Beta de los rendimientos (log) de una moneda frente a BTC, alineados por tiempo. None si hay pocos datos.
    cierres_*: {t: precio}."""
    ts = sorted(set(cierres_moneda) & set(cierres_btc))[-(n + 1):]
    if len(ts) < 50:
        return None
    ra = [math.log(cierres_moneda[b] / cierres_moneda[a]) for a, b in zip(ts, ts[1:])]
    rb = [math.log(cierres_btc[b] / cierres_btc[a]) for a, b in zip(ts, ts[1:])]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    var_b = sum((y - mb) ** 2 for y in rb)
    if var_b <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / var_b


def exposicion_beta(posiciones, betas, beta_desconocida):
    """Exposición NETA a BTC ponderada por beta: suma de lado × notional × beta (bot, externas y la nueva).
    Las monedas sin beta conocida usan 'beta_desconocida' (prudente) y se listan aparte."""
    total, sin_beta = 0.0, []
    for p in posiciones:
        b = 1.0 if p["moneda"] == "BTC" else betas.get(p["moneda"])
        if b is None:
            b = beta_desconocida
            sin_beta.append(p["moneda"])
        total += (1 if p["lado"] == "L" else -1) * p["notional"] * b
    return total, sorted(set(sin_beta))


# ============================================================ límites agregados
def comprobar_agregados(nueva, abiertas, externas, equity, cfg, limites=None, betas=None):
    """nueva = {"moneda", "lado", "notional", "riesgo_usd"}; abiertas = las del bot; externas = posiciones de la
    cuenta que no son del bot (cuentan como exposición aunque el bot no pueda tocarlas).
    betas: {moneda: beta frente a BTC} para el límite de exposición ponderada (si se da).
    Devuelve la lista de motivos de bloqueo (vacía si se puede)."""
    r = limites or cfg["riesgo"]
    motivos = []
    if len(abiertas) >= r["max_posiciones_abiertas"]:
        motivos.append(f"máximo de posiciones abiertas ({r['max_posiciones_abiertas']})")
    if sum(1 for a in abiertas if a["lado"] == nueva["lado"]) >= r["max_mismo_lado"]:
        motivos.append(f"máximo de posiciones en la misma dirección ({r['max_mismo_lado']})")
    if not equity or equity <= 0:
        return motivos + ["valor de la cuenta desconocido"]
    riesgo_total = sum(a.get("riesgo_usd") or 0 for a in abiertas) + nueva["riesgo_usd"]
    if riesgo_total > equity * r["riesgo_abierto_max_pct"] / 100 + 1e-12:
        motivos.append(f"riesgo abierto total {riesgo_total / equity * 100:.1f}% > {r['riesgo_abierto_max_pct']}%")
    todas = abiertas + externas + [nueva]
    en_mercado = sum(a["notional"] for a in todas if a["moneda"] == nueva["moneda"])
    if en_mercado > equity * r["exposicion_max_mercado_pct"] / 100:
        motivos.append(f"exposición en {nueva['moneda']} {en_mercado / equity * 100:.0f}% > {r['exposicion_max_mercado_pct']}%")
    bruta = sum(a["notional"] for a in todas)
    neta = abs(sum(a["notional"] * (1 if a["lado"] == "L" else -1) for a in todas))
    if bruta > equity * r["exposicion_bruta_max_pct"] / 100:
        motivos.append(f"exposición bruta {bruta / equity * 100:.0f}% > {r['exposicion_bruta_max_pct']}%")
    if neta > equity * r["exposicion_neta_max_pct"] / 100:
        motivos.append(f"exposición neta {neta / equity * 100:.0f}% > {r['exposicion_neta_max_pct']}%")
    if betas is not None and r.get("exposicion_beta_btc_max_pct"):
        # La correlación simple no basta: casi todo se mueve con BTC. Se mide la exposición NETA ponderada por beta
        # incluyendo externas y lados opuestos (un corto con beta alta compensa un largo).
        eb, sin_beta = exposicion_beta(todas, betas, r.get("beta_desconocida", 1.5))
        if abs(eb) > equity * r["exposicion_beta_btc_max_pct"] / 100:
            motivos.append(f"exposición a BTC (ponderada por beta) {eb / equity * 100:+.0f}% > ±{r['exposicion_beta_btc_max_pct']}%"
                           + (f" (beta supuesta {r.get('beta_desconocida', 1.5)} para {', '.join(sin_beta)})" if sin_beta else ""))
    return motivos


def exposicion(abiertas, externas, equity):
    todas = abiertas + externas
    bruta = sum(a["notional"] for a in todas)
    neta = sum(a["notional"] * (1 if a["lado"] == "L" else -1) for a in todas)
    return {"bruta_usd": bruta, "neta_usd": neta,
            "bruta_pct": bruta / equity * 100 if equity else None, "neta_pct": neta / equity * 100 if equity else None,
            "riesgo_abierto_usd": sum(a.get("riesgo_usd") or 0 for a in abiertas),
            "riesgo_abierto_pct": sum(a.get("riesgo_usd") or 0 for a in abiertas) / equity * 100 if equity else None}


# ============================================================ límites duros (bloquean entradas)
def ventana_utc(ts=None):
    """(día 'AAAA-MM-DD', semana ISO 'AAAA-Www') en UTC. Todo el control diario/semanal usa UTC."""
    d = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    iso = d.isocalendar()
    return d.strftime("%Y-%m-%d"), f"{iso[0]}-W{iso[1]:02d}"


def actualizar_ventanas(estado, equity, ts=None):
    """Mantiene saldo de inicio de día/semana (UTC) y el máximo. Devuelve True si empezó un día nuevo."""
    dia, semana = ventana_utc(ts)
    estado.setdefault("saldo_inicial", equity)
    estado["saldo_maximo"] = max(estado.get("saldo_maximo", equity), equity)
    nuevo_dia = estado.get("dia") != dia
    if estado.get("semana") != semana:
        estado.update(semana=semana, saldo_inicio_semana=equity)
    if nuevo_dia:
        estado.update(dia=dia, saldo_inicio_dia=equity, ops_hoy=0)
    return nuevo_dia


def rebasar_capital(estado, nuevo_inicial, ts_cambio=None):
    """Capital añadido o retirado (p. ej. saldo simulado de 58 -> 232 USD): desplaza las referencias de día, semana,
    máximo, inicio e historial. Sin esto el bot leería el depósito como +300 % de beneficio (y reduciría tamaños para
    «proteger lo ganado»). Con ts_cambio (hora del evento de capital registrado) solo se desplaza lo medido ANTES
    de esa hora; sin él (registros antiguos) se usa la cercanía al capital viejo. Devuelve la diferencia aplicada."""
    viejo = estado.get("saldo_inicial")
    if viejo is None or nuevo_inicial is None or abs(float(nuevo_inicial) - float(viejo)) < 1e-9:
        return 0.0
    viejo, nuevo = float(viejo), float(nuevo_inicial)
    delta = nuevo - viejo

    def antes(ts):
        return ts is not None and ts < ts_cambio

    def ajustar(v, ts=None):
        if ts_cambio is not None:
            return v + delta if antes(ts) else v
        return v + delta if abs(v - viejo) < abs(v - nuevo) else v
    estado["saldo_inicial"] = nuevo
    estado["historial_saldo"] = [[t, ajustar(v, t)] for t, v in estado.get("historial_saldo", [])]
    if ts_cambio is not None:
        def inicio(etq, fmt):
            try:
                return datetime.strptime(etq + ("-1" if "W" in etq else ""), fmt).replace(tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError):
                return None
        for k, etq, fmt in (("saldo_inicio_dia", estado.get("dia"), "%Y-%m-%d"),
                            ("saldo_inicio_semana", estado.get("semana"), "%G-W%V-%u")):
            if estado.get(k) is not None and antes(inicio(etq, fmt)):
                estado[k] += delta
        vals = [v for _, v in estado["historial_saldo"]]
        if estado.get("saldo_maximo") is not None:
            estado["saldo_maximo"] = max(vals) if vals else estado["saldo_maximo"] + delta
    else:
        for k in ("saldo_inicio_dia", "saldo_inicio_semana", "saldo_maximo"):
            if estado.get(k) is not None:
                estado[k] = ajustar(estado[k])
    estado.setdefault("ajustes_capital", []).append({"ts": time.time(), "delta": round(delta, 2),
                                                     "evento_ts": ts_cambio})
    return delta


def bloqueos_duros(estado, equity, cfg, limites=None, ahora=None):
    """Límites que IMPIDEN nuevas entradas (no reducen tamaño: paran). Devuelve lista de motivos."""
    r = limites or cfg["riesgo"]
    motivos = []
    if not equity or equity <= 0:
        return ["valor de la cuenta desconocido"]
    ini_d, ini_s = estado.get("saldo_inicio_dia"), estado.get("saldo_inicio_semana")
    if ini_d and (equity / ini_d - 1) * 100 <= -r["perdida_diaria_max_pct"]:
        motivos.append(f"pérdida del día {(equity / ini_d - 1) * 100:.1f}% (límite −{r['perdida_diaria_max_pct']}%)")
    if ini_s and (equity / ini_s - 1) * 100 <= -r["perdida_semanal_max_pct"]:
        motivos.append(f"pérdida de la semana {(equity / ini_s - 1) * 100:.1f}% (límite −{r['perdida_semanal_max_pct']}%)")
    mx = estado.get("saldo_maximo")
    if mx and (1 - equity / mx) * 100 >= r["drawdown_max_pct"]:
        motivos.append(f"caída desde el máximo {(1 - equity / mx) * 100:.1f}% (límite {r['drawdown_max_pct']}%)")
    if estado.get("ops_hoy", 0) >= r["max_operaciones_dia"]:
        motivos.append(f"máximo de operaciones del día ({r['max_operaciones_dia']})")
    if (ahora or 0) < estado.get("pausa_hasta", 0):
        motivos.append("pausa por racha de pérdidas")
    return motivos


def suelo_emergencia(estado, equity, cfg):
    ini = estado.get("saldo_inicial")
    if not ini or not equity:
        return False
    if equity <= ini * (1 - cfg["riesgo"]["perdida_max_total_pct"] / 100):
        return True
    # Piloto real: perdida_max_usd es un TOPE, no solo un bloqueo de entradas. Con el valor de la cuenta (incluye
    # lo no realizado) en esa pérdida se cierra todo. El stop no garantiza el precio: un salto puede pasarse.
    p = cfg.get("piloto_real") or {}
    return cfg.get("modo") == "real" and p.get("perdida_max_usd") is not None and ini - equity >= p["perdida_max_usd"]


def limites_efectivos(cfg, equity=None):
    """En real se aplican además los límites del piloto (gana el más prudente). Devuelve dict tipo cfg["riesgo"]."""
    r = dict(cfg["riesgo"])
    if cfg["modo"] == "real":
        p = cfg["piloto_real"]
        for k in ("riesgo_por_operacion_pct", "riesgo_abierto_max_pct", "max_posiciones_abiertas"):
            r[k] = min(r[k], p[k])
    return r
