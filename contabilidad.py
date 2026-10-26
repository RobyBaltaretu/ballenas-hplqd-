"""
Resultado de cada operación a partir de datos del exchange (no del saldo, que mezcla depósitos y transferencias).

  PnL bruto realizado = suma de closedPnl de los fills de SALIDA de la operación (los da el exchange).
  Comisiones          = suma de 'fee' de TODOS sus fills (entrada y salida), las reales que cobró el exchange.
  Funding             = pagos/cobros de funding de la cuenta en ESA moneda mientras la posición del bot estuvo
                        abierta, SOLO si se pueden atribuir con certeza (ver atribuir_funding).
  PnL neto            = bruto − comisiones + funding   (funding positivo = cobrado)
  R bruto / R neto    = PnL / riesgo inicial (|entrada − stop inicial| × tamaño ejecutado)
  Deslizamiento       = (precio medio de entrada − precio de referencia de la señal) × dirección, en %

Los depósitos, retiradas y transferencias NO entran nunca: no se usa la variación del saldo como resultado.
En el modo simulado los fills, comisiones y funding son los de la cuenta ficticia (marcados como simulados).
"""


def _f(x, defecto=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return defecto


def resumen_fills(fills):
    """[{px, sz, fee, closedPnl, side}] -> (tam, precio_medio, comisiones, pnl_cerrado)."""
    tam = sum(_f(f.get("sz")) for f in fills)
    pm = (sum(_f(f.get("px")) * _f(f.get("sz")) for f in fills) / tam) if tam else None
    return tam, pm, sum(_f(f.get("fee")) for f in fills), sum(_f(f.get("closedPnl")) for f in fills)


def atribuir_funding(registros, intervalos):
    """
    registros: userFunding del exchange: [{"time": ms, "delta": {"coin", "usdc", "szi", "fundingRate"}}]
    intervalos: [{"trade_id", "moneda", "lado", "desde_ms", "hasta_ms" (None = abierta), "tam"}]
       -> tramos en los que el bot SABE que tenía esa posición.
    Un pago se atribuye a una operación solo si:
      - es de su moneda y cae dentro de su intervalo,
      - no hay otra operación del bot en esa moneda en ese momento,
      - el signo de la posición del pago coincide con el lado del bot,
      - y el tamaño de la posición del pago coincide con el del bot (±1 %): si no, había algo más en la cuenta.
    Lo que no cumpla queda SIN ASIGNAR (se muestra aparte como diferencia de conciliación).
    Devuelve ({trade_id: [pagos]}, [sin_asignar])."""
    por_trade, sin_asignar = {}, []
    for reg in registros or []:
        d = reg.get("delta") or {}
        if d.get("type", "funding") != "funding":
            continue
        moneda, t = d.get("coin"), reg.get("time", 0)
        candidatos = [iv for iv in intervalos if iv["moneda"] == moneda and iv["desde_ms"] <= t
                      and (iv["hasta_ms"] is None or t <= iv["hasta_ms"])]
        pago = {"time": t, "usd": _f(d.get("usdc")), "tasa": _f(d.get("fundingRate")), "szi": _f(d.get("szi")),
                "moneda": moneda, "simulado": bool(reg.get("simulado"))}
        if len(candidatos) != 1:
            pago["motivo"] = "sin operación del bot en ese momento" if not candidatos else "varias operaciones a la vez"
            sin_asignar.append(pago)
            continue
        iv = candidatos[0]
        lado_ok = (pago["szi"] > 0) == (iv["lado"] == "L")
        tam_ok = iv.get("tam") and abs(abs(pago["szi"]) - iv["tam"]) <= 0.01 * iv["tam"]
        if not lado_ok or not tam_ok:
            pago["motivo"] = "la posición de la cuenta no coincide con la del bot (posible posición externa)"
            sin_asignar.append(pago)
            continue
        por_trade.setdefault(iv["trade_id"], []).append(pago)
    return por_trade, sin_asignar


def resultado(fills_entrada, fills_salida, funding_atribuido, lado, stop_inicial, precio_referencia=None):
    te, pe, fee_e, _ = resumen_fills(fills_entrada)
    ts, ps, fee_s, cerrado = resumen_fills(fills_salida)
    s = 1 if lado == "L" else -1
    bruto = cerrado if fills_salida and any(f.get("closedPnl") is not None for f in fills_salida) else (
        (ps - pe) * ts * s if pe is not None and ps is not None else None)
    funding = sum(p["usd"] for p in funding_atribuido or [])
    comisiones = fee_e + fee_s
    riesgo = abs(pe - stop_inicial) * te if pe is not None and stop_inicial is not None and te else None
    neto = None if bruto is None else bruto - comisiones + funding
    return {
        "tam_entrada": te, "precio_medio_entrada": pe, "tam_salida": ts, "precio_medio_salida": ps,
        "comisiones": comisiones, "comision_entrada": fee_e, "comision_salida": fee_s, "funding": funding,
        "pnl_bruto": bruto, "pnl_neto": neto, "riesgo_inicial_usd": riesgo,
        "r_bruto": bruto / riesgo if riesgo and bruto is not None else None,
        "r_neto": neto / riesgo if riesgo and neto is not None else None,
        "deslizamiento_entrada_pct": (s * (pe - precio_referencia) / precio_referencia * 100
                                      if pe is not None and precio_referencia else None),
    }
