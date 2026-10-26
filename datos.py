"""
Descarga de datos públicos de Hyperliquid con caché en disco y respeto al límite de peticiones.
Lo usan el laboratorio y las herramientas de análisis (no hace falta ninguna clave).
"""

import hashlib
import json
import os
import time

import requests

AQUI = os.path.dirname(os.path.abspath(__file__))
URL_INFO = "https://api.hyperliquid.xyz/info"
DIR_CACHE = os.path.join(AQUI, "datos_backtest")
MIN_INTERVALO = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


# ============================================================ descarga de datos
class Api:
    """POST a /info respetando el límite de 1200 de peso por minuto, con caché en disco."""

    def __init__(self, usar_cache=True, presupuesto=1000):
        self.usar_cache = usar_cache
        self.presupuesto = presupuesto     # peso por minuto; más bajo si el bot está funcionando a la vez
        self.historial = []  # (ts, peso)
        os.makedirs(DIR_CACHE, exist_ok=True)
        self.limpiar_cache()

    @staticmethod
    def limpiar_cache(dias=7):
        """Cada consulta nueva se guarda en caché; las de más de 'dias' días ya no se reutilizan (el periodo
        pedido cambia cada hora), así que se borran para que el disco no se llene con ejecuciones diarias."""
        limite = time.time() - dias * 86400
        try:
            for f in os.listdir(DIR_CACHE):
                ruta = os.path.join(DIR_CACHE, f)
                if f.endswith(".json") and os.path.getmtime(ruta) < limite:
                    os.remove(ruta)
        except OSError:
            pass

    def _esperar(self, peso):
        while True:
            ahora = time.time()
            self.historial = [(t, p) for t, p in self.historial if ahora - t < 60]
            if sum(p for _, p in self.historial) + peso <= self.presupuesto:
                self.historial.append((ahora, peso))
                return
            time.sleep(1)

    def post(self, cuerpo, peso=20, cache=True):
        clave = hashlib.sha1(json.dumps(cuerpo, sort_keys=True).encode()).hexdigest()[:20]
        ruta = os.path.join(DIR_CACHE, clave + ".json")
        if cache and self.usar_cache and os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        for intento in range(5):
            self._esperar(peso)
            try:
                r = requests.post(URL_INFO, json=cuerpo, timeout=30)
                if r.status_code == 429:
                    time.sleep(10 * (intento + 1))
                    continue
                r.raise_for_status()
                datos = r.json()
                if cache:
                    with open(ruta, "w", encoding="utf-8") as f:
                        json.dump(datos, f)
                return datos
            except requests.RequestException as e:
                if intento == 4:
                    raise
                print(f"   reintento por error de red: {e}")
                time.sleep(5 * (intento + 1))
        raise RuntimeError("Hyperliquid no responde")


def descargar_fills(api, ballena, inicio_ms, fin_ms):
    todos, vistos, desde = [], set(), inicio_ms
    for _ in range(40):
        lote = api.post({"type": "userFillsByTime", "user": ballena, "startTime": desde, "endTime": fin_ms,
                         "aggregateByTime": True}, peso=60)
        nuevos = [f for f in lote if (f.get("tid"), f.get("time"), f.get("oid")) not in vistos]
        if not nuevos:
            break
        for f in nuevos:
            vistos.add((f.get("tid"), f.get("time"), f.get("oid")))
        todos.extend(nuevos)
        ultimo = max(f["time"] for f in lote)
        if ultimo <= desde:
            break
        desde = ultimo + 1
    return sorted(todos, key=lambda f: f["time"])


def descargar_velas(api, moneda, intervalo, inicio_ms, fin_ms):
    datos = api.post({"type": "candleSnapshot",
                      "req": {"coin": moneda, "interval": intervalo, "startTime": inicio_ms, "endTime": fin_ms}},
                     peso=110)
    return sorted(datos, key=lambda v: v["t"])


def descargar_funding(api, moneda, inicio_ms, fin_ms):
    todos, desde = [], inicio_ms
    for _ in range(200):
        lote = api.post({"type": "fundingHistory", "coin": moneda, "startTime": desde, "endTime": fin_ms}, peso=20,
                        cache=False)
        if not lote:
            break
        todos.extend(lote)
        ultimo = max(x["time"] for x in lote)
        if ultimo <= desde or len(lote) < 400:
            break
        desde = ultimo + 1
    return sorted({x["time"]: x for x in todos}.values(), key=lambda x: x["time"])


def descargar_funding_cacheado(api, moneda, inicio_ms, fin_ms):
    """Como descargar_funding, pero guarda la historia de cada moneda en un archivo y solo pide lo que falta
    (antes del primer registro guardado y después del último). Las repeticiones del laboratorio son rápidas."""
    ruta = os.path.join(DIR_CACHE, f"funding_{moneda.replace(':', '_')}.json")
    regs = []
    if api.usar_cache:
        try:
            with open(ruta, encoding="utf-8") as f:
                regs = json.load(f)
        except (OSError, ValueError):
            regs = []
    if regs:
        primero, ultimo = regs[0]["time"], regs[-1]["time"]
        if inicio_ms < primero - 3600_000:
            regs = descargar_funding(api, moneda, inicio_ms, primero - 1) + regs
        if fin_ms > ultimo + 3600_000:
            regs = regs + descargar_funding(api, moneda, ultimo + 1, fin_ms)
    else:
        regs = descargar_funding(api, moneda, inicio_ms, fin_ms)
    regs = sorted({x["time"]: x for x in regs}.values(), key=lambda x: x["time"])
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(regs, f)
    except OSError:
        pass
    return [x for x in regs if inicio_ms <= x["time"] <= fin_ms]


def descargar_universo(api):
    """Perpetuos del mercado principal con su volumen de 24 h (sin caché: cambia a diario)."""
    meta, ctxs = api.post({"type": "metaAndAssetCtxs"}, peso=20, cache=False)
    out = []
    for a, x in zip(meta["universe"], ctxs):
        if a.get("isDelisted"):
            continue
        out.append({"moneda": a["name"], "volumen_24h": float(x.get("dayNtlVlm") or 0),
                    "max_apalancamiento": a.get("maxLeverage", 1), "sz_decimals": a.get("szDecimals", 0)})
    return sorted(out, key=lambda x: -x["volumen_24h"])
