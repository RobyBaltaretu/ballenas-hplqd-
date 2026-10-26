"""
Estudios comparativos del laboratorio (solo informe: no cambian nada ni activan estrategias).

aportacion(): para cada estrategia (sus operaciones FUERA DE MUESTRA), y para la combinación:
  - resultado neto en R, número de operaciones, caída máxima, resultado por trimestre y por régimen de BTC;
  - exposición: % de largos y correlación de su R diario con el rendimiento diario de BTC;
  - solapamiento de señales con cada otra estrategia: fracción de sus operaciones que coinciden en moneda, lado
    y tiempo con operaciones de la otra (si es alto, aportan casi lo mismo);
  - correlación de los R diarios entre estrategias;
  - APORTACIÓN MARGINAL: cuánto empeora (o mejora) la combinación de todas si se quita esa estrategia
    (R total, R por operación y caída máxima). Una estrategia que solo repite a otra aporta poco.
Es descriptivo: una correlación simple no es una medida completa del riesgo, y nada de esto autoriza nada.
"""

import math
from datetime import datetime, timezone


def _dia(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _caida(trades):
    acum = pico = caida = 0.0
    for t in sorted(trades, key=lambda x: x.get("cierre_ts", x["t"])):
        acum += t["r"]
        pico = max(pico, acum)
        caida = max(caida, pico - acum)
    return caida


def _corr(a, b):
    n = len(a)
    if n < 10:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if not va or not vb:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb)


def _diario(trades):
    d = {}
    for t in trades:
        k = _dia(t.get("cierre_ts", t["t"]))
        d[k] = d.get(k, 0.0) + t["r"]
    return d


def resumen(trades, btc_diario=None, regimen=None):
    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    out = {"n": len(rs), "r_total": round(sum(rs), 2), "r_medio": round(sum(rs) / len(rs), 3),
           "caida_max_r": round(_caida(trades), 2),
           "largos_pct": round(sum(1 for t in trades if t.get("lado") == "L") / len(rs) * 100, 1)}
    if btc_diario:
        d = _diario(trades)
        dias = sorted(set(d) & set(btc_diario))
        c = _corr([d[x] for x in dias], [btc_diario[x] for x in dias])
        out["correlacion_con_btc"] = None if c is None else round(c, 2)
    if regimen:
        por = {}
        for t in trades:
            por.setdefault(regimen(t["t"]) or "desconocido", []).append(t["r"])
        out["por_regimen"] = {k: {"n": len(v), "r_medio": round(sum(v) / len(v), 3)} for k, v in sorted(por.items())}
    por_t = {}
    for t in trades:
        d = datetime.fromtimestamp(t["t"], timezone.utc)
        por_t.setdefault(f"{d.year}-T{(d.month - 1) // 3 + 1}", []).append(t["r"])
    out["por_trimestre"] = {k: round(sum(v), 2) for k, v in sorted(por_t.items())}
    return out


def solapamiento(a, b, margen_seg=0):
    """Fracción de operaciones de 'a' que coinciden con alguna de 'b' en moneda, lado y tiempo (abiertas a la vez)."""
    if not a:
        return None
    idx = {}
    for t in b:
        idx.setdefault((t["moneda"], t["lado"]), []).append((t["t"] - margen_seg, t.get("cierre_ts", t["t"]) + margen_seg))
    n = 0
    for t in a:
        ini, fin = t["t"], t.get("cierre_ts", t["t"])
        if any(i <= fin and ini <= f for i, f in idx.get((t["moneda"], t["lado"]), [])):
            n += 1
    return round(n / len(a), 3)


def aportacion(por_estrategia, btc_diario=None, regimen=None, min_ops=30):
    """por_estrategia: {clave: [operaciones fuera de muestra]}. Devuelve el informe comparativo."""
    claves = [k for k, v in por_estrategia.items() if len(v) >= min_ops]
    todas = [t for k in claves for t in por_estrategia[k]]
    informe = {"estrategias": {}, "combinada": resumen(todas, btc_diario, regimen), "consideradas": claves,
               "nota": "fuera de muestra; descriptivo (no autoriza nada). La correlación simple no es una medida "
                       "completa del riesgo."}
    diarios = {k: _diario(por_estrategia[k]) for k in claves}
    for k in claves:
        r = resumen(por_estrategia[k], btc_diario, regimen)
        r["solapamiento"] = {o: solapamiento(por_estrategia[k], por_estrategia[o]) for o in claves if o != k}
        corr = {}
        for o in claves:
            if o == k:
                continue
            dias = sorted(set(diarios[k]) | set(diarios[o]))
            c = _corr([diarios[k].get(x, 0.0) for x in dias], [diarios[o].get(x, 0.0) for x in dias])
            corr[o] = None if c is None else round(c, 2)
        r["correlacion_diaria"] = corr
        resto = [t for o in claves if o != k for t in por_estrategia[o]]
        if resto:
            sin = resumen(resto)
            con = informe["combinada"]
            r["marginal"] = {"r_total_aportado": round(con["r_total"] - sin["r_total"], 2),
                             "r_medio_combinada_con_sin": [con["r_medio"], sin["r_medio"]],
                             "caida_max_con_sin": [con["caida_max_r"], sin["caida_max_r"]]}
        informe["estrategias"][k] = r
    return informe
