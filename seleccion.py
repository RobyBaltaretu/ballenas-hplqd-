"""
Selección de ballenas por COMPORTAMIENTO y de las monedas a operar.

Por qué: el ranking de Hyperliquid está dominado por creadores de mercado y algoritmos de alta
frecuencia (miles de operaciones al día). Sus posiciones son inventario, no apuestas con dirección,
y copiarlas no tiene sentido. Además muchas cuentas operan acciones tokenizadas (xyz:TSLA...),
spot (@107) o altcoins sin liquidez.

Se buscan traders DIRECCIONALES:
  1. Ranking: cuenta grande, beneficio histórico y del mes positivos, sin ROI disparatado.
  2. Rotación (volumen del mes / capital) baja: descarta creadores de mercado.
  3. Sus últimas operaciones: pocas al día y, sobre todo, en las monedas líquidas que copiamos.
"""

import json
import os
import time

URL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


FILTROS_DEFECTO = {
    "numero_ballenas": 15, "valor_cuenta_min_usd": 500000, "pnl_historico_min_usd": 500000, "roi_mensual_max": 1.0,
    "rotacion_mensual_max": 60, "candidatas_a_analizar": 60, "ordenes_dia_max": 300,
    "fraccion_min_en_monedas": 0.3, "dias_max_sin_operar": 7,
}


def _ventana(fila, nombre):
    for item in fila.get("windowPerformances", []):
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == nombre:
            d = item[1] or {}
            return float(d.get("pnl") or 0), float(d.get("roi") or 0), float(d.get("vlm") or 0)
    return 0.0, 0.0, 0.0


def monedas_liquidas(post, c, log=None):
    """Monedas que vigila el bot: perpetuos del mercado principal con más volumen (config "universo").
    Se usa para elegir ballenas que operen en las mismas monedas que el bot."""
    u = c["universo"]
    try:
        meta, ctxs = post({"type": "metaAndAssetCtxs"}, 20)
        filas = []
        for activo, ctx in zip(meta["universe"], ctxs):
            nombre = activo["name"]
            if activo.get("isDelisted") or nombre.startswith("@") or ":" in nombre or nombre in u["excluir"]:
                continue
            vol = float(ctx.get("dayNtlVlm") or 0)
            if vol >= u["volumen_min_24h_usd"]:
                filas.append((vol, nombre))
        filas.sort(reverse=True)
        monedas = [m for _, m in filas[: u["max_monedas"]]]
        for m in u.get("incluir_siempre", []):
            if m not in monedas:
                monedas.append(m)
        return monedas
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("No pude calcular las monedas líquidas (%s); uso las de siempre.", e)
        return list(u.get("incluir_siempre", ["BTC", "ETH"]))


def perfil_por_fills(fills, monedas):
    """Estadísticas de comportamiento a partir de las últimas operaciones de una cuenta."""
    if not fills:
        return None
    ts = sorted(f["time"] for f in fills)
    dias = max((ts[-1] - ts[0]) / 86_400_000, 1 / 24)
    total = sum(float(f["sz"]) * float(f["px"]) for f in fills) or 1
    en_monedas = sum(float(f["sz"]) * float(f["px"]) for f in fills if f["coin"] in monedas)
    # Una orden grande se llena en muchos trozos: se cuentan órdenes distintas, no trozos.
    ordenes = len({f.get("oid", i) for i, f in enumerate(fills)})
    return {
        "fills": len(fills), "dias": dias, "ordenes_dia": ordenes / dias,
        "frac_monedas": en_monedas / total, "ultima_ts": ts[-1] / 1000,
    }


def elegir_ballenas(c, post, get_leaderboard, monedas, log=None, ruta_cache=None, ahora=None, numero=None):
    """
    Devuelve una lista de direcciones. 'post(cuerpo, peso)' consulta /info; 'get_leaderboard()' el ranking.
    Guarda el resultado en ruta_cache para no repetir el análisis en cada arranque.
    numero: cuántas buscar (por defecto filtros_ballenas.numero_ballenas). El laboratorio pide más para
    después quedarse con las que de verdad anticipan el precio.
    """
    ahora = ahora or time.time()
    manuales = [a.lower() for a in c.get("ballenas_manuales", [])]
    if not c.get("seleccion_automatica", True):
        return manuales
    f = dict(FILTROS_DEFECTO, **c.get("filtros_ballenas", {}))
    if numero:
        f["numero_ballenas"] = numero
        f["candidatas_a_analizar"] = max(f["candidatas_a_analizar"], numero * 2)
        ruta_cache = None
    if ruta_cache and os.path.exists(ruta_cache):
        try:
            with open(ruta_cache, encoding="utf-8") as fh:
                cache = json.load(fh)
            if ahora - cache["ts"] < c["refrescar_ballenas_horas"] * 3600 and cache.get("ballenas"):
                return list(dict.fromkeys(manuales + cache["ballenas"]))
        except (OSError, ValueError, KeyError):
            pass

    try:
        filas = get_leaderboard()
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("No se pudo leer el ranking (%s). Uso solo las ballenas manuales.", e)
        return manuales

    # 1-2. Filtro rápido con los datos del ranking (sin peticiones extra).
    candidatas = []
    descartes = {"tamaño/beneficio": 0, "rotación (creador de mercado)": 0}
    for fila in filas:
        valor = float(fila.get("accountValue") or 0)
        pnl_dia, _, _ = _ventana(fila, "day")
        pnl_sem, _, _ = _ventana(fila, "week")
        pnl_mes, roi_mes, vlm_mes = _ventana(fila, "month")
        pnl_total, _, _ = _ventana(fila, "allTime")
        if not (valor >= f["valor_cuenta_min_usd"] and pnl_total >= f["pnl_historico_min_usd"] and pnl_mes > 0
                and pnl_dia > -abs(pnl_mes) * 0.5 and roi_mes <= f["roi_mensual_max"]):
            descartes["tamaño/beneficio"] += 1
            continue
        rotacion = vlm_mes / valor if valor else 1e9
        if rotacion > f["rotacion_mensual_max"]:
            descartes["rotación (creador de mercado)"] += 1
            continue
        candidatas.append((pnl_mes, fila["ethAddress"].lower(), rotacion))
    candidatas.sort(reverse=True)
    candidatas = candidatas[: f["candidatas_a_analizar"]]
    if log:
        log.info("Ranking: %d cuentas; %d candidatas tras filtrar (descartes: %s). Analizo su forma de operar...",
                 len(filas), len(candidatas), descartes)

    # 3. Comportamiento real: últimas operaciones de cada candidata.
    monedas = set(monedas)
    elegidas, motivos = [], {"alta frecuencia": 0, "otras monedas": 0, "inactiva": 0, "sin datos": 0}
    for i, (pnl_mes, direccion, rotacion) in enumerate(candidatas, 1):
        if len(elegidas) >= f["numero_ballenas"]:
            break
        try:
            fills = post({"type": "userFills", "user": direccion}, 120)
        except Exception as e:  # noqa: BLE001
            if log:
                log.warning("No pude leer las operaciones de %s: %s", direccion[:10], e)
            motivos["sin datos"] += 1
            continue
        p = perfil_por_fills(fills, monedas)
        if not p:
            motivos["sin datos"] += 1
            continue
        if p["ordenes_dia"] > f["ordenes_dia_max"]:
            motivos["alta frecuencia"] += 1
            continue
        if p["frac_monedas"] < f["fraccion_min_en_monedas"]:
            motivos["otras monedas"] += 1
            continue
        if ahora - p["ultima_ts"] > f["dias_max_sin_operar"] * 86400:
            motivos["inactiva"] += 1
            continue
        elegidas.append(direccion)
        if log:
            log.info("  ✓ %s  ganó %.0fk $ este mes · rotación %.0fx · %.0f órdenes/día · %.0f%% en nuestras monedas",
                     direccion[:10], pnl_mes / 1000, rotacion, p["ordenes_dia"], p["frac_monedas"] * 100)
    if log:
        log.info("Ballenas elegidas: %d (descartadas: %s)", len(elegidas), motivos)
    if ruta_cache and elegidas:
        try:
            with open(ruta_cache, "w", encoding="utf-8") as fh:
                json.dump({"ts": ahora, "ballenas": elegidas}, fh)
        except OSError:
            pass
    return list(dict.fromkeys(manuales + elegidas))
