"""
Contexto de mercado de Binance y Bybit en VIVO (solo datos públicos, solo lectura).

El bot opera SOLO en Hyperliquid. De Binance y Bybit no se usa ninguna cuenta ni clave y nunca se envía
nada: cada pocos minutos se leen sus datos públicos de futuros perpetuos en USDT para las monedas que vigila
el bot y se calcula, por moneda:
  - precio en cada exchange y diferencia con el de Hyperliquid (%);
  - funding por hora en cada exchange (Binance/Bybit pagan cada 8 h o 4 h: se pasa a %/h) y su diferencia
    con el de Hyperliquid;
  - interés abierto (USD) y su cambio en la última hora;
  - volumen de 24 h y qué parte del volumen total representa Hyperliquid.

Uso hoy: INVESTIGACIÓN e INFORME. Se muestra en el panel y se archiva (datos_mercado/externo_AAAA-MM-DD.jsonl)
para que el laboratorio mida si anticipa algo (laboratorio.probar_externo, a partir de 14 días). No cambia
ninguna decisión del bot: si algún día se usa como filtro o señal, entra por las etapas normales (laboratorio,
shadow, simulado) como cualquier estrategia.

Si un exchange no responde (o está bloqueado en tu país) se marca y el bot sigue igual.
"""

import json
import os
import threading
import time
from collections import defaultdict, deque

from historia_externa import Binance, Bybit, Cliente, simbolos_candidatos

DEFECTOS = {"activo": True, "cada_min": 5, "fuentes": ["binance", "bybit"], "desviacion_max_pct": 2.0}


def _dia(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class LectorBinance:
    nombre = "binance"

    def __init__(self, cliente):
        self.c, self.url = cliente, Binance.URL
        self._intervalos, self._intervalos_ts = {}, 0.0

    def _horas_funding(self):
        if time.time() - self._intervalos_ts > 3600:
            try:
                info = self.c.get(self.url + "/fapi/v1/fundingInfo", {})
                self._intervalos = {x["symbol"]: int(x.get("fundingIntervalHours") or 8) for x in info}
            except Exception:  # noqa: BLE001  (si falla, 8 h por defecto)
                pass
            self._intervalos_ts = time.time()
        return self._intervalos

    def leer(self, simbolos):
        """{simbolo: {precio, funding_h_pct, vol24h_usd, oi_usd}} de los símbolos pedidos que existen."""
        prem = {x["symbol"]: x for x in self.c.get(self.url + "/fapi/v1/premiumIndex", {})}
        tick = {x["symbol"]: x for x in self.c.get(self.url + "/fapi/v1/ticker/24hr", {})}
        horas = self._horas_funding()
        out = {}
        for s in simbolos:
            p, t = prem.get(s), tick.get(s)
            if not p or not t:
                continue
            precio = float(p.get("markPrice") or t.get("lastPrice") or 0)
            fila = {"precio": precio, "funding_h_pct": float(p.get("lastFundingRate") or 0) * 100 / horas.get(s, 8),
                    "vol24h_usd": float(t.get("quoteVolume") or 0), "oi_usd": None}
            try:
                oi = self.c.get(self.url + "/fapi/v1/openInterest", {"symbol": s})
                fila["oi_usd"] = float(oi.get("openInterest") or 0) * precio
            except Exception:  # noqa: BLE001
                pass
            out[s] = fila
        return out


    def recientes(self, simbolo):
        """Últimas velas de 1 h y 4 h (con volumen comprador agresivo) y funding de ~2 días, para el cerebro."""
        out = {}
        for iv in ("1h", "4h"):
            ks = self.c.get(self.url + "/fapi/v1/klines", {"symbol": simbolo, "interval": iv, "limit": 12})
            out[iv] = [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                        "v": float(k[5]), "tb": float(k[9])} for k in ks]
        fr = self.c.get(self.url + "/fapi/v1/fundingRate", {"symbol": simbolo, "limit": 8})
        out["funding"] = [{"time": int(x["fundingTime"]), "fundingRate": float(x["fundingRate"])} for x in fr]
        return out


class LectorBybit:
    nombre = "bybit"

    def __init__(self, cliente):
        self.c, self.url = cliente, Bybit.URL
        self._intervalos, self._intervalos_ts = {}, 0.0

    def _horas_funding(self):
        if time.time() - self._intervalos_ts > 3600:
            try:
                d = self.c.get(self.url + "/v5/market/instruments-info", {"category": "linear", "limit": 1000})
                self._intervalos = {x["symbol"]: max(1, int(x.get("fundingInterval") or 480) // 60)
                                    for x in d.get("result", {}).get("list", [])}
            except Exception:  # noqa: BLE001
                pass
            self._intervalos_ts = time.time()
        return self._intervalos

    def leer(self, simbolos):
        d = self.c.get(self.url + "/v5/market/tickers", {"category": "linear"})
        tick = {x["symbol"]: x for x in d.get("result", {}).get("list", [])}
        horas = self._horas_funding()
        out = {}
        for s in simbolos:
            t = tick.get(s)
            if not t:
                continue
            out[s] = {"precio": float(t.get("markPrice") or t.get("lastPrice") or 0),
                      "funding_h_pct": float(t.get("fundingRate") or 0) * 100 / horas.get(s, 8),
                      "vol24h_usd": float(t.get("turnover24h") or 0),
                      "oi_usd": float(t.get("openInterestValue") or 0) or None}
        return out


class ContextoExterno:
    def __init__(self, cfg, carpeta, log=None, lectores=None):
        self.cfg = dict(DEFECTOS, **(cfg or {}))
        self.carpeta, self.log = carpeta, log
        if lectores is None:
            cli = Cliente(por_minuto=120)
            tipos = {"binance": LectorBinance, "bybit": LectorBybit}
            lectores = [tipos[n](cli) for n in self.cfg["fuentes"] if n in tipos]
        self.lectores = lectores
        self.ultimo, self.hilo, self.bloqueo = float("-inf"), None, threading.Lock()
        self.datos = {}                       # moneda -> fila
        self.estado = {lec.nombre: {"estado": "sin_datos", "detalle": "aún no se ha leído", "ts": None}
                       for lec in self.lectores}
        self.oi_hist = defaultdict(lambda: deque(maxlen=40))     # moneda -> [(ts, oi_total)]
        self.no_coincide = {}                 # (fuente, moneda) -> motivo
        self.ts = None
        self.escritos = 0
        self.recientes = {}                   # moneda -> {"ts", "1h": velas, "4h": velas, "funding": regs}

    # ------------------------------------------------------------ ciclo
    def actualizar(self, monedas, mids, funding_hl, volumen_hl=None, ahora=None, en_segundo_plano=True):
        """Llamar en cada ciclo; solo lee cada 'cada_min' minutos y en un hilo aparte (nunca frena el bot)."""
        if not self.cfg["activo"] or not monedas or not self.lectores:
            return False
        ahora = time.time() if ahora is None else ahora
        if ahora - self.ultimo < self.cfg["cada_min"] * 60 or (self.hilo is not None and self.hilo.is_alive()):
            return False
        self.ultimo = ahora
        args = (list(monedas), dict(mids or {}), dict(funding_hl or {}), dict(volumen_hl or {}), ahora)
        if en_segundo_plano:
            self.hilo = threading.Thread(target=self._leer, args=args, daemon=True)
            self.hilo.start()
        else:
            self._leer(*args)
        return True

    def _leer(self, monedas, mids, funding_hl, volumen_hl, ahora):
        por_fuente = {}
        for lec in self.lectores:
            sims = {s: m for m in monedas for s in simbolos_candidatos(m)}
            try:
                leido = lec.leer(list(sims))
                por_fuente[lec.nombre] = {}
                for s, fila in leido.items():
                    m = sims[s]
                    mid = mids.get(m)
                    if mid and fila["precio"]:
                        dif = (fila["precio"] / mid - 1) * 100
                        if abs(dif) > self.cfg["desviacion_max_pct"]:
                            self.no_coincide[(lec.nombre, m)] = f"{s}: precio {dif:+.2f} % frente a Hyperliquid"
                            continue
                        fila = dict(fila, dif_precio_pct=round(dif, 4), simbolo=s)
                    elif not mid:
                        continue
                    if m not in por_fuente[lec.nombre]:
                        por_fuente[lec.nombre][m] = fila
                self.estado[lec.nombre] = {"estado": "correcto", "detalle": f"{len(por_fuente[lec.nombre])} monedas",
                                           "ts": ahora}
            except Exception as e:  # noqa: BLE001  (bloqueo regional, red caída...)
                self.estado[lec.nombre] = {"estado": "fallido", "detalle": f"no responde ({type(e).__name__})",
                                           "ts": ahora}
        filas = {}
        for m in monedas:
            ext = {f: d[m] for f, d in por_fuente.items() if m in d}
            if not ext:
                continue
            fund_ext = [x["funding_h_pct"] for x in ext.values()]
            oi_tot = sum(x["oi_usd"] or 0 for x in ext.values()) or None
            vol_ext = sum(x["vol24h_usd"] or 0 for x in ext.values())
            hist = self.oi_hist[m]
            if oi_tot:
                hist.append((ahora, oi_tot))
            previo = next((o for t, o in hist if ahora - t <= 3900 and ahora - t >= 3300), None)
            fh = funding_hl.get(m)
            vh = volumen_hl.get(m)
            filas[m] = {
                "t": round(ahora), "m": m, "fuentes": {f: {k: (round(v, 8) if isinstance(v, float) else v)
                                                          for k, v in x.items()} for f, x in ext.items()},
                "precio_hl": mids.get(m), "funding_hl_h_pct": fh,
                "funding_externo_h_pct": round(sum(fund_ext) / len(fund_ext), 8),
                "dif_funding_h_pct": round(fh - sum(fund_ext) / len(fund_ext), 8) if fh is not None else None,
                "oi_externo_usd": round(oi_tot) if oi_tot else None,
                "oi_externo_cambio_1h_pct": round((oi_tot / previo - 1) * 100, 3) if oi_tot and previo else None,
                "vol24h_externo_usd": round(vol_ext),
                "cuota_hl_volumen_pct": round(vh / (vh + vol_ext) * 100, 2) if vh and vol_ext else None,
            }
        with self.bloqueo:
            self.datos, self.ts = filas, ahora
        self._archivar(filas.values(), ahora)
        # Series recientes de Binance para el cerebro (mismas definiciones que en el laboratorio).
        lec = next((x for x in self.lectores if hasattr(x, "recientes")), None)
        for m, fila in (por_fuente.get(getattr(lec, "nombre", ""), {}) or {}).items() if lec else []:
            if not fila.get("simbolo"):
                continue
            try:
                r = lec.recientes(fila["simbolo"])
                with self.bloqueo:
                    self.recientes[m] = dict(r, ts=ahora)
            except Exception:  # noqa: BLE001  (sin serie: el cerebro trata esos rasgos como «sin dato»)
                continue

    def series_cerebro(self, moneda, iv, max_antiguedad_seg=1200, ahora=None):
        """(velas externas recientes, funding externo horario) o (None, None) si no hay datos o son viejos."""
        from historia_externa import funding_horario
        ahora = time.time() if ahora is None else ahora
        with self.bloqueo:
            r = self.recientes.get(moneda)
        if not r or ahora - r.get("ts", 0) > max_antiguedad_seg:
            return None, None
        # SOLO velas cerradas a la hora de decidir (como en el laboratorio): la vela en curso tiene volumen y
        # flujo parciales. Y solo funding ya cobrado.
        dur = {"1h": 3600_000, "4h": 4 * 3600_000}.get(iv, 3600_000)
        velas = [v for v in (r.get(iv) or []) if v["t"] + dur <= ahora * 1000]
        regs = [x for x in (r.get("funding") or []) if x["time"] <= ahora * 1000]
        return (velas or None), funding_horario(regs)

    def _archivar(self, filas, ahora):
        filas = list(filas)
        if not filas:
            return
        try:
            os.makedirs(self.carpeta, exist_ok=True)
            with open(os.path.join(self.carpeta, f"externo_{_dia(ahora)}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(f, separators=(",", ":")) + "\n" for f in filas))
            self.escritos += len(filas)
        except OSError as e:
            if self.log:
                self.log.warning("contexto externo: no pude archivar (%s)", e)

    # ------------------------------------------------------------ panel
    def resumen(self):
        with self.bloqueo:
            filas = sorted(self.datos.values(), key=lambda x: -(x.get("vol24h_externo_usd") or 0))
            ts = self.ts
        return {"activo": self.cfg["activo"], "ts": ts, "fuentes": self.estado, "monedas": filas,
                "no_coinciden": sorted({m for (_, m) in self.no_coincide}),
                "uso": "datos públicos: el cerebro puede usarlos como rasgos; nunca se opera en Binance ni Bybit",
                "dias_archivados": dias_archivados(self.carpeta)}


def dias_archivados(carpeta):
    try:
        return len({f[8:18] for f in os.listdir(carpeta) if f.startswith("externo_") and f.endswith(".jsonl")})
    except OSError:
        return 0


def leer_archivo(carpeta):
    """Para el laboratorio: todas las filas archivadas, en orden de tiempo."""
    out = []
    try:
        nombres = sorted(f for f in os.listdir(carpeta) if f.startswith("externo_") and f.endswith(".jsonl"))
    except OSError:
        return out
    for n in nombres:
        try:
            with open(os.path.join(carpeta, n), encoding="utf-8") as fh:
                for linea in fh:
                    try:
                        out.append(json.loads(linea))
                    except ValueError:
                        continue
        except OSError:
            continue
    return sorted(out, key=lambda x: x.get("t", 0))
