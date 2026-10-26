"""
Lo que pasa en TODO el mercado (no solo en las 15 ballenas que sigue el bot), grabado en vivo.

Hyperliquid no guarda historia de estas cosas, así que el bot las graba él mismo en la carpeta
datos_mercado/ para que, con unas semanas de datos, el laboratorio pueda comprobar si sirven:

  - Interés abierto (OI): cuánto dinero hay apostado en cada moneda, cada minuto.
      Si el precio cae fuerte y el OI cae de golpe, es que han LIQUIDADO a muchos largos: el precio
      suele pasarse de largo y rebotar. Al revés con los cortos. (Módulo "liquidaciones".)
  - Operaciones grandes de cualquier cuenta (≥ 50 000 $ por defecto), por websocket: compras y ventas
      agresivas. Es el "flujo de ballenas" de todo el mercado. También se guardan las direcciones de
      quién hace las más grandes, para descubrir ballenas nuevas.

Se usa como rasgo para el aprendizaje (oi_cambio_1h, flujo_grandes) y como señal propia
("liquidaciones", que solo opera en simulado hasta que el laboratorio la valide).
"""

import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone


def _dia(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


class Mercado:
    def __init__(self, c, log, carpeta):
        self.c, self.log, self.carpeta = c, log, carpeta
        self.cfg = c.get("mercado", {})
        os.makedirs(carpeta, exist_ok=True)
        self.bloqueo = threading.Lock()
        self.ctx = defaultdict(lambda: deque(maxlen=26 * 60))      # moneda -> [(t, oi_usd, precio, funding)]
        self.minuto = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])     # moneda -> compra_grande, venta_grande, compra, venta
        self.flujo = defaultdict(lambda: deque(maxlen=26 * 60))     # moneda -> [(t_minuto, compra_g, venta_g)]
        self.grandes = []                                           # operaciones enormes (con direcciones)
        self.ultima_foto = 0.0
        self.minuto_actual = int(time.time() // 60)
        self.ultima_senal = {}
        self.subs = {}
        self.ultimo_mensaje = 0.0          # último mensaje del websocket de trades (para vigilar el silencio)
        self.ultima_limpieza = 0.0
        self.trades_recibidos = 0
        self.observador_grandes = None      # función(evento) opcional: feed del panel

    # ---------------------------------------------------------------- grabar
    def _anotar(self, nombre, t, datos):
        try:
            with open(os.path.join(self.carpeta, f"{nombre}_{_dia(t)}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(datos, separators=(",", ":")) + "\n")
        except OSError as e:
            self.log.debug("No pude grabar %s: %s", nombre, e)

    def foto(self, info, monedas):
        """Cada minuto: interés abierto, precio y funding de las monedas vigiladas (una sola petición)."""
        ahora = time.time()
        if ahora - self.ultima_foto < 60:
            return
        self.ultima_foto = ahora
        try:
            meta, ctxs = info.meta_and_asset_ctxs()
        except Exception as e:  # noqa: BLE001
            self.log.debug("Sin foto del mercado: %s", e)
            return
        vigiladas, fila = set(monedas), {}
        for a, x in zip(meta["universe"], ctxs):
            m = a["name"]
            if m not in vigiladas:
                continue
            try:
                px = float(x.get("markPx") or x.get("midPx") or 0)
                oi = float(x.get("openInterest") or 0) * px
                fu = float(x.get("funding") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            self.ctx[m].append((ahora, oi, px, fu))
            fila[m] = [round(oi), px, fu]
        if fila:
            self._anotar("ctx", ahora, {"t": int(ahora), "d": fila})
        self._cerrar_minuto(ahora)
        self._limpiar(ahora)

    def _cerrar_minuto(self, ahora):
        m_ahora = int(ahora // 60)
        with self.bloqueo:
            if m_ahora == self.minuto_actual:
                return
            datos, self.minuto = dict(self.minuto), defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
            grandes, self.grandes = self.grandes, []
            t_min = self.minuto_actual * 60
            self.minuto_actual = m_ahora
        fila = {}
        for moneda, (cg, vg, ct, vt) in datos.items():
            self.flujo[moneda].append((t_min, cg, vg))
            fila[moneda] = [round(cg), round(vg), round(ct), round(vt)]
        if fila:
            self._anotar("flujo", t_min, {"t": t_min, "d": fila})
        for g in grandes:
            self._anotar("grandes", g["t"], g)

    def _limpiar(self, ahora):
        """Borra lo grabado hace más de 'dias_guardar' días (por defecto 120)."""
        if ahora - self.ultima_limpieza < 86400:
            return
        self.ultima_limpieza = ahora
        limite = _dia(ahora - self.cfg.get("dias_guardar", 120) * 86400)
        try:
            for f in os.listdir(self.carpeta):
                partes = f.rsplit("_", 1)
                if f.endswith(".jsonl") and len(partes) == 2 and partes[1][:10] < limite:
                    os.remove(os.path.join(self.carpeta, f))
        except OSError:
            pass

    # ---------------------------------------------------------------- websocket
    def al_recibir_trades(self, msg):
        """Callback del websocket 'trades': todas las operaciones de una moneda, de cualquier cuenta."""
        self.ultimo_mensaje = time.time()      # señal de vida del websocket (lo vigila el bot: vigilar_ws)
        umbral = self.cfg.get("operacion_grande_usd", 50_000)
        enorme = self.cfg.get("operacion_enorme_usd", 250_000)
        for tr in msg.get("data") or []:
            try:
                usd = float(tr["px"]) * float(tr["sz"])
                compra = tr["side"] == "B"          # B = el que ataca compra (agresor comprador)
                moneda = tr["coin"]
            except (KeyError, TypeError, ValueError):
                continue
            with self.bloqueo:
                self.trades_recibidos += 1
                fila = self.minuto[moneda]
                fila[2 if compra else 3] += usd
                if usd >= umbral:
                    fila[0 if compra else 1] += usd
                if usd >= enorme:
                    g = {"t": int(tr.get("time", time.time() * 1000) / 1000), "moneda": moneda,
                         "lado": "B" if compra else "A", "usd": round(usd), "cuentas": tr.get("users") or [],
                         "px": tr.get("px"), "tid": tr.get("tid")}
                    self.grandes.append(g)
                    if self.observador_grandes:
                        try:
                            self.observador_grandes(dict(g))
                        except Exception:  # noqa: BLE001
                            pass

    def suscribir(self, ws_info, monedas):
        """Mantiene una suscripción 'trades' por moneda vigilada (se ajusta si cambia el universo)."""
        if ws_info is None or not self.cfg.get("grabar_operaciones", True):
            return
        try:
            for m in list(self.subs):
                if m not in monedas:
                    ws_info.unsubscribe({"type": "trades", "coin": m}, self.subs.pop(m))
            for m in monedas:
                if m not in self.subs:
                    self.subs[m] = ws_info.subscribe({"type": "trades", "coin": m}, self.al_recibir_trades)
        except Exception as e:  # noqa: BLE001
            self.log.warning("No pude suscribirme a las operaciones del mercado: %s", e)

    # ---------------------------------------------------------------- lecturas
    def _hace(self, serie, segundos, ahora):
        """Último registro de 'serie' con t <= ahora - segundos."""
        objetivo = ahora - segundos
        previo = None
        for x in serie:
            if x[0] <= objetivo:
                previo = x
            else:
                break
        return previo

    def oi_cambio(self, moneda, minutos=60, ahora=None):
        """Cambio del interés abierto en % en los últimos 'minutos' (None si aún no hay datos)."""
        ahora = ahora or time.time()
        serie = self.ctx.get(moneda)
        if not serie:
            return None
        antes = self._hace(serie, minutos * 60, ahora)
        if not antes or not antes[1] or ahora - serie[-1][0] > 180:
            return None
        return (serie[-1][1] / antes[1] - 1) * 100

    def precio_cambio(self, moneda, minutos=60, ahora=None):
        ahora = ahora or time.time()
        serie = self.ctx.get(moneda)
        if not serie:
            return None
        antes = self._hace(serie, minutos * 60, ahora)
        if not antes or not antes[2]:
            return None
        return (serie[-1][2] / antes[2] - 1) * 100

    def flujo_grandes(self, moneda, lado, minutos=60, ahora=None):
        """Compras menos ventas de operaciones grandes (USD) en los últimos minutos, a favor de 'lado'."""
        ahora = ahora or time.time()
        s = 1 if lado == "L" else -1
        neto = sum(cg - vg for t, cg, vg in self.flujo.get(moneda, ()) if t >= ahora - minutos * 60)
        return s * neto

    def rasgos(self, moneda, lado):
        oi = self.oi_cambio(moneda)
        return {"oi_cambio_1h": round(oi, 3) if oi is not None else 0.0,
                "flujo_grandes": round(self.flujo_grandes(moneda, lado) / 1e5, 3)}

    # ---------------------------------------------------------------- señal
    def senales_liquidaciones(self, monedas, contexto_1h):
        """Cascadas de liquidaciones: el precio se mueve mucho en una hora y el OI cae de golpe (se han cerrado
        posiciones a la fuerza). Se entra en contra del movimiento, esperando el rebote.
        contexto_1h(moneda) -> Contexto de velas de 1 h (para el ATR)."""
        cfg = self.cfg.get("liquidaciones", {})
        if not cfg.get("activo", True):
            return []
        ahora, out = time.time(), []
        for m in monedas:
            if ahora - self.ultima_senal.get(m, 0) < cfg.get("espera_horas", 4) * 3600:
                continue
            oi, mov = self.oi_cambio(m, cfg.get("ventana_min", 60)), self.precio_cambio(m, cfg.get("ventana_min", 60))
            if oi is None or mov is None or oi > -cfg.get("caida_oi_pct", 3.0):
                continue
            try:
                ctx = contexto_1h(m)
            except Exception:  # noqa: BLE001
                ctx = None
            if ctx is None or not ctx.atr[-1]:
                continue
            atr_pct = ctx.atr[-1] / ctx.c[-1] * 100
            if abs(mov) < cfg.get("movimiento_atr", 2.0) * atr_pct:
                continue
            # Corroboración: una caída de OI con movimiento fuerte NO prueba que hubiera liquidaciones. Se exige
            # además flujo agresivo de operaciones grandes en la dirección del movimiento (ventas si cae).
            flujo_mov = self.flujo_grandes(m, "S" if mov < 0 else "L", cfg.get("ventana_min", 60), ahora)
            corroborada = self.subs and flujo_mov >= cfg.get("corroboracion_usd", 250_000)
            if not corroborada:
                continue
            self.ultima_senal[m] = ahora
            lado = "L" if mov < 0 else "S"
            px = self.ctx[m][-1][2]
            from configuracion import estrategia_activa
            out.append({
                "moneda": m, "lado": lado, "estrategia": "liquidaciones_1h", "modulo": "liquidaciones",
                "version": "1", "parametros": {k: cfg.get(k) for k in ("ventana_min", "caida_oi_pct", "movimiento_atr")},
                "solo_investigacion": not estrategia_activa(self.c, "liquidaciones_1h"),
                "etiqueta": "inferencia (OI y precio; no hay dato directo de liquidaciones)",
                "intervalo": "1h", "sl_pct": cfg.get("sl_atr", 1.5) * atr_pct, "fuerza": round(-oi, 3),
                "motivo": (f"posible cascada de liquidaciones de {'largos' if mov < 0 else 'cortos'} (inferida): precio "
                           f"{mov:+.1f}% y OI {oi:+.1f}% en {cfg.get('ventana_min', 60)} min, con "
                           f"{flujo_mov / 1000:.0f}k USD de flujo agresivo a favor del movimiento"),
                "salida": dict(self.c["salida"], **cfg.get("salida", {"tp1_r": 1.0, "tp_final_r": 2.0, "trailing_r": 0.8})),
                "duracion_min": cfg.get("barras_max", 6) * 60, "validada": False, "precio_senal": px,
                "ctx": ctx,
            })
        return out

    def resumen(self, monedas):
        filas = []
        for m in monedas:
            oi, pc = self.oi_cambio(m), self.precio_cambio(m)
            serie = self.ctx.get(m)
            filas.append({"moneda": m, "oi_usd": round(serie[-1][1]) if serie else None,
                          "oi_cambio_1h": round(oi, 2) if oi is not None else None,
                          "precio_cambio_1h": round(pc, 2) if pc is not None else None,
                          "flujo_grandes_1h": round(self.flujo_grandes(m, "L"))})
        filas.sort(key=lambda x: -abs(x["oi_cambio_1h"] or 0))
        dias = 0
        try:
            dias = len({f.rsplit("_", 1)[1][:10] for f in os.listdir(self.carpeta) if f.startswith("ctx_")})
        except OSError:
            pass
        return {"monedas": filas, "dias_grabados": dias, "operaciones_recibidas": self.trades_recibidos,
                "suscripciones": len(self.subs)}


def cargar_grabado(carpeta, prefijo="ctx"):
    """Para el laboratorio: lee lo grabado. Devuelve {moneda: [(t, oi_usd, precio, funding)]} (ctx)
    o {moneda: [(t, compra_grande, venta_grande, compra, venta)]} (flujo)."""
    out = defaultdict(list)
    try:
        nombres = sorted(f for f in os.listdir(carpeta) if f.startswith(prefijo + "_") and f.endswith(".jsonl"))
    except OSError:
        return {}
    for f in nombres:
        try:
            with open(os.path.join(carpeta, f), encoding="utf-8") as fh:
                for linea in fh:
                    try:
                        x = json.loads(linea)
                    except ValueError:
                        continue
                    for m, v in x["d"].items():
                        out[m].append((x["t"], *v))
        except OSError:
            continue
    return {m: sorted(v) for m, v in out.items()}


def dias_grabados(serie):
    if not serie:
        return 0.0
    return (serie[-1][0] - serie[0][0]) / 86400
