"""
Ciclo de vida de las órdenes. ÚNICO punto por el que el bot envía, cancela o consulta órdenes.

Reglas (las mismas en real y en simulado; en shadow no existe ejecutor, ver EjecutorShadow):
  1. Cada orden lleva un CLOID único (id de cliente, 16 bytes hex) generado ANTES de enviarla y registrado en
     el ledger antes de la petición. Hyperliquid rechaza un CLOID repetido, así que reenviar la misma orden
     nunca la duplica.
  2. Respuesta clara (resting / filled / error) -> estado directo.
     Excepción, timeout o respuesta ilegible -> estado AMBIGUO: se consulta orderStatus por CLOID (con
     reintentos y espera creciente). NUNCA se reenvía la orden. Si tras las consultas sigue sin saberse,
     el mercado queda CONGELADO (sin entradas nuevas) y se genera una alerta.
  3. Una cancelación no se da por hecha porque la petición respondiera: se comprueba el estado final.
     Si la orden se llenó mientras tanto, se informa como 'completada'. Si no se puede confirmar ->
     'desconocida' + congelar + alerta.
  4. Los errores seguidos ponen el ejecutor en estado DEGRADADO (bloquea entradas nuevas) hasta que
     vuelvan a funcionar varias peticiones seguidas.

Estados de orden (ledger.ESTADOS_ORDEN): creada, enviada, abierta, parcial, completada,
cancelacion_solicitada, cancelada, rechazada, disparada, desconocida, no_confirmada.
Las transiciones permitidas están en TRANSICIONES_ORDEN: un estado final confirmado no vuelve atrás y un estado
incierto (desconocida / no_confirmada) solo se resuelve consultando, nunca reenviando.
"""

import time
import uuid

ESTADOS_HL = {
    "open": "abierta", "filled": "completada", "triggered": "disparada", "rejected": "rechazada",
    "canceled": "cancelada", "marginCanceled": "cancelada", "vaultWithdrawalCanceled": "cancelada",
    "openInterestCapCanceled": "cancelada", "selfTradeCanceled": "cancelada", "reduceOnlyCanceled": "cancelada",
    "siblingFilledCanceled": "cancelada", "delistedCanceled": "cancelada", "liquidatedCanceled": "cancelada",
    "scheduledCancel": "cancelada", "tickRejected": "rechazada", "minTradeNtlRejected": "rechazada",
    "perpMarginRejected": "rechazada", "reduceOnlyRejected": "rechazada", "badAloPxRejected": "rechazada",
    "iocCancelRejected": "rechazada", "badTriggerPxRejected": "rechazada", "marketOrderNoLiquidityRejected": "rechazada",
}
FINALES = ("completada", "cancelada", "rechazada")
INCIERTOS = ("desconocida", "no_confirmada")          # no se sabe si existe / qué pasó: NUNCA se reenvía a ciegas
ACTIVOS = ("abierta", "parcial", "disparada")

# Órdenes que SOLO pueden reducir una posición. Enviarlas sin reduce_only es un fallo del bot: se bloquea.
ROLES_REDUCCION = ("stop", "stop_sobrante", "tp1", "tpf", "cierre")

# Máquina de estados de una orden. Un estado FINAL confirmado no vuelve atrás; un estado incierto se resuelve
# consultando al exchange (nunca reenviando). 'no_confirmada' = el exchange no la conoce tras varias consultas.
TRANSICIONES_ORDEN = {
    "creada": {"enviada", "abierta", "parcial", "completada", "rechazada", "cancelada", "desconocida", "no_confirmada",
               "disparada"},
    "enviada": {"abierta", "parcial", "completada", "rechazada", "cancelada", "desconocida", "no_confirmada",
                "disparada"},
    "abierta": {"parcial", "completada", "cancelacion_solicitada", "cancelada", "rechazada", "disparada", "desconocida"},
    "parcial": {"parcial", "completada", "cancelacion_solicitada", "cancelada", "desconocida"},
    "cancelacion_solicitada": {"cancelada", "completada", "abierta", "parcial", "desconocida"},
    "disparada": {"completada", "cancelada", "desconocida"},
    "desconocida": {"abierta", "parcial", "completada", "cancelada", "rechazada", "disparada", "desconocida",
                    "no_confirmada"},
    "no_confirmada": {"abierta", "parcial", "completada", "cancelada", "rechazada", "disparada", "desconocida",
                      "no_confirmada"},
    "completada": set(), "cancelada": set(), "rechazada": set(),
}


def transicion_valida(de, a):
    if de is None or de == a:
        return True
    return a in TRANSICIONES_ORDEN.get(de, set())


def cambiar_estado(orden, nuevo, log=None):
    """Aplica 'nuevo' al dict de una orden solo si la transición es válida. Devuelve True si cambió/coincide.
    Un estado final confirmado no se pisa (p. ej. una respuesta repetida o atrasada no 'reabre' una orden)."""
    actual = orden.get("estado")
    if transicion_valida(actual, nuevo):
        orden["estado"] = nuevo
        return True
    if log:
        log.warning("Transición de orden ignorada %s -> %s (cloid %s)", actual, nuevo, orden.get("cloid"))
    return False


class ErrorShadow(RuntimeError):
    """Intento de ejecutar en modo shadow. Nunca debe ocurrir: si ocurre es un fallo del bot."""


def id_fill(f):
    """Identificador estable de un fill para no contarlo dos veces (comisiones, PnL) si se lee de nuevo.
    Hyperliquid da 'tid' (id del trade); si falta, se usa oid + hora + tamaño + precio."""
    if f.get("tid") is not None:
        return f"tid:{f['tid']}"
    return f"{f.get('oid')}-{f.get('time')}-{f.get('sz')}-{f.get('px')}"


def nuevo_cloid():
    return "0x" + uuid.uuid4().hex


def _objeto_cloid(raw):
    """El SDK de Hyperliquid quiere un objeto Cloid; la cuenta simulada acepta el texto."""
    try:
        from hyperliquid.utils.types import Cloid
        return Cloid(raw)
    except ImportError:
        return raw


def normalizar_consulta(resp):
    """Respuesta de orderStatus -> {"estado", "oid", "cloid", "orig", "restante", "ejecutado", "px_medio", "hl"}.
    None si la respuesta no se entiende (se trata como desconocida)."""
    if not isinstance(resp, dict):
        return None
    if resp.get("status") == "unknownOid":
        return {"estado": "no_consta", "oid": None, "cloid": None, "orig": None, "restante": None,
                "ejecutado": None, "px_medio": None, "hl": "unknownOid"}
    if resp.get("status") != "order" or not isinstance(resp.get("order"), dict):
        return None
    o = resp["order"]
    det = o.get("order") or {}
    hl = o.get("status")
    estado = ESTADOS_HL.get(hl)
    if estado is None:
        estado = "cancelada" if str(hl).endswith("Canceled") else ("rechazada" if str(hl).endswith("Rejected") else "desconocida")
    try:
        orig = float(det["origSz"]) if det.get("origSz") is not None else None
        rest = float(det["sz"]) if det.get("sz") is not None else None
    except (TypeError, ValueError):
        orig = rest = None
    ejecutado = (orig - rest) if orig is not None and rest is not None else None
    if estado == "abierta" and ejecutado and ejecutado > 0:
        estado = "parcial"
    if estado == "completada" and orig is not None:
        ejecutado = orig
    px = det.get("avgPx")
    return {"estado": estado, "oid": det.get("oid"), "cloid": det.get("cloid"), "orig": orig, "restante": rest,
            "ejecutado": ejecutado, "px_medio": float(px) if px else None, "hl": hl}


def interpretar_respuesta(r):
    """Respuesta de order() -> (estado, oid, ejecutado, px_medio, error, ambigua)."""
    if not isinstance(r, dict):
        return "desconocida", None, None, None, f"respuesta no reconocida: {str(r)[:200]}", True
    if r.get("status") == "err":
        # La acción completa fue rechazada (firma, nonce, cuenta...). No se aceptó ninguna orden.
        return "rechazada", None, 0.0, None, str(r.get("response"))[:300], False
    if r.get("status") != "ok":
        return "desconocida", None, None, None, f"estado {r.get('status')!r}", True
    try:
        st = r["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return "desconocida", None, None, None, "respuesta sin 'statuses'", True
    if isinstance(st, dict) and "resting" in st:
        return "abierta", st["resting"].get("oid"), 0.0, None, None, False
    if isinstance(st, dict) and "filled" in st:
        f = st["filled"]
        try:
            return "completada", f.get("oid"), float(f.get("totalSz")), float(f.get("avgPx")), None, False
        except (TypeError, ValueError):
            return "completada", f.get("oid"), None, None, None, True
    if isinstance(st, dict) and "error" in st:
        return "rechazada", None, 0.0, None, str(st["error"])[:300], False
    return "desconocida", None, None, None, f"estado no reconocido: {str(st)[:200]}", True


class Ejecutor:
    puede_operar = True

    def __init__(self, exchange, info, cuenta, ledger, origen, log, cfg_ordenes, umbral_degradado=5,
                 esperar=time.sleep):
        self.ex, self.info, self.cuenta = exchange, info, cuenta
        self.ledger, self.origen, self.log = ledger, origen, log
        self.cfg = cfg_ordenes
        self.umbral = umbral_degradado
        self.esperar = esperar
        self.congelados = {}            # moneda -> {"motivo", "ts", "tipo"}
        self.errores_seguidos = 0
        self.aciertos_seguidos = 0
        self.degradado = None           # motivo o None
        self.alertas = []               # (ts, severidad, mensaje, trade_id)
        self.llamadas = {"order": 0, "cancel": 0, "consulta": 0, "leverage": 0}

    # ------------------------------------------------------------ salud
    def _fallo(self, motivo):
        self.errores_seguidos += 1
        self.aciertos_seguidos = 0
        if self.errores_seguidos >= self.umbral and not self.degradado:
            self.degradado = f"{self.errores_seguidos} errores seguidos con el exchange: {motivo}"
            self._alerta("error", "Ejecución DEGRADADA: bloqueo entradas nuevas. " + self.degradado)

    def _exito(self):
        self.errores_seguidos = 0
        self.aciertos_seguidos += 1
        if self.degradado and self.aciertos_seguidos >= 3:
            self.log.info("El exchange vuelve a responder bien: salgo del estado degradado.")
            self.degradado = None

    def _alerta(self, severidad, mensaje, trade_id=None, moneda=None):
        self.alertas.append((time.time(), severidad, mensaje, trade_id))
        del self.alertas[:-200]
        (self.log.error if severidad == "error" else self.log.warning)(mensaje)
        if self.ledger and trade_id:
            self.ledger.registrar("alerta", trade_id, self.origen, moneda=moneda, severidad=severidad, mensaje=mensaje)

    def congelar(self, moneda, motivo, tipo="ambigua", trade_id=None):
        if moneda not in self.congelados:
            self.congelados[moneda] = {"motivo": motivo, "ts": time.time(), "tipo": tipo}
            self._alerta("error", f"{moneda} CONGELADO (sin entradas nuevas): {motivo}", trade_id, moneda)

    def descongelar(self, moneda, motivo):
        if self.congelados.pop(moneda, None):
            self.log.info("%s descongelado: %s", moneda, motivo)

    # ------------------------------------------------------------ consultas
    def consultar(self, cloid=None, oid=None):
        """Estado de una orden en el exchange. None si la consulta falla."""
        self.llamadas["consulta"] += 1
        try:
            if cloid:
                r = self.info.query_order_by_cloid(self.cuenta, _objeto_cloid(cloid))
            else:
                r = self.info.query_order_by_oid(self.cuenta, oid)
        except Exception as e:  # noqa: BLE001
            self._fallo(f"consulta de orden: {e}")
            return None
        n = normalizar_consulta(r)
        if n is None:
            self._fallo("respuesta de consulta no reconocida")
        else:
            self._exito()
        return n

    def resolver(self, trade_id, rol, moneda, cloid):
        """Tras una respuesta ambigua: consulta (sin reenviar) hasta saber qué pasó."""
        espera = 1.0
        ultimo = None
        for _ in range(self.cfg.get("max_consultas_estado", 3)):
            self.esperar(espera)
            espera *= 2
            n = self.consultar(cloid=cloid)
            if n is None:
                continue
            ultimo = n
            if n["estado"] != "no_consta":
                return n
        if ultimo and ultimo["estado"] == "no_consta":
            # El exchange no la conoce tras varias consultas: lo más probable es que no llegara, pero NO se da por
            # rechazada (podría aparecer tarde). Estado 'no_confirmada': no se reenvía nada a ciegas, se congela el
            # mercado y la conciliación decide con las órdenes abiertas y la posición reales.
            self.congelar(moneda, f"orden {rol} sin confirmar (no consta en el exchange)", "ambigua", trade_id)
            return {"estado": "no_confirmada", "oid": None, "cloid": cloid, "orig": None, "restante": None,
                    "ejecutado": None, "px_medio": None, "hl": "unknownOid", "sin_confirmar": True}
        self.congelar(moneda, f"no puedo confirmar el estado de la orden {rol}", "ambigua", trade_id)
        return {"estado": "desconocida", "oid": None, "cloid": cloid, "orig": None, "restante": None,
                "ejecutado": None, "px_medio": None, "hl": None}

    # ------------------------------------------------------------ envío
    def enviar(self, trade_id, rol, moneda, compra, sz, px, tipo_orden, reduce_only=False, extra=None):
        """Envía UNA orden y devuelve su estado conocido:
        {"estado", "oid", "cloid", "ejecutado", "px_medio", "error"}."""
        if rol in ROLES_REDUCCION and not reduce_only:
            # Una orden de protección o de cierre que no fuera reduce-only podría AUMENTAR o dar la vuelta a la
            # posición. Es un fallo de programación: se bloquea antes de enviar nada.
            raise ValueError(f"la orden '{rol}' debe ser reduce-only")
        cloid = nuevo_cloid()
        trigger = (tipo_orden.get("trigger") or {}).get("triggerPx") if isinstance(tipo_orden, dict) else None
        if self.ledger:
            self.ledger.registrar("orden_enviada", trade_id, self.origen, moneda=moneda, rol=rol, cloid=cloid,
                                  lado="compra" if compra else "venta", sz=sz, px=px, trigger=trigger,
                                  tipo_orden=tipo_orden, reduce_only=reduce_only, **(extra or {}))
        self.llamadas["order"] += 1
        ambigua, error = False, None
        try:
            r = self.ex.order(moneda, compra, sz, px, tipo_orden, reduce_only=reduce_only, cloid=_objeto_cloid(cloid))
            estado, oid, ejecutado, px_medio, error, ambigua = interpretar_respuesta(r)
        except Exception as e:  # noqa: BLE001  (timeout, red...): NO se sabe si la orden llegó
            estado, oid, ejecutado, px_medio, error, ambigua = "desconocida", None, None, None, f"excepción: {e}", True
        if ambigua:
            self._fallo(error or "respuesta ambigua")
            self.log.warning("%s: respuesta ambigua al enviar %s (%s). Consulto su estado sin reenviar.",
                             moneda, rol, error)
            n = self.resolver(trade_id, rol, moneda, cloid)
            estado, oid = n["estado"], n.get("oid")
            ejecutado, px_medio = n.get("ejecutado"), n.get("px_medio")
        elif estado == "rechazada":
            self._exito()   # el exchange respondió con claridad
        else:
            self._exito()
        res = {"estado": estado, "oid": oid, "cloid": cloid, "ejecutado": ejecutado, "px_medio": px_medio,
               "error": error}
        if self.ledger:
            self.ledger.registrar("orden_estado", trade_id, self.origen, moneda=moneda, rol=rol, cloid=cloid, oid=oid,
                                  estado=estado, ejecutado=ejecutado, px_medio=px_medio, detalle=error)
        return res

    def cancelar(self, trade_id, rol, moneda, oid=None, cloid=None):
        """Cancela y VERIFICA. Devuelve el estado final: cancelada | completada | abierta | parcial | desconocida."""
        return self.cancelar_confirmada(trade_id, rol, moneda, oid, cloid)["estado"]

    def cancelar_confirmada(self, trade_id, rol, moneda, oid=None, cloid=None):
        """Cancela y RE-CONSULTA al exchange. Un ACK de la cancelación NO cuenta como cancelación efectiva.
        Devuelve {"estado", "ejecutado", "px_medio", "confirmado"}:
          confirmado=True solo si el estado final es cancelada/completada/rechazada según el propio exchange
          (o la orden nunca llegó a existir: sin oid conocido y el exchange no la conoce)."""
        if self.ledger:
            self.ledger.registrar("orden_estado", trade_id, self.origen, moneda=moneda, rol=rol, cloid=cloid, oid=oid,
                                  estado="cancelacion_solicitada")
        for intento in range(2):
            self.llamadas["cancel"] += 1
            try:
                if cloid and hasattr(self.ex, "cancel_by_cloid"):
                    self.ex.cancel_by_cloid(moneda, _objeto_cloid(cloid))
                else:
                    self.ex.cancel(moneda, oid)
            except Exception as e:  # noqa: BLE001
                self._fallo(f"cancelación: {e}")
            n = self.consultar(cloid=cloid, oid=oid)
            if n and n["estado"] in ("cancelada", "completada", "rechazada"):
                estado = "cancelada" if n["estado"] == "rechazada" else n["estado"]
                ejecutado = n.get("ejecutado")
                if estado == "cancelada" and ejecutado is None and n.get("orig") is None:
                    ejecutado = None          # el exchange no dice cuánto se llenó: quien llama lo mira en la cuenta
                if self.ledger:
                    self.ledger.registrar("orden_estado", trade_id, self.origen, moneda=moneda, rol=rol, cloid=cloid,
                                          oid=n.get("oid") or oid, estado=estado, ejecutado=ejecutado,
                                          px_medio=n.get("px_medio"), detalle="verificado tras cancelar")
                return {"estado": estado, "ejecutado": ejecutado, "px_medio": n.get("px_medio"), "confirmado": True}
            if n and n["estado"] == "no_consta" and oid is None:
                # Nunca tuvo oid y el exchange no la conoce: no llegó a existir.
                if self.ledger:
                    self.ledger.registrar("orden_estado", trade_id, self.origen, moneda=moneda, rol=rol, cloid=cloid,
                                          oid=oid, estado="cancelada", ejecutado=0.0, detalle="no consta en el exchange")
                return {"estado": "cancelada", "ejecutado": 0.0, "px_medio": None, "confirmado": True}
            self.esperar(1.0 + intento)
        n = self.consultar(cloid=cloid, oid=oid)
        if n and n["estado"] in ("cancelada", "completada", "rechazada"):
            estado = "cancelada" if n["estado"] == "rechazada" else n["estado"]
            if self.ledger:
                self.ledger.registrar("orden_estado", trade_id, self.origen, moneda=moneda, rol=rol, cloid=cloid,
                                      oid=n.get("oid") or oid, estado=estado, ejecutado=n.get("ejecutado"),
                                      px_medio=n.get("px_medio"), detalle="verificado tras cancelar")
            return {"estado": estado, "ejecutado": n.get("ejecutado"), "px_medio": n.get("px_medio"), "confirmado": True}
        estado = n["estado"] if n and n["estado"] in ("abierta", "parcial") else "desconocida"
        # Ni 'sigue abierta' ni 'desconocida' permiten seguir como si estuviera cancelada: se congela el mercado.
        self.congelar(moneda, f"no puedo confirmar la cancelación de la orden {rol} (estado: {estado})", "ambigua",
                      trade_id)
        if self.ledger:
            self.ledger.registrar("orden_estado", trade_id, self.origen, moneda=moneda, rol=rol, cloid=cloid, oid=oid,
                                  estado=estado, ejecutado=(n or {}).get("ejecutado"), detalle="cancelación no confirmada")
        return {"estado": estado, "ejecutado": (n or {}).get("ejecutado"), "px_medio": (n or {}).get("px_medio"),
                "confirmado": False}

    def apalancamiento(self, moneda, apal):
        self.llamadas["leverage"] += 1
        try:
            r = self.ex.update_leverage(apal, moneda, is_cross=False)
        except Exception as e:  # noqa: BLE001
            self._fallo(f"apalancamiento: {e}")
            return False, str(e)
        if isinstance(r, dict) and r.get("status") == "ok":
            self._exito()
            return True, None
        return False, str(r)[:200]

    # ------------------------------------------------------------ fills
    def fills(self, oids=None, cloids=None, desde_ms=0):
        """Fills reales de la cuenta para esas órdenes. None si no se pueden leer."""
        try:
            todos = self.info.user_fills(self.cuenta)
        except Exception as e:  # noqa: BLE001
            self._fallo(f"fills: {e}")
            return None
        oids = {int(o) for o in (oids or []) if o is not None}
        cloids = {c for c in (cloids or []) if c}
        out = []
        for f in todos:
            if f.get("time", 0) < desde_ms:
                continue
            if (f.get("oid") is not None and int(f["oid"]) in oids) or (f.get("cloid") and f["cloid"] in cloids):
                out.append(f)
        return sorted(out, key=lambda f: f.get("time", 0))

    def funding(self, desde_ms, hasta_ms=None):
        try:
            return self.info.user_funding_history(self.cuenta, desde_ms, hasta_ms)
        except Exception as e:  # noqa: BLE001
            self._fallo(f"funding: {e}")
            return None


class EjecutorShadow:
    """Barrera del modo shadow: no tiene exchange, ni info firmante, ni cuenta. Cualquier intento de enviar,
    cancelar, consultar fills de cuenta o cambiar apalancamiento lanza ErrorShadow y queda contado."""

    puede_operar = False

    def __init__(self, log=None):
        self.log = log
        self.intentos = []
        self.congelados = {}
        self.degradado = None
        self.alertas = []
        self.llamadas = {"order": 0, "cancel": 0, "consulta": 0, "leverage": 0}

    def _bloquear(self, que):
        self.intentos.append((time.time(), que))
        msg = f"SHADOW: intento de {que} bloqueado (este modo no puede enviar ni modificar órdenes)"
        self.alertas.append((time.time(), "error", msg, None))
        if self.log:
            self.log.error(msg)
        raise ErrorShadow(msg)

    def enviar(self, *a, **k):
        self._bloquear("enviar una orden")

    def cancelar(self, *a, **k):
        self._bloquear("cancelar una orden")

    def cancelar_confirmada(self, *a, **k):
        self._bloquear("cancelar una orden")

    def apalancamiento(self, *a, **k):
        self._bloquear("cambiar el apalancamiento")

    def consultar(self, *a, **k):
        self._bloquear("consultar órdenes de una cuenta")

    def resolver(self, *a, **k):
        self._bloquear("resolver una orden")

    def fills(self, *a, **k):
        self._bloquear("leer fills de una cuenta")

    def funding(self, *a, **k):
        self._bloquear("leer funding de una cuenta")

    def congelar(self, *a, **k):
        pass

    def descongelar(self, *a, **k):
        pass
