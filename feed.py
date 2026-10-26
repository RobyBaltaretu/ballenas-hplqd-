"""
Feed de «Señales en vivo»: flujo PERSISTENTE (append-only) de eventos auditables para el panel.

No es una fuente de predicciones: muestra lo que el bot detecta, decide y hace, y lo que observa del mercado y de
las ballenas, con la procedencia y la frescura de cada dato. Lo que no se sabe se guarda como None y el panel lo
muestra como «desconocido» (nunca se infiere).

Archivo: registro/feed.jsonl (rota al pasar de feed.max_mb; se borran los archivados de más de feed.dias_retencion).
Cada evento:
  seq            número de secuencia creciente (cursor para la API incremental: sin pérdidas, sin duplicados)
  event_id       id estable (el del evento del ledger si viene de él)
  tipo           ver TIPOS
  ts_exchange    hora del exchange (s, si el dato la trae), ts_recepcion (cuándo llegó al bot), ts_proceso
  moneda, lado, estrategia, version, fuente, trade_id
  precio_ref     {"valor", "tipo": mid | mark | oraculo | fill | trade | senal}
  tam, riesgo_usd, frescura_seg, estado, detalle (texto), datos (extra)
Las direcciones de cuentas se ENMASCARAN y los secretos se ocultan antes de escribir.

Origen de cada tipo:
  - Del ledger (lo que hace el bot): señal_detectada, decision_aprobada / decision_rechazada (con cada motivo),
    orden_* (enviada, abierta=resting, parcial, completada, cancelada, rechazada, desconocida, no_confirmada),
    proteccion (stop/TP colocado, verificado, ausente, fallo, duplicado), stop_movido, entrada, salida, cierre,
    intencion_shadow, resultado_shadow, alerta, error.
  - Del mercado y las ballenas: ballena_fill (fill OBSERVADO por websocket, evento bruto normalizado),
    ballena_cambio_inferido (diferencia entre dos fotos de posición: NO es un fill), operacion_grande (trade
    público agresivo: NO revela la posición neta de nadie), liquidacion_inferida (inferencia por precio/OI, NO
    confirmada), datos_obsoletos, reconexion, fuente_perdida, fuente_recuperada.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

TIPOS = {
    "senal_detectada": "señal detectada por una estrategia",
    "decision_aprobada": "señal aprobada por los filtros y el riesgo",
    "decision_rechazada": "señal rechazada (con cada motivo)",
    "orden_enviada": "orden enviada (antes de conocer la respuesta)",
    "orden_abierta": "orden en el libro (resting)",
    "orden_parcial": "orden parcialmente ejecutada",
    "orden_completada": "orden ejecutada",
    "orden_cancelada": "orden cancelada (confirmado)",
    "orden_rechazada": "orden rechazada",
    "orden_desconocida": "estado de la orden desconocido (se concilia, no se reenvía)",
    "orden_no_confirmada": "el exchange no la conoce (se concilia, no se reenvía)",
    "orden_cancelacion_solicitada": "cancelación pedida (aún sin confirmar)",
    "orden_disparada": "orden trigger activada",
    "proteccion": "stop / objetivo: colocado, verificado, ausente o fallo",
    "stop_movido": "cambio de stop (sin pérdidas o dinámico)",
    "entrada": "fill de entrada",
    "salida": "fill de salida",
    "cierre": "operación cerrada (con resultado)",
    "intencion_shadow": "lo que el bot HABRÍA hecho (shadow, sin órdenes)",
    "resultado_shadow": "resultado MODELADO de una intención shadow",
    "alerta": "alerta operativa",
    "error": "error al operar",
    "ballena_fill": "fill de una ballena OBSERVADO por websocket",
    "ballena_cambio_inferido": "cambio de posición de una ballena INFERIDO (diferencia entre fotos)",
    "operacion_grande": "operación pública agresiva grande (no revela posiciones netas)",
    "liquidacion_inferida": "posible liquidación INFERIDA (precio/OI; no confirmada)",
    "datos_obsoletos": "datos viejos o no disponibles",
    "reconexion": "reconexión de una fuente",
    "fuente_perdida": "se ha perdido una fuente de datos",
    "fuente_recuperada": "una fuente de datos vuelve a responder",
}

DESDE_LEDGER = {"senal": "senal_detectada", "orden_enviada": "orden_enviada", "trailing": "stop_movido",
                "cierre": "cierre", "shadow_intencion": "intencion_shadow", "shadow_resultado": "resultado_shadow",
                "alerta": "alerta", "error": "error", "proteccion": "proteccion"}


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


_DIRECCION = None


def _limpiar(obj):
    """Oculta secretos y enmascara direcciones (0x + 40 hex) en TODOS los textos del evento (recursivo)."""
    import re

    from observabilidad import ocultar_secretos
    global _DIRECCION
    if _DIRECCION is None:
        _DIRECCION = re.compile(r"0x([0-9a-fA-F]{4})[0-9a-fA-F]{32}([0-9a-fA-F]{4})(?![0-9a-fA-F])")
    if isinstance(obj, str):
        return _DIRECCION.sub(r"0x\1…\2", ocultar_secretos(obj))
    if isinstance(obj, dict):
        return {k: _limpiar(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_limpiar(v) for v in obj]
    return obj


class Feed:
    def __init__(self, carpeta, cfg=None, nombre="feed.jsonl"):
        cfg = cfg or {}
        self.carpeta = carpeta
        os.makedirs(carpeta, exist_ok=True)
        self.ruta = os.path.join(carpeta, nombre)
        self.max_bytes = int(cfg.get("max_mb", 20) * 1_000_000)
        self.dias = cfg.get("dias_retencion", 14)
        self.activo = cfg.get("activo", True)
        self.bloqueo = threading.Lock()
        self.seq = self._ultimo_seq()
        self.ultima_limpieza = 0.0
        self.escritos = 0
        self._por_trade = {}

    # ------------------------------------------------------------ escritura
    def archivos(self):
        try:
            arch = sorted(f for f in os.listdir(self.carpeta) if f.startswith("feed-") and f.endswith(".jsonl"))
        except OSError:
            arch = []
        out = [os.path.join(self.carpeta, f) for f in arch]
        if os.path.exists(self.ruta):
            out.append(self.ruta)
        return out

    def _ultimo_seq(self):
        for ruta in reversed(self.archivos()):
            try:
                with open(ruta, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    tam = fh.tell()
                    fh.seek(max(0, tam - 65536))
                    lineas = fh.read().decode("utf-8", "replace").splitlines()
                for linea in reversed(lineas):
                    try:
                        return int(json.loads(linea)["seq"])
                    except (ValueError, KeyError, TypeError):
                        continue
            except OSError:
                continue
        return 0

    def publicar(self, tipo, *, event_id=None, ts_exchange=None, ts_recepcion=None, moneda=None, lado=None,
                 estrategia=None, version=None, fuente=None, trade_id=None, precio=None, tipo_precio=None, tam=None,
                 riesgo_usd=None, frescura_seg=None, estado=None, detalle=None, datos=None):
        if not self.activo:
            return None
        if tipo not in TIPOS:
            raise ValueError(f"tipo de evento de feed desconocido: {tipo}")
        ahora = time.time()
        ev = {"tipo": tipo, "event_id": event_id or uuid.uuid4().hex[:12],
              "ts_exchange": ts_exchange, "ts_recepcion": ts_recepcion if ts_recepcion is not None else ahora,
              "ts_proceso": ahora, "moneda": moneda, "lado": lado, "estrategia": estrategia, "version": version,
              "fuente": fuente, "trade_id": trade_id,
              "precio_ref": {"valor": precio, "tipo": tipo_precio} if precio is not None else None,
              "tam": tam, "riesgo_usd": riesgo_usd, "frescura_seg": frescura_seg, "estado": estado,
              "detalle": detalle, "datos": datos}
        ev = _limpiar(ev)
        with self.bloqueo:
            self.seq += 1
            ev = {"seq": self.seq, **ev}
            linea = json.dumps(ev, ensure_ascii=False, separators=(",", ":"), default=str)
            self._rotar()
            try:
                with open(self.ruta, "a", encoding="utf-8") as fh:
                    fh.write(linea + "\n")
            except OSError:
                return None
            self.escritos += 1
        if ahora - self.ultima_limpieza > 3600:
            self.limpiar(ahora)
        return ev

    def _rotar(self):
        try:
            if os.path.getsize(self.ruta) < self.max_bytes:
                return
        except OSError:
            return
        marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        os.replace(self.ruta, os.path.join(self.carpeta, f"feed-{marca}.jsonl"))

    def limpiar(self, ahora=None):
        """Borra los archivos rotados más viejos que la retención (el actual nunca)."""
        ahora = ahora or time.time()
        self.ultima_limpieza = ahora
        for ruta in self.archivos()[:-1]:
            try:
                if ahora - os.path.getmtime(ruta) > self.dias * 86400:
                    os.remove(ruta)
            except OSError:
                pass

    # ------------------------------------------------------------ desde el ledger
    def desde_ledger(self, ev):
        """Observador del ledger: convierte sus eventos (lo que hace el bot) en eventos del feed."""
        t = ev.get("tipo")
        # El ejecutor no repite lado/estrategia en cada orden: se completan con lo que ya se sabe de ESA operación
        # (mismo trade_id). Nunca se inventan: si no se conocen, quedan como desconocidos.
        tid = ev.get("trade_id")
        conocido = self._por_trade.get(tid, {}) if tid else {}
        if tid:
            for k in ("lado", "estrategia", "version"):
                if ev.get(k) is not None and ev.get(k) not in ("compra", "venta"):
                    conocido[k] = ev[k]
            self._por_trade[tid] = conocido
            while len(self._por_trade) > 2000:
                self._por_trade.pop(next(iter(self._por_trade)))
        lado = ev.get("lado") if ev.get("lado") in ("L", "S") else conocido.get("lado", ev.get("lado"))
        base = {"event_id": ev.get("id"), "moneda": ev.get("moneda"), "lado": lado,
                "estrategia": ev.get("estrategia") or conocido.get("estrategia"),
                "version": ev.get("version") or conocido.get("version"), "trade_id": tid,
                "fuente": f"bot ({ev.get('origen') or 'desconocido'})", "ts_recepcion": _ts(ev.get("ts"))}
        if t == "decision":
            plan = ev.get("plan") or {}
            return self.publicar("decision_aprobada" if ev.get("aprobada") else "decision_rechazada", **base,
                                 tam=plan.get("tam"), riesgo_usd=plan.get("riesgo_usd"),
                                 estado="aprobada" if ev.get("aprobada") else "rechazada",
                                 detalle="; ".join(ev.get("motivos") or []) or None,
                                 datos={"motivos": ev.get("motivos") or [], "impacto_pct": plan.get("impacto_pct")})
        if t == "orden_estado":
            estado = ev.get("estado") or "desconocida"
            tipo = f"orden_{estado}" if f"orden_{estado}" in TIPOS else "orden_desconocida"
            # 'tam' = lo EJECUTADO (solo tiene sentido si hubo ejecución); en una orden en el libro sería 0 y se
            # leería como "tamaño 0": se deja desconocido y lo ejecutado va en 'datos'.
            return self.publicar(tipo, **base, estado=estado,
                                 tam=ev.get("ejecutado") if estado in ("parcial", "completada") else None,
                                 precio=ev.get("px_medio"), tipo_precio="fill" if ev.get("px_medio") else None,
                                 detalle=ev.get("detalle"), datos={"rol": ev.get("rol"), "cloid": ev.get("cloid"),
                                                                   "oid": ev.get("oid"), "ejecutado": ev.get("ejecutado")})
        if t == "fill":
            return self.publicar("entrada" if ev.get("rol") == "entrada" else "salida", **dict(
                base, ts_recepcion=base["ts_recepcion"]), ts_exchange=(ev.get("ts_exchange") or 0) / 1000 or None,
                precio=ev.get("px"), tipo_precio="fill", tam=ev.get("sz"), estado="estimado" if ev.get("estimado") else "real",
                datos={"rol": ev.get("rol"), "comision": ev.get("comision"), "maker": ev.get("maker"),
                       "fill_id": ev.get("fill_id")})
        if t == "senal":
            ctx = ev.get("contexto") or {}
            return self.publicar("senal_detectada", **base, precio=ev.get("precio"), tipo_precio="senal",
                                 estado="detectada", detalle=ev.get("motivo"),
                                 datos={"sl_pct": ev.get("sl_pct"), "intervalo": ctx.get("intervalo"),
                                        "vela_ts": ctx.get("vela_ts"), "etiqueta": ctx.get("etiqueta"),
                                        "mid": ctx.get("mid"), "mark": ctx.get("mark")})
        if t == "orden_enviada":
            return self.publicar("orden_enviada", **base, precio=ev.get("trigger") or ev.get("px"),
                                 tipo_precio="mark" if ev.get("trigger") else "limite", tam=ev.get("sz"), estado="enviada",
                                 datos={"rol": ev.get("rol"), "cloid": ev.get("cloid"), "reduce_only": ev.get("reduce_only"),
                                        "tipo_orden": ev.get("tipo_orden")})
        if t == "proteccion":
            return self.publicar("proteccion", **base, precio=ev.get("trigger") or ev.get("px"),
                                 tipo_precio=ev.get("precio_referencia") or ("mark" if ev.get("clase") == "stop" else "limite"),
                                 tam=ev.get("sz"), estado=ev.get("accion"), detalle=ev.get("motivo") or ev.get("detalle"),
                                 datos={"clase": ev.get("clase"), "rol": ev.get("rol")})
        if t == "trailing":
            return self.publicar("stop_movido", **base, precio=ev.get("a"), tipo_precio="mark", estado="movido",
                                 detalle=ev.get("motivo"), datos={"de": ev.get("de"), "a": ev.get("a")})
        if t == "cierre":
            return self.publicar("cierre", **base, estado="cerrada", detalle=ev.get("causa"),
                                 precio=ev.get("precio_medio_salida"), tipo_precio="fill" if ev.get("precio_medio_salida") else None,
                                 datos={"pnl_neto": ev.get("pnl_neto"), "r_neto": ev.get("r_neto"),
                                        "comisiones": ev.get("comisiones"), "funding": ev.get("funding")})
        if t in ("shadow_intencion", "shadow_resultado"):
            mod = ev.get("modelado") or {}
            return self.publicar(DESDE_LEDGER[t], **base, precio=mod.get("fill"), tipo_precio="modelado" if mod else None,
                                 tam=mod.get("tam"), riesgo_usd=mod.get("riesgo_usd"),
                                 estado="modelado", detalle=ev.get("motivo") or ev.get("causa"),
                                 datos={"r": ev.get("r"), "estimado": True})
        if t in ("alerta", "error"):
            return self.publicar(t, **base, estado=ev.get("severidad") or "error", detalle=ev.get("mensaje"))
        return None


# ================================================================ lectura incremental (la usa el panel)
class LectorFeed:
    """Lee el feed de forma INCREMENTAL (por inodo + desplazamiento, así sobrevive a la rotación) y guarda en
    memoria los últimos 'max_eventos'. consulta(desde_seq) devuelve los eventos con seq > desde_seq, en orden,
    sin duplicados."""

    def __init__(self, carpeta, max_eventos=20000):
        self.carpeta = carpeta
        self.max = max_eventos
        self.eventos = []
        self.offsets = {}          # inodo -> bytes leídos
        self.bloqueo = threading.Lock()
        self.ultimo_ts_lectura = 0.0

    def _archivos(self):
        try:
            arch = sorted(f for f in os.listdir(self.carpeta) if f.startswith("feed-") and f.endswith(".jsonl"))
        except OSError:
            return []
        out = [os.path.join(self.carpeta, f) for f in arch]
        actual = os.path.join(self.carpeta, "feed.jsonl")
        if os.path.exists(actual):
            out.append(actual)
        return out

    def actualizar(self):
        with self.bloqueo:
            nuevos = []
            vivos = set()
            for ruta in self._archivos():
                try:
                    st = os.stat(ruta)
                except OSError:
                    continue
                clave = (st.st_dev, st.st_ino) if st.st_ino else ruta
                vivos.add(clave)
                leido = self.offsets.get(clave, 0)
                if st.st_size < leido:
                    leido = 0                  # se truncó o es otro archivo: se relee (los duplicados se filtran)
                if st.st_size == leido:
                    continue
                try:
                    with open(ruta, "rb") as fh:
                        fh.seek(leido)
                        bloque = fh.read()
                except OSError:
                    continue
                fin = bloque.rfind(b"\n") + 1  # solo líneas completas (la última puede estar a medio escribir)
                for linea in bloque[:fin].decode("utf-8", "replace").splitlines():
                    try:
                        ev = json.loads(linea)
                        int(ev["seq"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    nuevos.append(ev)
                self.offsets[clave] = leido + fin
            for k in list(self.offsets):
                if k not in vivos:
                    self.offsets.pop(k)
            if nuevos:
                ultimo = self.eventos[-1]["seq"] if self.eventos else 0
                vistos = {e["seq"] for e in self.eventos[-5000:]}
                nuevos = sorted((e for e in nuevos if e["seq"] not in vistos), key=lambda e: e["seq"])
                if nuevos and nuevos[0]["seq"] < ultimo:
                    self.eventos = sorted(self.eventos + nuevos, key=lambda e: e["seq"])
                else:
                    self.eventos.extend(nuevos)
                del self.eventos[:-self.max]
            self.ultimo_ts_lectura = time.time()

    def consulta(self, desde_seq=0, limite=200, filtro=None):
        """Eventos con seq > desde_seq que cumplen el filtro (en orden de seq). Si desde_seq < 0, los ÚLTIMOS
        'limite' (carga inicial). Devuelve (eventos, ultimo_seq_existente, hay_mas)."""
        self.actualizar()
        with self.bloqueo:
            evs = self.eventos
            ultimo = evs[-1]["seq"] if evs else 0
            primero = evs[0]["seq"] if evs else 0
        f = filtro or (lambda e: True)
        if desde_seq is None or desde_seq < 0:
            sel = [e for e in evs if f(e)][-limite:]
            return sel, ultimo, primero, False
        sel = [e for e in evs if e["seq"] > desde_seq and f(e)]
        return sel[:limite], ultimo, primero, len(sel) > limite


def filtro_desde_parametros(q):
    """q: dict de listas (parse_qs). Filtros por tipo (varios, separados por comas), moneda, estrategia, lado,
    estado y texto libre."""
    def uno(k):
        v = (q.get(k) or [""])[0].strip()
        return v or None
    tipos = {t for t in (uno("tipo") or "").split(",") if t}
    moneda, estrategia, lado, estado, texto = uno("moneda"), uno("estrategia"), uno("lado"), uno("estado"), uno("q")
    texto = texto.lower() if texto else None

    def f(e):
        if tipos and e.get("tipo") not in tipos:
            return False
        if moneda and (e.get("moneda") or "").upper() != moneda.upper():
            return False
        if estrategia and e.get("estrategia") != estrategia:
            return False
        if lado and e.get("lado") != lado:
            return False
        if estado and e.get("estado") != estado:
            return False
        if texto and texto not in json.dumps(e, ensure_ascii=False).lower():
            return False
        return True
    return f
