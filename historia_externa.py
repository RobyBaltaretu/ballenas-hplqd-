"""
Historia ampliada para el laboratorio con DATOS PÚBLICOS de Binance y Bybit (futuros perpetuos en USDT).

Por qué: Hyperliquid solo entrega las últimas 5000 velas (≈ 7 meses en 1 h, ≈ 2,3 años en 4 h) y su funding
empieza cuando cada moneda se listó allí. Con tan poca historia el laboratorio casi nunca puede juzgar una
estrategia. Binance y Bybit tienen años de velas y funding de las mismas monedas.

Reglas (para no engañarnos):
  - Solo LECTURA de endpoints públicos de mercado: sin cuenta, sin claves, sin órdenes. Nada de esto opera.
  - Hyperliquid MANDA: las velas externas solo se usan ANTES de la primera vela de Hyperliquid (y del archivo
    propio del bot). Donde hay datos de Hyperliquid, se usan esos.
  - Antes de usar una fuente se COMPRUEBA el solape con Hyperliquid: si el precio no coincide (otro token con el
    mismo ticker, otra escala…) la fuente se rechaza para esa moneda y se anota el motivo.
  - El volumen externo se ESCALA al de Hyperliquid (factor medido en el solape), para que el filtro de universo
    "en el momento" no favorezca a las monedas con historia externa.
  - El funding externo (cada 8 h o 4 h) se reparte en registros horarios, que es el formato de Hyperliquid.
  - Cada vela lleva "fuente" y el informe dice cuántas velas externas hay por moneda; además el laboratorio
    calcula los resultados SOLO en el tramo de Hyperliquid como comprobación.

Limitación que sigue: el precio, la liquidez y el funding de otro exchange no son idénticos a los de Hyperliquid
antes de que la moneda existiera allí. Es una aproximación razonable, no una copia exacta.
"""

import json
import os
import statistics
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_EXTERNO = os.path.join(AQUI, "datos_backtest", "externo")
HORA_MS = 3_600_000
MIN_IV = {"1h": 60, "4h": 240, "8h": 480}

DEFECTOS = {"activa": True, "fuentes": ["binance", "bybit"], "anos_1h": 4, "anos_4h": 6,
            "desviacion_max_pct": 1.0, "min_velas_solape": 100, "volumen_min_pool_usd": 2_000_000}


# ============================================================ HTTP con límite y reintentos
class Cliente:
    """GET con un presupuesto de peticiones por minuto y reintentos. 'get' se puede inyectar (pruebas: sin red)."""

    def __init__(self, get=None, por_minuto=600, dormir=time.sleep):
        if get is None:
            import requests

            def get(url, params):
                r = requests.get(url, params=params, timeout=30)
                if r.status_code in (418, 429):
                    raise ErrorLimite(r.status_code)
                r.raise_for_status()
                return r.json()
        self._get, self.por_minuto, self._dormir = get, por_minuto, dormir
        self._marcas = []

    def get(self, url, params):
        for intento in range(5):
            ahora = time.time()
            self._marcas = [t for t in self._marcas if ahora - t < 60]
            if len(self._marcas) >= self.por_minuto:
                self._dormir(1)
                continue
            self._marcas.append(ahora)
            try:
                return self._get(url, params)
            except ErrorLimite:
                self._dormir(15 * (intento + 1))
            except Exception:  # noqa: BLE001  (red caída, respuesta rara...)
                if intento == 4:
                    raise
                self._dormir(3 * (intento + 1))
        raise RuntimeError(f"sin respuesta de {url}")


class ErrorLimite(Exception):
    pass


# ============================================================ fuentes
class Binance:
    nombre = "binance"
    URL = "https://fapi.binance.com"
    LIMITE = 1500

    def __init__(self, cliente):
        self.c = cliente
        self._simbolos = None

    def simbolos(self):
        if self._simbolos is None:
            d = self.c.get(self.URL + "/fapi/v1/exchangeInfo", {})
            self._simbolos = {s["symbol"] for s in d.get("symbols", [])
                              if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"}
        return self._simbolos

    def velas(self, simbolo, iv, desde_ms, hasta_ms):
        out, t = [], desde_ms
        while t <= hasta_ms:
            lote = self.c.get(self.URL + "/fapi/v1/klines", {"symbol": simbolo, "interval": iv, "startTime": t,
                                                           "endTime": hasta_ms, "limit": self.LIMITE})
            if not lote:
                break
            for k in lote:
                out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                            "c": float(k[4]), "v": float(k[5]), "tb": float(k[9]) if len(k) > 9 else None})
            ultimo = int(lote[-1][0])
            if ultimo < t or len(lote) < self.LIMITE:
                break
            t = ultimo + MIN_IV[iv] * 60_000
        return out

    def funding(self, simbolo, desde_ms, hasta_ms):
        out, t = [], desde_ms
        while t <= hasta_ms:
            lote = self.c.get(self.URL + "/fapi/v1/fundingRate", {"symbol": simbolo, "startTime": t,
                                                                "endTime": hasta_ms, "limit": 1000})
            if not lote:
                break
            out += [{"time": int(x["fundingTime"]), "fundingRate": float(x["fundingRate"])} for x in lote]
            ultimo = int(lote[-1]["fundingTime"])
            if ultimo < t or len(lote) < 1000:
                break
            t = ultimo + 1
        return out


class BinanceSpot(Binance):
    """Velas de CONTADO de Binance (pata spot del estudio de carry). Mismo formato que las de futuros."""
    nombre = "binance_spot"
    URL = "https://api.binance.com"
    LIMITE = 1000

    def simbolos(self):
        if self._simbolos is None:
            d = self.c.get(self.URL + "/api/v3/exchangeInfo", {"permissions": "SPOT"})
            self._simbolos = {x["symbol"] for x in d.get("symbols", []) if x.get("quoteAsset") == "USDT"}
        return self._simbolos

    def velas(self, simbolo, iv, desde_ms, hasta_ms):
        out, t = [], desde_ms
        while t <= hasta_ms:
            lote = self.c.get(self.URL + "/api/v3/klines", {"symbol": simbolo, "interval": iv, "startTime": t,
                                                         "endTime": hasta_ms, "limit": self.LIMITE})
            if not lote:
                break
            out += [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                     "v": float(k[5])} for k in lote]
            if len(lote) < self.LIMITE:
                break
            t = int(lote[-1][0]) + MIN_IV[iv] * 60_000
        return out


class Bybit:
    nombre = "bybit"
    URL = "https://api.bybit.com"
    LIMITE = 1000
    IV = {"1h": "60", "4h": "240"}

    def __init__(self, cliente):
        self.c = cliente
        self._simbolos = None

    def simbolos(self):
        if self._simbolos is None:
            sims, cursor = set(), None
            for _ in range(20):
                p = {"category": "linear", "limit": 1000}
                if cursor:
                    p["cursor"] = cursor
                d = self.c.get(self.URL + "/v5/market/instruments-info", p).get("result", {})
                sims |= {s["symbol"] for s in d.get("list", [])
                         if s.get("contractType") == "LinearPerpetual" and s.get("quoteCoin") == "USDT"}
                cursor = d.get("nextPageCursor")
                if not cursor:
                    break
            self._simbolos = sims
        return self._simbolos

    def velas(self, simbolo, iv, desde_ms, hasta_ms):
        """Bybit devuelve de la más nueva a la más vieja: se pagina hacia atrás moviendo 'end'."""
        out, fin = {}, hasta_ms
        for _ in range(500):
            d = self.c.get(self.URL + "/v5/market/kline", {"category": "linear", "symbol": simbolo,
                                                          "interval": self.IV[iv], "start": desde_ms, "end": fin,
                                                          "limit": self.LIMITE}).get("result", {})
            lote = d.get("list") or []
            if not lote:
                break
            for k in lote:
                t = int(k[0])
                if desde_ms <= t <= hasta_ms:
                    out[t] = {"t": t, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                              "v": float(k[5])}
            primero = min(int(k[0]) for k in lote)
            if primero <= desde_ms or len(lote) < self.LIMITE:
                break
            fin = primero - 1
        return [out[t] for t in sorted(out)]

    def funding(self, simbolo, desde_ms, hasta_ms):
        out, fin = {}, hasta_ms
        for _ in range(500):
            d = self.c.get(self.URL + "/v5/market/funding/history", {"category": "linear", "symbol": simbolo,
                                                                     "startTime": desde_ms, "endTime": fin,
                                                                     "limit": 200}).get("result", {})
            lote = d.get("list") or []
            if not lote:
                break
            for x in lote:
                t = int(x["fundingRateTimestamp"])
                out[t] = {"time": t, "fundingRate": float(x["fundingRate"])}
            primero = min(int(x["fundingRateTimestamp"]) for x in lote)
            if primero <= desde_ms or len(lote) < 200:
                break
            fin = primero - 1
        return [out[t] for t in sorted(out)]


def crear_fuentes(nombres, cliente=None):
    # Binance cuenta 10 de peso por petición de 1500 velas (máximo 2400 por minuto): 150 peticiones/min deja margen.
    cliente = cliente or Cliente(por_minuto=150)
    tipos = {"binance": Binance, "bybit": Bybit}
    return [tipos[n](cliente) for n in nombres if n in tipos]


# ============================================================ utilidades
def simbolos_candidatos(moneda):
    """Nombre de Hyperliquid -> símbolos posibles en USDT. 'kPEPE' en Hyperliquid = 1000 PEPE = '1000PEPEUSDT'."""
    base = moneda.split(":")[-1]
    out = []
    if base.startswith("k") and len(base) > 1 and base[1:].isupper():
        out.append("1000" + base[1:] + "USDT")
    out.append(base.upper() + "USDT")
    return out


def _cache(ruta, descargar, desde_ms, hasta_ms, clave_t, paso_ms):
    """Guarda en disco lo descargado y solo pide lo que falta (antes del primero y después del último)."""
    regs = []
    try:
        with open(ruta, encoding="utf-8") as fh:
            regs = json.load(fh)
    except (OSError, ValueError):
        regs = []
    if regs:
        primero, ultimo = regs[0][clave_t], regs[-1][clave_t]
        if desde_ms < primero - paso_ms:
            regs = descargar(desde_ms, primero - 1) + regs
        if hasta_ms > ultimo + paso_ms:
            regs = regs + descargar(ultimo + 1, hasta_ms)
    else:
        regs = descargar(desde_ms, hasta_ms)
    regs = sorted({int(x[clave_t]): x for x in regs}.values(), key=lambda x: x[clave_t])
    try:
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(regs, fh)
        os.replace(tmp, ruta)
    except OSError:
        pass
    return [x for x in regs if desde_ms <= x[clave_t] <= hasta_ms]


def velas_cacheadas(fuente, simbolo, iv, desde_ms, hasta_ms, carpeta=DIR_EXTERNO):
    # v2: las velas de Binance guardan también el volumen comprador agresivo ("tb", flujo de órdenes).
    ruta = os.path.join(carpeta, f"{fuente.nombre}_{simbolo}_{iv}_v2.json")
    return _cache(ruta, lambda a, b: fuente.velas(simbolo, iv, a, b), desde_ms, hasta_ms, "t", MIN_IV[iv] * 60_000)


def funding_cacheado(fuente, simbolo, desde_ms, hasta_ms, carpeta=DIR_EXTERNO):
    ruta = os.path.join(carpeta, f"{fuente.nombre}_{simbolo}_funding.json")
    return _cache(ruta, lambda a, b: fuente.funding(simbolo, a, b), desde_ms, hasta_ms, "time", 8 * HORA_MS)


def comprobar_solape(velas_hl, velas_ext, min_velas=100, desviacion_max_pct=1.0):
    """¿Es la misma moneda y la misma escala? Compara cierres en las velas comunes.
    Devuelve (ok, info) con la desviación mediana en % y el factor de volumen HL/externo."""
    ext = {int(v["t"]): v for v in velas_ext}
    pares = [(v, ext[int(v["t"])]) for v in velas_hl if int(v["t"]) in ext]
    info = {"velas_solape": len(pares)}
    if len(pares) < min_velas:
        return False, dict(info, motivo=f"solo {len(pares)} velas en común con Hyperliquid (mínimo {min_velas})")
    desv = [abs(float(a["c"]) / float(b["c"]) - 1) * 100 for a, b in pares if float(b["c"]) > 0]
    info["desviacion_mediana_pct"] = round(statistics.median(desv), 4) if desv else None
    if not desv or info["desviacion_mediana_pct"] > desviacion_max_pct:
        return False, dict(info, motivo=f"el precio no coincide con Hyperliquid (desviación mediana "
                                        f"{info['desviacion_mediana_pct']} % > {desviacion_max_pct} %)")
    vol_hl = sum(float(a.get("v") or 0) for a, _ in pares)
    vol_ext = sum(float(b.get("v") or 0) for _, b in pares)
    info["factor_volumen"] = round(vol_hl / vol_ext, 6) if vol_ext > 0 else 0.0
    return True, info


def anteponer(velas_hl, velas_ext, factor_volumen, fuente):
    """Velas externas ANTERIORES a la primera de Hyperliquid (con el volumen escalado) + las de Hyperliquid."""
    if not velas_hl:
        return list(velas_hl), 0
    primera = int(velas_hl[0]["t"])
    previas = [dict(v, v=float(v.get("v") or 0) * factor_volumen, fuente=fuente)
               for v in velas_ext if int(v["t"]) < primera]
    return previas + list(velas_hl), len(previas)


def funding_horario(regs_ext, hasta_ms=None):
    """Funding por periodo (8 h o 4 h) -> registros horarios con la tasa repartida (formato Hyperliquid).
    Cada pago cubre las horas desde el pago anterior; el primero se asume de 8 h."""
    out, previo = [], None
    for x in sorted(regs_ext, key=lambda r: r["time"]):
        t = int(x["time"]) // HORA_MS * HORA_MS
        horas = 8 if previo is None else max(1, min(8, round((t - previo) / HORA_MS)))
        tasa = float(x["fundingRate"]) / horas
        for h in range(horas - 1, -1, -1):
            th = t - h * HORA_MS
            if hasta_ms is None or th < hasta_ms:
                out.append({"time": th, "fundingRate": tasa, "fuente": "externo"})
        previo = t
    return sorted({r["time"]: r for r in out}.values(), key=lambda r: r["time"])


# ============================================================ ampliación de una moneda
class Ampliador:
    """Une la historia externa a las velas y el funding de Hyperliquid de cada moneda. Recuerda qué fuente se
    aceptó para cada moneda (la misma para 1 h, 4 h y funding) y por qué se rechazaron las otras."""

    def __init__(self, cfg, fuentes, fin_ms, carpeta=DIR_EXTERNO, log=print):
        self.cfg = dict(DEFECTOS, **(cfg or {}))
        self.fuentes, self.fin_ms, self.carpeta, self.log = fuentes, fin_ms, carpeta, log
        self.elegida = {}          # moneda -> (fuente, simbolo)
        self.informe = {}          # moneda -> {...}
        # Series externas COMPLETAS (también donde hay Hyperliquid) para la vista de mercado unificada.
        self.series = {}           # (moneda, iv) -> velas externas
        self.funding_ext = {}      # moneda -> registros horarios externos

    def _desde(self, iv):
        anos = self.cfg["anos_4h"] if iv == "4h" else self.cfg["anos_1h"]
        return self.fin_ms - int(anos * 365.25 * 86400_000)

    def velas(self, moneda, iv, velas_hl):
        inf = self.informe.setdefault(moneda, {"fuente": None, "rechazos": []})
        desde = self._desde(iv)
        if not velas_hl or int(velas_hl[0]["t"]) <= desde:
            return velas_hl                      # Hyperliquid ya cubre todo el periodo pedido
        candidatas = ([self.elegida[moneda]] if moneda in self.elegida else
                      [(f, s) for f in self.fuentes for s in simbolos_candidatos(moneda)])
        for fuente, simbolo in candidatas:
            try:
                if simbolo not in fuente.simbolos():
                    continue
                ext = velas_cacheadas(fuente, simbolo, iv, desde, self.fin_ms, self.carpeta)
            except Exception as e:  # noqa: BLE001  (sin red, fuente bloqueada en el país...)
                inf["rechazos"].append(f"{fuente.nombre}: no responde ({type(e).__name__})")
                continue
            ok, sol = comprobar_solape(velas_hl, ext, self.cfg["min_velas_solape"], self.cfg["desviacion_max_pct"])
            if not ok:
                inf["rechazos"].append(f"{fuente.nombre} {simbolo} {iv}: {sol['motivo']}")
                continue
            juntas, n = anteponer(velas_hl, ext, sol["factor_volumen"], fuente.nombre)
            self.elegida[moneda] = (fuente, simbolo)
            self.series[(moneda, iv)] = ext
            inf.update({"fuente": fuente.nombre, "simbolo": simbolo})
            inf[iv] = {"velas_externas": n, "desde": juntas[0]["t"] if juntas else None,
                       "inicio_hyperliquid": int(velas_hl[0]["t"]),
                       "desviacion_mediana_pct": sol["desviacion_mediana_pct"],
                       "factor_volumen": sol["factor_volumen"]}
            return juntas
        return velas_hl

    def funding(self, moneda, regs_hl):
        """Antepone funding externo (en horas) antes del primer registro de Hyperliquid, con la fuente ya
        validada por el precio. Sin fuente validada, devuelve el de Hyperliquid tal cual."""
        if moneda not in self.elegida:
            return regs_hl
        fuente, simbolo = self.elegida[moneda]
        desde = self._desde("4h")
        primero = regs_hl[0]["time"] if regs_hl else self.fin_ms
        if primero <= desde:
            return regs_hl
        try:
            completo = funding_cacheado(fuente, simbolo, desde, self.fin_ms, self.carpeta)
            self.funding_ext[moneda] = funding_horario(completo)
            ext = [x for x in completo if x["time"] <= primero]
        except Exception as e:  # noqa: BLE001
            self.informe.setdefault(moneda, {"rechazos": []})["rechazos"].append(
                f"{fuente.nombre}: funding no disponible ({type(e).__name__})")
            return regs_hl
        previos = funding_horario(ext, hasta_ms=primero)
        self.informe[moneda]["funding_externo_horas"] = len(previos)
        return previos + list(regs_hl)
