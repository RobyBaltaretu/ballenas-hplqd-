"""
Registro (ledger) de operaciones: archivo JSONL, una línea por evento, solo se añade (nunca se reescribe).

Es la fuente CANÓNICA de lo que ha hecho el bot con cada operación. El log (bot.log) es diagnóstico.

  - Cada operación tiene un id estable (trade_id). Cada evento lleva: id propio, versión del esquema,
    hora UTC ISO-8601, tipo, trade_id, origen (real / simulado / shadow / backtest), mercado,
    estrategia y versión, y los datos del evento (órdenes con oid/cloid, fills, comisiones, stops...).
  - Escritura segura: se añade la línea completa y se fuerza a disco (fsync) bajo un candado.
  - Rotación: al pasar de 'max_bytes' el archivo se archiva como ledger-AAAAMMDDTHHMMSSZ.jsonl y se empieza
    uno nuevo. Al leer se recorren todos (archivados + actual) en orden.
  - Recuperación: una línea corrupta (p. ej. corte de luz a mitad de escritura) se salta, se cuenta y se
    informa; nunca se inventa su contenido.
  - Lo que no se sabe se guarda como None ("desconocido"), nunca se deduce desde el log.

Los valores modelados (fills hipotéticos de shadow, comisiones estimadas) llevan "estimado": true.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone

VERSION_ESQUEMA = 1
ORIGENES = ("real", "simulado", "shadow", "backtest")

TIPOS = {
    "senal": "señal detectada por una estrategia",
    "decision": "aprobada o rechazada, con motivos y parámetros efectivos",
    "orden_enviada": "orden enviada al exchange (antes de conocer la respuesta)",
    "orden_estado": "cambio de estado de una orden",
    "fill": "ejecución (total o parcial) de una orden",
    "proteccion": "stop o take profit: colocado, verificado, ausente, fallo",
    "trailing": "movimiento del stop (sin pérdidas o dinámico)",
    "funding": "pago o cobro de funding atribuido a la operación",
    "salida_solicitada": "el bot pide cerrar (tiempo, emergencia, manual...)",
    "cierre": "operación cerrada, con resultado",
    "error": "error al operar",
    "conciliacion": "comparación del registro con el exchange",
    "externa": "posición u orden que el bot no puede atribuirse",
    "alerta": "alerta operativa ligada a la operación",
    "shadow_intencion": "lo que el bot HABRÍA hecho (modo shadow, sin órdenes)",
    "shadow_resultado": "resultado MODELADO de una intención shadow",
}

ESTADOS_OPERACION = ("senal", "aprobada", "rechazada", "enviada", "abierta", "parcial", "completada",
                     "cerrando", "cerrada", "error", "desconocido")
ESTADOS_ORDEN = ("creada", "enviada", "abierta", "parcial", "completada", "cancelacion_solicitada", "cancelada",
                 "rechazada", "disparada", "desconocida", "no_confirmada")


def ahora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def nuevo_trade_id():
    return "op-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


class ErrorLedger(Exception):
    pass


class Ledger:
    def __init__(self, carpeta, max_bytes=20_000_000, nombre="ledger.jsonl"):
        self.carpeta = carpeta
        os.makedirs(carpeta, exist_ok=True)
        self.ruta = os.path.join(carpeta, nombre)
        self.max_bytes = max_bytes
        self.bloqueo = threading.Lock()
        self.ultimo_error = None
        self.observador = None          # función(evento) opcional (feed del panel). Nunca rompe el ledger.

    # ------------------------------------------------------------ escritura
    def registrar(self, tipo, trade_id=None, origen=None, /, **datos):
        if tipo not in TIPOS:
            raise ErrorLedger(f"tipo de evento desconocido: {tipo}")
        if origen is not None and origen not in ORIGENES:
            raise ErrorLedger(f"origen desconocido: {origen}")
        evento = {"v": VERSION_ESQUEMA, "id": uuid.uuid4().hex[:12], "ts": ahora_iso(), "tipo": tipo,
                  "trade_id": trade_id, "origen": origen}
        for k, v in datos.items():
            if k in evento:
                raise ErrorLedger(f"campo reservado: {k}")
            evento[k] = v
        linea = json.dumps(evento, ensure_ascii=False, separators=(",", ":"), default=str)
        if "\n" in linea:
            raise ErrorLedger("un evento no puede contener saltos de línea sin escapar")
        with self.bloqueo:
            self._rotar_si_hace_falta()
            with open(self.ruta, "a", encoding="utf-8") as fh:
                fh.write(linea + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        if self.observador is not None:
            try:
                self.observador(evento)
            except Exception as e:  # noqa: BLE001  (el feed es secundario: el ledger es lo canónico)
                self.ultimo_error = f"observador: {e}"
        return evento

    def _rotar_si_hace_falta(self):
        try:
            if os.path.getsize(self.ruta) < self.max_bytes:
                return
        except OSError:
            return
        marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        os.replace(self.ruta, os.path.join(self.carpeta, f"ledger-{marca}.jsonl"))

    # ------------------------------------------------------------ lectura
    def archivos(self):
        archivados = sorted(f for f in os.listdir(self.carpeta) if f.startswith("ledger-") and f.endswith(".jsonl"))
        out = [os.path.join(self.carpeta, f) for f in archivados]
        if os.path.exists(self.ruta):
            out.append(self.ruta)
        return out

    def leer(self, desde_iso=None):
        """Devuelve (eventos, problemas). problemas = [{"archivo", "linea", "motivo"}] de líneas ilegibles."""
        eventos, problemas = [], []
        for ruta in self.archivos():
            try:
                with open(ruta, encoding="utf-8", errors="replace") as fh:
                    for n, linea in enumerate(fh, 1):
                        linea = linea.strip()
                        if not linea:
                            continue
                        try:
                            ev = json.loads(linea)
                        except ValueError:
                            problemas.append({"archivo": os.path.basename(ruta), "linea": n, "motivo": "JSON corrupto"})
                            continue
                        if not isinstance(ev, dict) or "tipo" not in ev or "ts" not in ev:
                            problemas.append({"archivo": os.path.basename(ruta), "linea": n, "motivo": "evento incompleto"})
                            continue
                        ev = migrar_evento(ev)
                        if desde_iso and ev["ts"] < desde_iso:
                            continue
                        eventos.append(ev)
            except OSError as e:
                problemas.append({"archivo": os.path.basename(ruta), "linea": None, "motivo": str(e)})
        eventos.sort(key=lambda e: e["ts"])   # orden temporal aunque haya varios archivos
        return eventos, problemas

    def operaciones(self):
        """{trade_id: resumen} reconstruido SOLO a partir de los eventos registrados."""
        eventos, problemas = self.leer()
        ops = {}
        for ev in eventos:
            tid = ev.get("trade_id")
            if not tid:
                continue
            op = ops.setdefault(tid, nueva_operacion(tid))
            aplicar(op, ev)
        for op in ops.values():
            op.pop("_ids", None)
            cerrar_resumen(op)
        return ops, problemas


def migrar_evento(ev):
    """Eventos de versiones anteriores del esquema -> versión actual (hoy solo hay la 1)."""
    ev.setdefault("v", 1)
    ev.setdefault("origen", None)
    return ev


# ============================================================ reconstrucción de una operación
def nueva_operacion(tid):
    return {"trade_id": tid, "origen": None, "moneda": None, "lado": None, "estrategia": None, "version": None,
            "estado": "senal", "eventos": [], "ordenes": {}, "fills_entrada": [], "fills_salida": [],
            "funding": [], "comisiones": 0.0, "comisiones_estimadas": False, "pnl_bruto": None,
            "funding_total": 0.0, "causa_salida": None, "abierta_ts": None, "cerrada_ts": None,
            "riesgo_inicial_usd": None, "stop_inicial": None, "stop_actual": None, "tp_inicial": None,
            "trailing": [], "motivo": None, "contexto": None, "parametros": None, "decision": None,
            "alertas": [], "precio_referencia": None}


def _repetido(op, ev):
    """Idempotencia al reconstruir: un fill, un pago de funding o un cierre con el mismo id (registrado dos veces,
    p. ej. por una caída a mitad del cierre) solo cuenta una vez para comisiones, PnL y funding."""
    for campo in ("fill_id", "funding_id", "cierre_id"):
        v = ev.get(campo)
        if v:
            vistos = op.setdefault("_ids", set())
            if (campo, v) in vistos:
                return True
            vistos.add((campo, v))
    return False


def aplicar(op, ev):
    t = ev["tipo"]
    if _repetido(op, ev):
        op.setdefault("eventos_repetidos", 0)
        op["eventos_repetidos"] += 1
        return
    op["eventos"].append(ev)
    for k in ("origen", "moneda", "lado", "estrategia", "version"):
        if ev.get(k) is not None and op.get(k) is None:
            op[k] = ev[k]
    if t == "senal":
        op["motivo"] = ev.get("motivo")
        op["contexto"] = ev.get("contexto")
        op["precio_referencia"] = ev.get("precio")
    elif t == "decision":
        op["decision"] = {"aprobada": ev.get("aprobada"), "motivos": ev.get("motivos"), "plan": ev.get("plan")}
        op["parametros"] = ev.get("parametros")
        op["estado"] = "aprobada" if ev.get("aprobada") else "rechazada"
    elif t == "orden_enviada":
        clave = ev.get("cloid") or str(ev.get("oid"))
        op["ordenes"][clave] = {"cloid": ev.get("cloid"), "oid": ev.get("oid"), "rol": ev.get("rol"),
                                "estado": "enviada", "sz": ev.get("sz"), "px": ev.get("px"),
                                "trigger": ev.get("trigger"), "tipo_orden": ev.get("tipo_orden"),
                                "reduce_only": ev.get("reduce_only"), "historial": [(ev["ts"], "enviada", None)]}
        if ev.get("rol") == "entrada" and op["estado"] in ("senal", "aprobada"):
            op["estado"] = "enviada"
    elif t == "orden_estado":
        clave = ev.get("cloid") or str(ev.get("oid"))
        o = op["ordenes"].setdefault(clave, {"cloid": ev.get("cloid"), "oid": ev.get("oid"), "rol": ev.get("rol"),
                                             "historial": []})
        o["estado"] = ev.get("estado")
        if ev.get("oid") is not None:
            o["oid"] = ev["oid"]
        o["historial"].append((ev["ts"], ev.get("estado"), ev.get("detalle")))
    elif t == "fill":
        f = {"ts": ev["ts"], "px": ev.get("px"), "sz": ev.get("sz"), "comision": ev.get("comision"),
             "pnl_cerrado": ev.get("pnl_cerrado"), "oid": ev.get("oid"), "cloid": ev.get("cloid"),
             "rol": ev.get("rol"), "estimado": bool(ev.get("estimado"))}
        (op["fills_entrada"] if ev.get("rol") == "entrada" else op["fills_salida"]).append(f)
        if ev.get("comision") is not None:
            op["comisiones"] += float(ev["comision"])
        if f["estimado"]:
            op["comisiones_estimadas"] = True
        if ev.get("rol") == "entrada":
            op["estado"] = "parcial" if ev.get("parcial") else "completada"
            op["abierta_ts"] = op["abierta_ts"] or ev["ts"]
    elif t == "proteccion":
        if ev.get("clase") == "stop" and ev.get("accion") in ("colocado", "verificado"):
            op["stop_actual"] = ev.get("trigger")
            if op["stop_inicial"] is None:
                op["stop_inicial"] = ev.get("trigger")
        if ev.get("clase") == "tp" and ev.get("accion") == "colocado" and op["tp_inicial"] is None:
            op["tp_inicial"] = ev.get("px")
        if ev.get("riesgo_usd") is not None and op["riesgo_inicial_usd"] is None:
            op["riesgo_inicial_usd"] = ev.get("riesgo_usd")
    elif t == "trailing":
        op["trailing"].append({"ts": ev["ts"], "de": ev.get("de"), "a": ev.get("a"), "motivo": ev.get("motivo")})
        op["stop_actual"] = ev.get("a")
    elif t == "funding":
        op["funding"].append({"ts": ev["ts"], "usd": ev.get("usd"), "tasa": ev.get("tasa"),
                              "estimado": bool(ev.get("estimado"))})
        if ev.get("usd") is not None:
            op["funding_total"] += float(ev["usd"])
    elif t == "salida_solicitada":
        op["estado"] = "cerrando"
        op["causa_salida"] = ev.get("causa")
    elif t == "cierre":
        op["estado"] = "cerrada"
        op["cerrada_ts"] = ev["ts"]
        op["causa_salida"] = ev.get("causa") or op["causa_salida"]
        for k in ("pnl_bruto", "riesgo_inicial_usd"):
            if ev.get(k) is not None:
                op[k] = ev[k]
    elif t == "error":
        op["alertas"].append({"ts": ev["ts"], "severidad": "error", "mensaje": ev.get("mensaje")})
        if ev.get("estado"):
            op["estado"] = ev["estado"]
    elif t == "alerta":
        op["alertas"].append({"ts": ev["ts"], "severidad": ev.get("severidad", "aviso"), "mensaje": ev.get("mensaje")})
    elif t == "shadow_intencion":
        op["estado"] = "shadow"
        op["motivo"] = ev.get("motivo")
        op["contexto"] = ev.get("contexto")
    elif t == "shadow_resultado":
        op["estado"] = "shadow_cerrada"
        op["causa_salida"] = ev.get("causa")
        op["r_modelado"] = ev.get("r")


def _media(fills):
    tot = sum(float(f["sz"]) for f in fills if f.get("sz") is not None and f.get("px") is not None)
    if not tot:
        return None, 0.0
    return sum(float(f["px"]) * float(f["sz"]) for f in fills if f.get("sz") is not None and f.get("px") is not None) / tot, tot


def cerrar_resumen(op):
    """Métricas derivadas de los eventos. Lo que no se pueda calcular queda en None (desconocido).
    Fórmulas:
      pnl_bruto   = suma de 'pnl_cerrado' de los fills de salida (si el exchange lo da) o
                    (precio medio salida − precio medio entrada) × tamaño × dirección
      pnl_neto    = pnl_bruto − comisiones + funding (funding con su signo: positivo = cobrado)
      R bruto     = pnl_bruto / riesgo_inicial_usd ;  R neto = pnl_neto / riesgo_inicial_usd
      riesgo_inicial_usd = |entrada − stop inicial| × tamaño ejecutado (sin costes)"""
    pe, te = _media(op["fills_entrada"])
    ps, ts_ = _media(op["fills_salida"])
    op["precio_medio_entrada"], op["tam_entrada"] = pe, te
    op["precio_medio_salida"], op["tam_salida"] = ps, ts_
    op["tam_restante"] = round(te - ts_, 12) if te else None
    if op["pnl_bruto"] is None and op["fills_salida"]:
        cerr = [f.get("pnl_cerrado") for f in op["fills_salida"]]
        if all(x is not None for x in cerr):
            op["pnl_bruto"] = sum(float(x) for x in cerr)
        elif pe is not None and ps is not None and op["lado"]:
            s = 1 if op["lado"] == "L" else -1
            op["pnl_bruto"] = (ps - pe) * ts_ * s
    if op["riesgo_inicial_usd"] is None and pe is not None and op["stop_inicial"] is not None and te:
        op["riesgo_inicial_usd"] = abs(pe - float(op["stop_inicial"])) * te
    op["pnl_neto"] = None if op["pnl_bruto"] is None else op["pnl_bruto"] - op["comisiones"] + op["funding_total"]
    rr = op["riesgo_inicial_usd"]
    op["r_bruto"] = op["pnl_bruto"] / rr if rr and op["pnl_bruto"] is not None else None
    op["r_neto"] = op["pnl_neto"] / rr if rr and op["pnl_neto"] is not None else None
    if pe is not None and op.get("precio_referencia") and op["lado"]:
        s = 1 if op["lado"] == "L" else -1
        op["deslizamiento_entrada_pct"] = s * (pe - float(op["precio_referencia"])) / float(op["precio_referencia"]) * 100
    else:
        op["deslizamiento_entrada_pct"] = None
    return op
