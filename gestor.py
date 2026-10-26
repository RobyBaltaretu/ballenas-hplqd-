"""
Gestión de las operaciones DEL BOT: propiedad, conciliación con el exchange, apertura, protección (stop y
objetivos), gestión (sin pérdidas, stop dinámico, tiempo máximo) y cierre.

PROPIEDAD
  Una posición es del bot si y solo si está en su registro (estado["posiciones"], respaldado por el ledger)
  con su trade_id y los CLOID/oid de sus órdenes, y coincide con lo que hay en la cuenta. Todo lo demás es
  EXTERNO: se muestra y cuenta como riesgo, pero el bot NUNCA lo cierra, ni le pone stops, ni cancela sus
  órdenes. Si la cuenta no coincide con el registro (tamaño mayor, lado distinto, órdenes desconocidas en la
  misma moneda) el mercado se CONGELA y se alerta: el bot no sigue operando a ciegas.

  (Separar el bot en una subcuenta dedicada es lo que de verdad evita la ambigüedad: Hyperliquid compensa
  todas las posiciones de una moneda en una sola por cuenta, así que el registro y los CLOID sirven para
  auditar, pero no separan una posición manual de la del bot en la misma cuenta.)

PROTECCIÓN
  - El stop es una orden trigger de mercado, reduce-only, del tamaño de la posición. Se verifica que existe,
    con su tamaño, lado y precio de activación, en CADA ciclo (con la lista de órdenes abiertas, sin
    consultas extra). Si falta o no cuadra: alerta, bloqueo de entradas y la política configurada
    (ordenes.politica_sin_stop: "reponer_y_alertar" o "cerrar").
  - Mover un stop: primero se pone el NUEVO y se verifica; solo después se cancela el viejo (y se verifica).
    Si el viejo no se puede cancelar queda como "sobrante" (también reduce-only, no puede abrir nada) y se
    reintenta. La posición nunca queda sin stop durante el cambio.
  - Tras una salida parcial se rehace el stop con el tamaño que queda.
  - Antes de poner (o reintentar) un stop se mira en la cuenta si ya hay uno del bot que valga: un envío con
    respuesta ambigua nunca acaba en dos stops. Si aun así hay varios, se conserva el correcto y los demás se
    cancelan (verificado).

ENTRADA
  - Orden límite post-only (ALO). Si no se llena en 'espera_limite_seg' se cancela y se CONFIRMA con el exchange
    que está cancelada (o completada). Solo entonces, y solo por lo que falte según la POSICIÓN REAL y los fills,
    se completa con una IOC. Si la cancelación no se confirma (sigue abierta, parcial o desconocida), NO se envía
    nada más: el mercado se congela, se protege el tamaño real y la conciliación lo resuelve.
  - El trigger del exchange usa el precio de MARCA (mark). El stop dinámico local usa el precio configurado
    (salida.precio_trailing: "mid" o "mark"). Un stop se dispara a un precio y se ejecuta a otro (deslizamiento).
"""

import time

from contabilidad import atribuir_funding, resultado
from ejecucion import FINALES, INCIERTOS, cambiar_estado, id_fill
from estrategia import redondear_precio
from riesgo import NOTIONAL_MINIMO_HL, redondear_tamano_abajo, riesgo_tras_fill

TOLERANCIA_TAM = 0.002          # 0,2 % de diferencia de tamaño se considera igual (redondeos)


def nombre_op(lado, moneda):
    return f"{'LARGO' if lado == 'L' else 'CORTO'} {moneda}"


# ============================================================ lectura de la cuenta
def leer_cuenta(info, cuenta):
    """Foto de la cuenta. Lanza excepción si no se puede leer (quien llama decide)."""
    st = info.user_state(cuenta)
    posiciones = {}
    for p in st.get("assetPositions", []):
        pos = p["position"]
        szi = float(pos["szi"])
        if szi == 0:
            continue
        liq = pos.get("liquidationPx")
        lev = pos.get("leverage") or {}
        posiciones[pos["coin"]] = {
            "szi": szi, "tam": abs(szi), "lado": "L" if szi > 0 else "S",
            "entrada": float(pos.get("entryPx") or 0), "notional": abs(float(pos.get("positionValue") or 0)),
            "pnl": float(pos.get("unrealizedPnl") or 0), "liq": float(liq) if liq not in (None, "", "null") else None,
            "margen": float(pos["marginUsed"]) if pos.get("marginUsed") not in (None, "") else None,
            "apal": lev.get("value"), "tipo_margen": lev.get("type"),
        }
    ms = st.get("marginSummary") or {}
    ordenes = []
    for o in info.frontend_open_orders(cuenta):
        ordenes.append({"moneda": o["coin"], "oid": o.get("oid"), "cloid": o.get("cloid"),
                        "trigger": bool(o.get("isTrigger")), "trigger_px": float(o.get("triggerPx") or 0),
                        "px": float(o.get("limitPx") or 0), "sz": float(o.get("sz") or 0),
                        "compra": o.get("side") == "B", "reduce_only": bool(o.get("reduceOnly")),
                        "tipo": o.get("orderType")})
    mant = st.get("crossMaintenanceMarginUsed")
    return {"ts": time.time(), "posiciones": posiciones, "ordenes": ordenes,
            "equity": float(ms.get("accountValue") or 0), "libre": float(st.get("withdrawable") or 0),
            "margen_usado": float(ms.get("totalMarginUsed") or 0) if ms.get("totalMarginUsed") is not None else None,
            "mantenimiento": float(mant) if mant not in (None, "") else None}


# ============================================================ conciliación (pura: no cambia nada)
def conciliar(ops, cuenta):
    """Compara el registro del bot (ops: moneda -> operación) con la cuenta. No modifica nada."""
    inf = {"propias": [], "cerradas": [], "tam_menor": [], "tam_mayor": [], "lado_distinto": [], "externas": [],
           "ordenes_bot": [], "ordenes_externas": [], "sin_stop": [], "stop_incorrecto": [], "stops_extra": []}
    por_cloid, por_oid = {}, {}
    for moneda, op in ops.items():
        for cl, o in op.get("ordenes", {}).items():
            por_cloid[cl] = (moneda, o.get("rol"))
            if o.get("oid") is not None:
                por_oid[o["oid"]] = (moneda, o.get("rol"))
    for o in cuenta["ordenes"]:
        if (o.get("cloid") and o["cloid"] in por_cloid) or (o.get("oid") in por_oid):
            inf["ordenes_bot"].append(o)
        else:
            inf["ordenes_externas"].append(o)
    for moneda, p in cuenta["posiciones"].items():
        if moneda not in ops:
            inf["externas"].append(dict(p, moneda=moneda))
    for moneda, op in ops.items():
        p = cuenta["posiciones"].get(moneda)
        if not p:
            inf["cerradas"].append(moneda)
            continue
        if p["lado"] != op["lado"]:
            inf["lado_distinto"].append({"moneda": moneda, "local": op["lado"], "cuenta": p["lado"]})
            continue
        tol = TOLERANCIA_TAM * max(op["tam"], 1e-12)
        if p["tam"] < op["tam"] - tol:
            inf["tam_menor"].append({"moneda": moneda, "local": op["tam"], "cuenta": p["tam"]})
        elif p["tam"] > op["tam"] + tol:
            inf["tam_mayor"].append({"moneda": moneda, "local": op["tam"], "cuenta": p["tam"]})
        else:
            inf["propias"].append(moneda)
        # ¿Stop activo y correcto?
        stop = next((o for o in inf["ordenes_bot"] if o["moneda"] == moneda and o["trigger"]
                     and (o.get("cloid") == op.get("stop_cloid") or o.get("oid") == op.get("stop_oid"))), None)
        extra = [o for o in inf["ordenes_bot"] if o["moneda"] == moneda and o["trigger"] and o is not stop]
        if extra:
            inf["stops_extra"].append({"moneda": moneda, "ordenes": extra})
        if stop is None:
            inf["sin_stop"].append(moneda)
            continue
        problemas = []
        if stop["compra"] == (op["lado"] == "L"):
            problemas.append("lado del stop invertido")
        if not stop["reduce_only"]:
            problemas.append("el stop no es reduce-only")
        tam_ref = min(p["tam"], op["tam"])
        if abs(stop["sz"] - tam_ref) > max(TOLERANCIA_TAM * tam_ref, 1e-9):
            problemas.append(f"tamaño del stop {stop['sz']} ≠ posición {tam_ref}")
        if op.get("sl") and abs(stop["trigger_px"] - op["sl"]) > 1e-6 * max(op["sl"], 1):
            problemas.append(f"trigger {stop['trigger_px']} ≠ stop calculado {op['sl']}")
        if problemas:
            inf["stop_incorrecto"].append({"moneda": moneda, "problemas": problemas})
    return inf


# ============================================================ gestor
class Gestor:
    def __init__(self, cfg, ejecutor, info, cuenta, ledger, estado, meta, log, origen, avisar, guardar,
                 memoria=None, esperar=time.sleep):
        self.c, self.ej, self.info, self.cuenta = cfg, ejecutor, info, cuenta
        self.ledger, self.estado, self.meta, self.log = ledger, estado, meta, log
        self.origen, self.avisar, self.guardar, self.memoria = origen, avisar, guardar, memoria
        self.esperar = esperar
        self.ultima_cuenta = None
        self.externas_vistas = set()
        self.sin_stop = {}               # moneda -> motivo (bloquea entradas mientras no se resuelva)
        self.ultimo_informe = None
        self.al_cerrar = None            # función(op, resultado) opcional: el bot la usa para registrar evidencia
        self.riesgo_liquidacion = {}     # moneda -> motivo (bloquea entradas): liquidación demasiado cerca o desconocida
        self.margen = {}                 # foto de margen/mantenimiento (para el panel y los bloqueos)
        estado.setdefault("posiciones", {})

    @property
    def ops(self):
        return self.estado["posiciones"]

    # ------------------------------------------------------------ utilidades
    def _ev(self, tipo_evento, op, /, **datos):
        return self.ledger.registrar(tipo_evento, op["trade_id"], self.origen, moneda=op["moneda"], lado=op["lado"],
                                     estrategia=op.get("estrategia"), version=op.get("version"), **datos)

    def _dec(self, moneda):
        return self.meta[moneda]["szDecimals"]

    def leer(self):
        try:
            self.ultima_cuenta = leer_cuenta(self.info, self.cuenta)
            return self.ultima_cuenta
        except Exception as e:  # noqa: BLE001
            self.ej._fallo(f"lectura de la cuenta: {e}")
            self.log.warning("No pude leer la cuenta: %s", e)
            return None

    def _registrar_orden(self, op, rol, res, sz, px=None, trigger=None):
        op.setdefault("ordenes", {})[res["cloid"]] = {"rol": rol, "oid": res.get("oid"), "estado": res["estado"],
                                                      "sz": sz, "px": px, "trigger": trigger, "ts": time.time()}
        if res.get("cloid"):
            reg = self.estado.setdefault("cloids_propios", {})
            reg[res["cloid"]] = {"trade_id": op["trade_id"], "moneda": op["moneda"], "rol": rol, "ts": time.time()}
            if len(reg) > 2000:                                # se guardan las más recientes
                for cl in sorted(reg, key=lambda k: reg[k].get("ts", 0))[:len(reg) - 2000]:
                    reg.pop(cl, None)

    def _orden_propia(self, cloid):
        """¿Este CLOID lo creó ESTE bot? Primero el registro persistente; si no está, el ledger (una vez)."""
        if not cloid:
            return None
        reg = self.estado.setdefault("cloids_propios", {})
        if cloid in reg:
            return reg[cloid]
        if getattr(self, "_cloids_ledger", None) is None:
            self._cloids_ledger = {}
            try:
                eventos, _ = self.ledger.leer()
                for ev in eventos:
                    if ev.get("tipo") == "orden_enviada" and ev.get("cloid") and ev.get("origen") == self.origen:
                        self._cloids_ledger[ev["cloid"]] = {"trade_id": ev.get("trade_id"), "moneda": ev.get("moneda"),
                                                            "rol": ev.get("rol")}
            except Exception as e:  # noqa: BLE001
                self.log.warning("No pude leer el ledger para reconocer órdenes propias: %s", e)
        return self._cloids_ledger.get(cloid)

    def _cancelar_huerfanas(self, ordenes):
        """Órdenes que creó ESTE bot (reduce-only, o entradas) para una operación que ya no está abierta (p. ej. dos entradas en
        la misma moneda que se fusionaron): se cancelan. Son reduce-only, así que cancelarlas nunca abre nada.
        Devuelve las que NO se han podido tratar como propias."""
        abiertas = {o.get("trade_id") for o in self.ops.values()}
        resto = []
        for o in ordenes:
            propia = self._orden_propia(o.get("cloid"))
            if not propia or propia.get("trade_id") in abiertas or \
                    (not o.get("reduce_only") and propia.get("rol") != "entrada"):
                resto.append(o)
                continue
            try:
                estado = self.ej.cancelar(propia.get("trade_id"), "huerfana", o["moneda"], o.get("oid"), o.get("cloid"))
            except Exception as e:  # noqa: BLE001
                estado = f"error: {e}"
            self.ledger.registrar("conciliacion", propia.get("trade_id"), self.origen, moneda=o["moneda"],
                                  accion="orden propia huérfana cancelada", cloid=o.get("cloid"), oid=o.get("oid"),
                                  resultado=estado)
            self.log.warning("Orden propia huérfana en %s (operación %s ya no abierta): cancelada -> %s",
                             o["moneda"], propia.get("trade_id"), estado)
            if estado not in ("cancelada", "completada"):
                resto.append(o)
        return resto

    # ------------------------------------------------------------ conciliación con efectos
    def conciliar(self, mids):
        """Lee la cuenta, la compara con el registro y aplica SOLO acciones seguras sobre posiciones del bot.
        Devuelve el informe (o None si no se pudo leer la cuenta: en ese caso no se hace nada)."""
        cuenta = self.leer()
        if cuenta is None:
            return None
        # Entradas a medias o inciertas (el proceso cayó, o la orden de entrada no se pudo confirmar): la cuenta
        # y el estado CONFIRMADO de las órdenes deciden qué se ejecutó. Nunca se reenvía nada.
        for op in [o for o in list(self.ops.values()) if o.get("estado") in ("enviada", "desconocido")]:
            if not self._resolver_entradas(op, cuenta):
                continue
            self._ev("conciliacion", op, accion="entrada incompleta: se reconstruye desde la cuenta")
            op.pop("entrada_incierta", None)
            self._despues_de_entrada(op, {"tam": op.get("tam_plan") or 0.0}, sin_reserva=True)
            cuenta = self.leer()
            if cuenta is None:
                return None
        pendientes = {m: o for m, o in self.ops.items() if o.get("estado") in ("enviada", "desconocido")}
        # Órdenes de entrada que siguen vivas en una operación ya abierta (p. ej. tras una entrada incierta): son
        # del bot y se cancelan (verificado) para que la posición no siga creciendo sin protección.
        for op in [o for o in self.ops.values() if o.get("estado") == "abierta"]:
            self._cancelar_entradas_vivas(op, cuenta)
        inf = conciliar({m: o for m, o in self.ops.items() if m not in pendientes}, cuenta)
        self.ultimo_informe = inf
        for moneda in inf["cerradas"]:
            op = self.ops.get(moneda)
            if op:
                self.procesar_cierre(op, None)
        # Primero se averigua qué pasó con las órdenes del bot que ya no están abiertas (p. ej. un objetivo
        # cobrado): así una reducción de tamaño se puede explicar.
        self._ordenes_desaparecidas(cuenta)
        reducidas = set()
        for d in inf["tam_menor"]:
            self._reduccion(self.ops[d["moneda"]], d["cuenta"])
            reducidas.add(d["moneda"])
        adoptadas = set()
        for d in inf["tam_mayor"]:
            op = self.ops[d["moneda"]]
            if self._adoptar_tamano_de_entrada(op, d["cuenta"]):
                adoptadas.add(d["moneda"])
            else:
                self.ej.congelar(d["moneda"], f"la cuenta no coincide con el registro del bot ({d})", "divergencia",
                                 op["trade_id"])
        for d in inf["lado_distinto"]:
            self.ej.congelar(d["moneda"], f"la cuenta no coincide con el registro del bot ({d})", "divergencia",
                             self.ops[d["moneda"]]["trade_id"])
        for p in inf["externas"]:
            if p["moneda"] not in self.externas_vistas:
                self.externas_vistas.add(p["moneda"])
                self.ledger.registrar("externa", None, self.origen, moneda=p["moneda"], lado=p["lado"], tam=p["tam"],
                                      mensaje="posición que el bot no puede atribuirse: NO la toca")
                self.avisar(f"Posición externa en {p['moneda']} ({nombre_op(p['lado'], p['moneda'])}). "
                            f"El bot no la cierra ni la protege.", "aviso")
        for m in list(self.externas_vistas):
            if m not in cuenta["posiciones"]:
                self.externas_vistas.discard(m)
        inf["ordenes_externas"] = self._cancelar_huerfanas(inf["ordenes_externas"])
        avisos = self.__dict__.setdefault("_avisos_cloid", {})
        for o in inf["ordenes_externas"]:
            if o["moneda"] in self.ops:
                self.ej.congelar(o["moneda"], "hay órdenes desconocidas en un mercado con posición del bot",
                                 "divergencia", self.ops[o["moneda"]]["trade_id"])
            if o.get("cloid") and time.time() - avisos.get(o["moneda"], 0) > 3600:   # como mucho 1 aviso/hora/moneda
                avisos[o["moneda"]] = time.time()
                # Una orden con CLOID que no está en el registro en una cuenta dedicada al bot suele indicar OTRA
                # instancia del bot operando la misma cuenta.
                self.avisar(f"Orden con CLOID desconocido en {o['moneda']}: ¿hay otra instancia del bot usando esta "
                            f"cuenta? No la toco.", "error")
        self.sin_stop = {}
        for moneda in inf["sin_stop"]:
            if moneda in self.ops and moneda not in inf["cerradas"] and moneda not in reducidas | adoptadas:
                self._recuperar_stop(self.ops[moneda], "no hay stop activo")
        for d in inf["stop_incorrecto"]:
            op = self.ops.get(d["moneda"])
            if op and d["moneda"] not in reducidas | adoptadas:   # tras una reducción el stop ya se rehízo
                self._recuperar_stop(op, "; ".join(d["problemas"]))
        # Stops del bot que sobran (p. ej. un envío ambiguo que al final sí llegó): se cancelan. Son reduce-only
        # (no pueden abrir nada) y se conserva siempre el stop registrado.
        for d in inf["stops_extra"]:
            op = self.ops.get(d["moneda"])
            if not op:
                continue
            for o in d["ordenes"]:
                cl = o.get("cloid") or self._cloid_por_oid(op, o.get("oid"))
                if cl and cl not in (op.get("stop_cloid"),) and cl not in op.setdefault("sobrantes", []):
                    op["sobrantes"].append(cl)
                    self._ev("proteccion", op, clase="stop", accion="duplicado", cloid=cl, oid=o.get("oid"),
                             motivo="stop del bot de más: se cancela (se conserva el registrado)")
        for op in list(self.ops.values()):
            self._limpiar_sobrantes(op)
        self.vigilar_liquidacion(cuenta)
        # Un congelado por orden ambigua se levanta cuando la cuenta vuelve a cuadrar con el registro
        divergentes = {d["moneda"] for d in inf["tam_mayor"] + inf["lado_distinto"] if d["moneda"] not in adoptadas} | \
                      {o["moneda"] for o in inf["ordenes_externas"] if o["moneda"] in self.ops} | set(pendientes)
        for moneda, cg in list(self.ej.congelados.items()):
            op = self.ops.get(moneda)
            incierta = bool(op and (op.get("entrada_incierta") or any(
                o["estado"] in INCIERTOS for o in op.get("ordenes", {}).values())))
            if cg["tipo"] == "ambigua" and time.time() - cg["ts"] > 60 and moneda not in divergentes and not incierta \
                    and moneda not in self.sin_stop:
                self.ej.descongelar(moneda, "la conciliación confirma que la cuenta cuadra con el registro")
            if cg["tipo"] == "divergencia" and moneda not in divergentes and moneda not in self.ops:
                self.ej.descongelar(moneda, "ya no hay divergencia")
        self.guardar()
        return inf

    def vigilar_liquidacion(self, cuenta):
        """Con los datos REALES de la cuenta (liquidationPx, margen de mantenimiento, margen usado, equity) tras abrir
        y durante toda la vida de la operación. La fórmula aproximada solo filtra al entrar; aquí manda el exchange.
          - Liquidación más cerca que el stop (el stop no protegería): alerta y cierre reduce-only de la posición
            del bot.
          - Liquidación demasiado cerca (menos de distancia_liquidacion_min_multiplo_stop × la distancia al stop):
            alerta y bloqueo de entradas nuevas.
          - Liquidación desconocida (posición del bot o externa): incertidumbre visible; con
            politica_riesgo_desconocido = "bloquear", bloquea entradas.
          - Margen de mantenimiento usado demasiado alto frente al valor de cuenta: bloquea entradas."""
        r = self.c["riesgo"]
        antes = dict(self.riesgo_liquidacion)
        self.riesgo_liquidacion = {}
        for moneda, p in cuenta["posiciones"].items():
            op = self.ops.get(moneda)
            marca = p["notional"] / p["tam"] if p.get("tam") else None      # positionValue usa el precio de marca
            liq = p.get("liq")
            if liq is None:
                if r.get("politica_riesgo_desconocido", "bloquear") == "bloquear" and self.origen == "real":
                    self.riesgo_liquidacion[moneda] = "precio de liquidación desconocido (el exchange no lo da)"
                continue
            if not op or op.get("estado") != "abierta" or not op.get("sl") or not marca:
                continue
            s = 1 if op["lado"] == "L" else -1
            dist_stop = s * (marca - op["sl"])
            dist_liq = s * (marca - liq)
            if s * (liq - op["sl"]) >= 0:
                motivo = f"la liquidación ({liq}) llega ANTES que el stop ({op['sl']})"
                self.riesgo_liquidacion[moneda] = motivo
                self._ev("alerta", op, severidad="error", mensaje=motivo, precio_referencia="mark", marca=marca, liq=liq)
                self.avisar(f"🛑 {moneda}: {motivo}. Cierro la posición del bot (reduce-only).", "error")
                self.cerrar(op, "liquidación más cerca que el stop")
            elif dist_stop > 0 and dist_liq < dist_stop * r["distancia_liquidacion_min_multiplo_stop"]:
                motivo = (f"liquidación a {dist_liq / marca * 100:.1f}% y stop a {dist_stop / marca * 100:.1f}% de la "
                          f"marca (mínimo {r['distancia_liquidacion_min_multiplo_stop']}×)")
                self.riesgo_liquidacion[moneda] = motivo
                if moneda not in antes:
                    self._ev("alerta", op, severidad="aviso", mensaje=motivo, precio_referencia="mark", marca=marca, liq=liq)
                    self.avisar(f"⚠️ {moneda}: {motivo}. Bloqueo entradas nuevas.", "aviso")
        eq, mant = cuenta.get("equity"), cuenta.get("mantenimiento")
        self.margen = {"equity": eq, "mantenimiento": mant, "margen_usado": cuenta.get("margen_usado"),
                       "fraccion_mantenimiento": (mant / eq) if mant is not None and eq else None, "ts": cuenta.get("ts")}
        frac = self.margen["fraccion_mantenimiento"]
        if frac is not None and frac > r.get("mantenimiento_max_fraccion_equity", 0.5):
            self.riesgo_liquidacion["_cuenta"] = (f"margen de mantenimiento {frac * 100:.0f}% del valor de la cuenta "
                                                  f"(máximo {r.get('mantenimiento_max_fraccion_equity', 0.5) * 100:.0f}%)")

    @staticmethod
    def _cloid_por_oid(op, oid):
        if oid is None:
            return None
        return next((cl for cl, o in op.get("ordenes", {}).items() if o.get("oid") == oid), None)

    def _resolver_entradas(self, op, cuenta):
        """¿Están TODAS las órdenes de entrada de 'op' en un estado final confirmado? Si alguna sigue viva se
        intenta cancelar (es del bot) y se confirma. Devuelve True si ya se puede reconstruir desde la cuenta."""
        vivas = {o.get("cloid") for o in cuenta["ordenes"]} | {o.get("oid") for o in cuenta["ordenes"]}
        listo = True
        for cl, o in op.get("ordenes", {}).items():
            if o["rol"] != "entrada" or o["estado"] in FINALES:
                continue
            if cl in vivas or (o.get("oid") is not None and o["oid"] in vivas):
                can = self.ej.cancelar_confirmada(op["trade_id"], "entrada", op["moneda"], o.get("oid"), cl)
                cambiar_estado(o, can["estado"], self.log)
                if not can["confirmado"]:
                    listo = False
                continue
            n = self.ej.consultar(cloid=cl)
            if n is None:
                listo = False
            elif n["estado"] == "no_consta":
                # Ni abierta en la cuenta ni conocida por el exchange: no llegó a existir.
                cambiar_estado(o, "cancelada", self.log) if o.get("oid") is None else None
                listo = listo and o.get("oid") is None
            elif n["estado"] in FINALES:
                cambiar_estado(o, n["estado"], self.log)
            else:
                listo = False
        if not listo:
            self.ej.congelar(op["moneda"], "orden de entrada del bot sin estado final confirmado", "ambigua",
                             op["trade_id"])
        return listo

    def _cancelar_entradas_vivas(self, op, cuenta):
        vivas = {o.get("cloid") for o in cuenta["ordenes"]} | {o.get("oid") for o in cuenta["ordenes"]}
        for cl, o in op.get("ordenes", {}).items():
            if o["rol"] == "entrada" and (cl in vivas or (o.get("oid") is not None and o["oid"] in vivas)):
                can = self.ej.cancelar_confirmada(op["trade_id"], "entrada", op["moneda"], o.get("oid"), cl)
                cambiar_estado(o, can["estado"], self.log)

    def _fills_entrada(self, op):
        oids = [o["oid"] for o in op.get("ordenes", {}).values() if o["rol"] == "entrada" and o.get("oid") is not None]
        cloids = [cl for cl, o in op.get("ordenes", {}).items() if o["rol"] == "entrada"]
        return self.ej.fills(oids=oids, cloids=cloids, desde_ms=int(op["abierta_ts"] * 1000) - 60_000)

    def _adoptar_tamano_de_entrada(self, op, tam_cuenta):
        """La posición es MAYOR que el registro. Si la diferencia la explican fills de las órdenes de ENTRADA del
        propio bot (una entrada incierta que acabó llenándose), el tamaño real es del bot: se adopta y se protege
        con un stop del tamaño real. Si no se puede demostrar, no se adopta (divergencia)."""
        if len(op.get("tramos_tam") or []) > 1:
            return False                # ya hubo salidas parciales: no se puede atribuir con certeza
        fills = self._fills_entrada(op)
        if fills is None:
            return False
        suma = sum(float(f["sz"]) for f in fills)
        if abs(suma - tam_cuenta) > TOLERANCIA_TAM * max(tam_cuenta, 1e-12):
            return False
        entrada = sum(float(f["px"]) * float(f["sz"]) for f in fills) / suma if suma else op["entrada"]
        anterior = op["tam"]
        self._registrar_fills(op, fills, "entrada")
        op.update(tam=tam_cuenta, tam_inicial=tam_cuenta, entrada=entrada,
                  tramos_tam=[[int(op["abierta_ts"] * 1000), tam_cuenta]])
        op["riesgo_usd"] = riesgo_tras_fill(tam_cuenta, entrada, op.get("sl") or op.get("sl_inicial"), self.c)
        self._ev("conciliacion", op, accion="tamaño adoptado: lo explican fills de la entrada del propio bot",
                 de=anterior, a=tam_cuenta)
        self.avisar(f"{op['moneda']}: la entrada del bot acabó llenando {tam_cuenta} (registro: {anterior}). "
                    f"Protejo el tamaño real.", "aviso")
        self._recuperar_stop(op, f"tamaño real {tam_cuenta} distinto del registrado {anterior}")
        return True

    def _registrar_fills(self, op, fills, rol, **extra):
        """Registra en el ledger SOLO los fills que no se habían registrado (idempotente)."""
        vistos = set(op.get("fills_vistos") or [])
        for f in fills:
            fid = id_fill(f)
            if fid in vistos:
                continue
            vistos.add(fid)
            self._ev("fill", op, rol=rol, px=float(f["px"]), sz=float(f["sz"]), comision=float(f.get("fee") or 0),
                     pnl_cerrado=float(f.get("closedPnl") or 0) if rol != "entrada" else None, oid=f.get("oid"),
                     cloid=f.get("cloid"), fill_id=fid, ts_exchange=f.get("time"), estimado=False,
                     maker=None if f.get("crossed") is None else not f.get("crossed"), **extra)
        op["fills_vistos"] = sorted(vistos)

    def _ordenes_desaparecidas(self, cuenta):
        """Órdenes del bot que el registro cree abiertas y ya no lo están: se consulta qué pasó."""
        abiertas = {o.get("cloid") for o in cuenta["ordenes"]} | {o.get("oid") for o in cuenta["ordenes"]}
        for op in list(self.ops.values()):
            for cl, o in list(op.get("ordenes", {}).items()):
                if o["estado"] not in ("abierta", "parcial", "desconocida", "no_confirmada", "disparada"):
                    continue
                if cl in abiertas or o.get("oid") in abiertas:
                    continue
                n = self.ej.consultar(cloid=cl)
                if n is None:
                    continue
                nuevo = n["estado"]
                if nuevo == "no_consta":
                    if o.get("oid") is not None:
                        continue                 # se conocía y ahora no consta: no se inventa; se reintenta luego
                    nuevo = "cancelada"
                if cambiar_estado(o, nuevo, self.log):
                    self._ev("orden_estado", op, rol=o["rol"], cloid=cl, oid=n.get("oid") or o.get("oid"),
                             estado=o["estado"], ejecutado=n.get("ejecutado"), px_medio=n.get("px_medio"),
                             detalle="detectado en conciliación")

    def _reduccion(self, op, tam_cuenta):
        """La posición del bot es MÁS PEQUEÑA que en el registro: objetivo parcial cobrado (lo normal) o algo
        externo. Se actualiza el tamaño y se rehace el stop con lo que queda."""
        tps = [o for o in op.get("ordenes", {}).values() if o["rol"] in ("tp1", "tpf")]
        llenado_tp = sum(o["sz"] for o in tps if o["estado"] == "completada")
        diferencia = op["tam"] - tam_cuenta
        explicado = llenado_tp and abs(llenado_tp - (op["tam_inicial"] - tam_cuenta)) <= TOLERANCIA_TAM * op["tam_inicial"] + 1e-9
        if not explicado:
            self.avisar(f"{op['moneda']}: la posición del bot ha bajado de {op['tam']} a {tam_cuenta} sin que conste "
                        f"un objetivo cobrado. Actualizo el registro y el stop.", "aviso")
            self._ev("alerta", op, severidad="aviso", mensaje=f"reducción no explicada {op['tam']} -> {tam_cuenta}")
        else:
            if op.get("parcial") and not op.get("tp1_hecho"):
                op["tp1_hecho"] = True
                self.avisar(f"💰 {nombre_op(op['lado'], op['moneda'])}: primer objetivo cobrado.", "info")
        op["tam"] = tam_cuenta
        op.setdefault("tramos_tam", []).append([int(time.time() * 1000), tam_cuenta])
        op["riesgo_usd"] = riesgo_tras_fill(tam_cuenta, op["entrada"], op["sl"], self.c)
        self._ev("conciliacion", op, accion="tamaño actualizado", diferencia=diferencia, tam=tam_cuenta)
        if not self.reemplazar_stop(op, op["sl"], tam_cuenta, "tamaño actualizado tras salida parcial"):
            self.sin_stop[op["moneda"]] = "no se pudo rehacer el stop tras la reducción"

    def _recuperar_stop(self, op, motivo):
        moneda = op["moneda"]
        self.sin_stop[moneda] = motivo
        if op.get("sl") is None:
            op["sl"] = op.get("sl_inicial")
        if op.get("sl") is None and op.get("entrada"):
            s = 1 if op["lado"] == "L" else -1
            op["sl"] = redondear_precio(op["entrada"] * (1 - s * op["sl_pct"] / 100), self._dec(moneda))
        self._ev("proteccion", op, clase="stop", accion="ausente_o_incorrecto", motivo=motivo)
        politica = self.c["ordenes"]["politica_sin_stop"]
        self.avisar(f"⚠️ {moneda}: stop no verificado ({motivo}). Bloqueo entradas nuevas y aplico la política "
                    f"'{politica}'.", "error")
        if politica == "cerrar" or op.get("sl") is None:
            self.cerrar(op, f"stop no verificado: {motivo}")
            return
        if self.reemplazar_stop(op, op["sl"], op["tam"], "reponer stop"):
            self.sin_stop.pop(moneda, None)

    # ------------------------------------------------------------ protección
    def poner_stop(self, op, trigger, tam):
        """Stop trigger de mercado reduce-only. Devuelve el resultado si queda VERIFICADO como abierto.
        Si la respuesta es ambigua NO se reintenta aquí: asegurar_stop mira antes la cuenta."""
        moneda, larga = op["moneda"], op["lado"] == "L"
        trigger = redondear_precio(trigger, self._dec(moneda))
        limite = redondear_precio(trigger * (0.9 if larga else 1.1), self._dec(moneda))   # tolerancia de ejecución
        tipo = {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "sl"}}
        res = self.ej.enviar(op["trade_id"], "stop", moneda, not larga, tam, limite, tipo, reduce_only=True)
        self._registrar_orden(op, "stop", res, tam, limite, trigger)
        if res["estado"] not in ("abierta",):
            self._ev("proteccion", op, clase="stop", accion="fallo", trigger=trigger, cloid=res["cloid"],
                     motivo=res.get("error") or res["estado"])
            return None
        n = self.ej.consultar(cloid=res["cloid"])
        if not n or n["estado"] not in ("abierta",):
            self._ev("proteccion", op, clase="stop", accion="no_verificado", trigger=trigger, cloid=res["cloid"],
                     motivo=(n or {}).get("estado", "consulta fallida"))
            return None
        self._ev("proteccion", op, clase="stop", accion="verificado", trigger=trigger, cloid=res["cloid"],
                 oid=res.get("oid"), sz=tam, tipo_orden="trigger de mercado (precio de marca)",
                 precio_referencia="mark", riesgo_usd=op.get("riesgo_usd"))
        return res

    def _stops_en_cuenta(self, op, cuenta):
        """Órdenes trigger del BOT (por cloid/oid registrados) en la moneda de 'op'."""
        mios = {cl for cl, o in op.get("ordenes", {}).items() if o["rol"] in ("stop", "stop_sobrante")}
        oids = {o.get("oid") for o in op.get("ordenes", {}).values()
                if o["rol"] in ("stop", "stop_sobrante") and o.get("oid") is not None}
        return [o for o in cuenta["ordenes"] if o["moneda"] == op["moneda"] and o["trigger"]
                and ((o.get("cloid") and o["cloid"] in mios) or (o.get("oid") is not None and o["oid"] in oids))]

    def _stop_correcto(self, op, o, trigger, tam):
        return (o["compra"] == (op["lado"] == "S") and o["reduce_only"]
                and abs(o["sz"] - tam) <= max(TOLERANCIA_TAM * tam, 1e-9)
                and abs(o["trigger_px"] - trigger) <= 1e-6 * max(trigger, 1))

    def asegurar_stop(self, op, trigger, tam, intentos=2):
        """Deja UN stop del bot verificado de 'tam' a 'trigger'. Antes de enviar —y antes de cada reintento— mira
        en la cuenta si ya existe uno del bot que valga (p. ej. de un envío cuya respuesta fue ambigua): así un
        reintento nunca duplica stops. Devuelve el resultado o None."""
        trigger = redondear_precio(trigger, self._dec(op["moneda"]))
        for _ in range(intentos):
            cuenta = self.leer()
            if cuenta is None:
                return None
            existentes = self._stops_en_cuenta(op, cuenta)
            valido = next((o for o in existentes if self._stop_correcto(op, o, trigger, tam)), None)
            if valido is not None:
                cl = valido.get("cloid") or self._cloid_por_oid(op, valido.get("oid"))
                if cl in op.get("ordenes", {}):
                    cambiar_estado(op["ordenes"][cl], "abierta", self.log)
                self._ev("proteccion", op, clase="stop", accion="verificado", trigger=trigger, cloid=cl,
                         oid=valido.get("oid"), sz=tam, detalle="ya existía en el exchange: se adopta (sin reenviar)",
                         precio_referencia="mark")
                return {"cloid": cl, "oid": valido.get("oid"), "estado": "abierta"}
            res = self.poner_stop(op, trigger, tam)
            if res:
                return res
        return None

    def poner_tp(self, op, rol, tam, px):
        moneda, larga = op["moneda"], op["lado"] == "L"
        px = redondear_precio(px, self._dec(moneda))
        res = self.ej.enviar(op["trade_id"], rol, moneda, not larga, tam, px, {"limit": {"tif": "Gtc"}}, reduce_only=True)
        self._registrar_orden(op, rol, res, tam, px)
        ok = res["estado"] in ("abierta", "completada")
        self._ev("proteccion", op, clase="tp", accion="colocado" if ok else "fallo", px=px, sz=tam, cloid=res["cloid"],
                 rol=rol, tipo_orden="límite reduce-only (puede quedar sin llenar si el precio solo la toca)",
                 motivo=res.get("error"))
        return ok

    def reemplazar_stop(self, op, nuevo, tam, motivo):
        """Nuevo stop primero (verificado); después se cancela el viejo (verificado)."""
        viejo_cloid, viejo_trigger = op.get("stop_cloid"), op.get("sl")
        res = self.asegurar_stop(op, nuevo, tam)
        if not res:
            self.log.warning("%s: no pude poner el stop nuevo (%s); el anterior sigue protegiendo.", op["moneda"], motivo)
            return False
        op["stop_cloid"], op["stop_oid"] = res["cloid"], res.get("oid")
        op["sl"] = redondear_precio(nuevo, self._dec(op["moneda"]))
        op["stop_verificado_ts"] = time.time()
        if viejo_cloid and viejo_cloid != res["cloid"] and viejo_cloid not in op.setdefault("sobrantes", []):
            op["sobrantes"].append(viejo_cloid)
        self._limpiar_sobrantes(op)
        if viejo_trigger is not None and viejo_trigger != op["sl"]:
            self._ev("trailing", op, de=viejo_trigger, a=op["sl"], motivo=motivo, precio_referencia="mark")
        self.guardar()
        return True

    def _limpiar_sobrantes(self, op):
        quedan = []
        for cl in op.get("sobrantes", []):
            if cl == op.get("stop_cloid"):
                continue                         # nunca se cancela el stop vigente
            o = op["ordenes"].get(cl, {})
            if o.get("estado") in FINALES:
                continue
            estado = self.ej.cancelar(op["trade_id"], "stop_sobrante", op["moneda"], o.get("oid"), cl)
            if cl in op["ordenes"]:
                cambiar_estado(op["ordenes"][cl], estado, self.log)
            if estado not in ("cancelada", "completada"):
                quedan.append(cl)
        op["sobrantes"] = quedan

    # ------------------------------------------------------------ apertura
    def abrir(self, plan):
        """plan: trade_id, moneda, lado, tam, apalancamiento, sl_pct, salida, duracion_min, estrategia, version,
        precio_ref, riesgo_usd, motivo, id_mem, explorando. Devuelve la operación o None."""
        moneda, lado = plan["moneda"], plan["lado"]
        comprar, dec = lado == "L", self._dec(moneda)
        tid = plan["trade_id"]
        # Reserva por moneda: UNA operación por mercado. Si ya hay una del bot (aunque esté «enviada» o incierta)
        # o la cuenta ya tiene posición ahí, no se envía nada: el exchange netea por mercado y dos operaciones
        # acabarían mezcladas en una sola posición sin dueño claro.
        previa = self.ops.get(moneda)
        cuenta = self.leer() if previa is None else None          # lectura fresca: nada de fotos viejas
        en_cuenta = ((cuenta or {}).get("posiciones") or {}).get(moneda)
        if previa is not None or cuenta is None or en_cuenta:
            motivo = (f"ya hay una operación del bot en {moneda} ({previa.get('trade_id')})" if previa is not None
                      else "no pude leer la cuenta antes de entrar" if cuenta is None
                      else f"la cuenta ya tiene posición en {moneda}")
            self.ledger.registrar("decision", tid, self.origen, moneda=moneda, lado=lado, estrategia=plan.get("estrategia"),
                                  aprobada=False, motivos=[motivo + ": no se abre otra"])
            self.log.warning("%s: %s; no se abre otra.", moneda, motivo)
            return None
        ok, err = self.ej.apalancamiento(moneda, plan["apalancamiento"])
        if not ok:
            self.ledger.registrar("error", tid, self.origen, moneda=moneda, mensaje=f"no se pudo fijar el apalancamiento: {err}",
                                  estado="error")
            return None
        op = {"trade_id": tid, "origen": self.origen, "moneda": moneda, "lado": lado, "estrategia": plan["estrategia"],
              "version": plan.get("version"), "motivo": plan.get("motivo"), "estado": "enviada", "tam": 0.0,
              "tam_inicial": 0.0, "entrada": None, "precio_ref": plan["precio_ref"], "sl": None, "sl_pct": plan["sl_pct"],
              "salida": plan["salida"], "duracion_min": plan["duracion_min"], "abierta_ts": time.time(),
              "ordenes": {}, "sobrantes": [], "id_mem": plan.get("id_mem"), "explorando": plan.get("explorando", False),
              "riesgo_plan_usd": plan.get("riesgo_usd"), "tam_plan": plan["tam"], "parcial": False, "tp1_hecho": False,
              "en_breakeven": False, "huella": plan.get("huella")}
        # Se registra ANTES de enviar nada: si el proceso cae a mitad, la posición tiene dueño conocido.
        self.ops[moneda] = op
        self.guardar()
        cfg = self.c["ordenes"]
        ejecutado = 0.0
        if cfg["entrada_limite"]:
            try:
                libro = self.info.l2_snapshot(moneda)["levels"]
                px = float(libro[0][0]["px"]) if comprar else float(libro[1][0]["px"])
            except Exception:  # noqa: BLE001
                px = plan["precio_ref"]
            px = redondear_precio(px, dec)
            res = self.ej.enviar(tid, "entrada", moneda, comprar, plan["tam"], px, {"limit": {"tif": "Alo"}})
            self._registrar_orden(op, "entrada", res, plan["tam"], px)
            orden = op["ordenes"][res["cloid"]]
            if res["estado"] in INCIERTOS:
                return self._entrada_incierta(op, plan, "no se sabe si la orden límite de entrada llegó al exchange")
            if res["estado"] in ("abierta", "parcial"):
                final = None
                for _ in range(max(1, int(cfg["espera_limite_seg"] // 2))):     # una consulta cada 2 s
                    self.esperar(2)
                    n = self.ej.consultar(cloid=res["cloid"])
                    if n and (n["estado"] in FINALES or n["estado"] == "parcial"):
                        # Al primer llenado parcial se deja de esperar: se cancela el resto y se protege YA lo
                        # ejecutado (la ventana sin stop queda en ~2 s en vez de toda la espera).
                        final = n["estado"]
                        break
                if final != "completada":
                    # Cancelar y CONFIRMAR con el exchange. Un ACK no basta: se re-consulta el estado.
                    can = self.ej.cancelar_confirmada(tid, "entrada", moneda, res.get("oid"), res["cloid"])
                    cambiar_estado(orden, can["estado"], self.log)
                    if not can["confirmado"]:
                        return self._entrada_incierta(
                            op, plan, f"la orden límite de entrada no se pudo confirmar cancelada (estado: {can['estado']})")
                else:
                    cambiar_estado(orden, "completada", self.log)
            # La orden de entrada está en un estado FINAL confirmado. Lo ejecutado lo dicen la POSICIÓN REAL y los
            # fills (y deben coincidir): así un parcial nunca se cuenta dos veces ni se completa de más.
            ejecutado = self._ejecutado_confirmado(op)
            if ejecutado is None:
                return self._entrada_incierta(op, plan, "lo ejecutado según la cuenta y según los fills no coincide "
                                                        "(o no se pudo leer)")
        falta = redondear_tamano_abajo(plan["tam"] - ejecutado, dec)
        if falta > 0 and falta * plan["precio_ref"] >= NOTIONAL_MINIMO_HL and (cfg["reserva_a_mercado"] or not cfg["entrada_limite"]):
            try:
                mid = float(self.info.all_mids()[moneda])
            except Exception:  # noqa: BLE001
                mid = None
            s = 1 if comprar else -1
            if mid is None:
                self.log.warning("%s: sin precio actual, no completo a mercado.", moneda)
            elif s * (mid - plan["precio_ref"]) / plan["precio_ref"] * 100 > cfg["persecucion_max_pct"]:
                self._ev("decision", op, aprobada=False, motivos=["el precio se escapó: no completo a mercado"])
            else:
                lim = redondear_precio(mid * (1 + s * cfg["deslizamiento_max"]), dec)
                res = self.ej.enviar(tid, "entrada", moneda, comprar, falta, lim, {"limit": {"tif": "Ioc"}},
                                     extra={"nota": "reserva a mercado (IOC) tras confirmar la orden límite"})
                self._registrar_orden(op, "entrada", res, falta, lim)
                if res["estado"] in INCIERTOS:
                    return self._entrada_incierta(op, plan, "respuesta ambigua de la orden IOC de reserva")
        return self._despues_de_entrada(op, plan)

    def _ejecutado_confirmado(self, op):
        """Tamaño ejecutado de verdad tras una entrada con estado final confirmado, o None si no se puede asegurar:
        posición real de la cuenta (debe ser del lado esperado) y, si hay fills, deben sumar lo mismo; y ninguna
        orden de entrada del bot puede seguir abierta en la cuenta."""
        cuenta = self.leer()
        if cuenta is None:
            return None
        vivas = {o.get("cloid") for o in cuenta["ordenes"]} | {o.get("oid") for o in cuenta["ordenes"]}
        if any(o["rol"] == "entrada" and (cl in vivas or (o.get("oid") is not None and o["oid"] in vivas))
               for cl, o in op["ordenes"].items()):
            return None
        p = cuenta["posiciones"].get(op["moneda"])
        if p and p["lado"] != op["lado"]:
            return None
        real = p["tam"] if p else 0.0
        fills = self._fills_entrada(op)
        if fills is not None:
            suma = sum(float(f["sz"]) for f in fills)
            if abs(suma - real) > max(TOLERANCIA_TAM * max(op["tam_plan"], real), 1e-9):
                return None
        return real

    def _entrada_incierta(self, op, plan, motivo):
        """No se sabe con certeza qué ha pasado con la entrada: NO se completa nada más ni se reenvía. Se congela el
        mercado (bloquea entradas nuevas en él) y se protege lo que de verdad haya en la cuenta."""
        op["entrada_incierta"] = motivo
        self.ej.congelar(op["moneda"], f"entrada incierta: {motivo}", "ambigua", op["trade_id"])
        self._ev("alerta", op, severidad="error", mensaje=f"entrada incierta: {motivo}. No se completa a mercado; "
                                                           f"se protege el tamaño real y se concilia.")
        self.avisar(f"⚠️ {op['moneda']}: entrada incierta ({motivo}). Congelo el mercado y protejo lo que haya.", "error")
        return self._despues_de_entrada(op, plan, sin_reserva=True)

    def _despues_de_entrada(self, op, plan, sin_reserva=False):
        """La verdad es la cuenta: se mira cuánto se ejecutó de verdad y se protege ESE tamaño (aunque difiera
        del plan)."""
        moneda = op["moneda"]
        cuenta = self.leer()
        if cuenta is None:
            self.ej.congelar(moneda, "no pude leer la cuenta tras la entrada", "ambigua", op["trade_id"])
            op["estado"] = "desconocido"
            self.guardar()
            return op
        p = cuenta["posiciones"].get(moneda)
        if not p:
            if op.get("entrada_incierta") or any(o["rol"] == "entrada" and o["estado"] not in FINALES
                                                  for o in op["ordenes"].values()):
                # Aún no hay posición, pero la entrada no está resuelta: se mantiene el registro (dueño conocido)
                # y el mercado congelado; la conciliación decidirá cuando el exchange confirme el estado final.
                op["estado"] = "desconocido"
                self.guardar()
                return op
            self.ops.pop(moneda, None)
            self._ev("cierre", op, causa="entrada sin ejecutar", pnl_bruto=0.0, sin_ejecucion=True,
                     cierre_id=f"{op['trade_id']}:cierre")
            self.guardar()
            self.log.info("%s: la entrada no se llenó.", moneda)
            return None
        if p["lado"] != op["lado"]:
            self.ej.congelar(moneda, "posición en sentido contrario al esperado tras la entrada", "divergencia", op["trade_id"])
            return op
        fills = self._fills_entrada(op) or []
        self._registrar_fills(op, fills, "entrada", parcial=len(fills) > 1)
        tam = p["tam"]
        te = sum(float(f["sz"]) for f in fills)
        if fills and abs(te - tam) <= max(TOLERANCIA_TAM * tam, 1e-9):
            entrada = sum(float(f["px"]) * float(f["sz"]) for f in fills) / te
        else:
            entrada = p["entrada"]
            if fills:
                self._ev("alerta", op, severidad="aviso", mensaje=f"los fills de entrada ({te}) no suman la posición "
                                                                   f"({tam}): uso el precio medio de la cuenta")
        s = 1 if op["lado"] == "L" else -1
        cs = op["salida"]
        dec = self._dec(moneda)
        r_dist = entrada * op["sl_pct"] / 100
        op.update(estado="abierta", tam=tam, tam_inicial=tam, entrada=entrada, r_dist=r_dist, mejor=entrada,
                  r_usd=tam * r_dist, tramos_tam=[[int(op["abierta_ts"] * 1000), tam]])
        if tam < plan["tam"] * (1 - TOLERANCIA_TAM):
            self._ev("alerta", op, severidad="info",
                     mensaje=f"entrada parcial: {tam} de {plan['tam']} (se protege y gestiona lo ejecutado)")
        elif tam > plan["tam"] * (1 + TOLERANCIA_TAM):
            self._ev("alerta", op, severidad="aviso",
                     mensaje=f"ejecutado {tam} > plan {plan['tam']}: se protege el tamaño REAL")
        sl = entrada - s * r_dist
        op["sl_inicial"] = redondear_precio(sl, dec)
        # Riesgo recalculado con lo ejecutado de verdad (tamaño y precio medio reales)
        op["riesgo_usd"] = riesgo_tras_fill(tam, entrada, op["sl_inicial"], self.c)
        if self.memoria is not None and op.get("id_mem"):
            for r in self.memoria.datos["registros"]:
                if r["id"] == op["id_mem"]:
                    r["entrada"] = entrada
        self.guardar()
        res = self.asegurar_stop(op, sl, tam)
        if not res:
            self.avisar(f"No pude poner ni verificar el stop de {moneda}: cierro la posición del bot.", "error")
            self.sin_stop[moneda] = "no se pudo poner el stop tras la entrada"
            if self.cerrar(op, "no se pudo poner el stop"):
                self.sin_stop.pop(moneda, None)
                return None
            # Ni stop ni cierre: queda registrada, sin_stop bloquea entradas y el siguiente ciclo lo reintenta.
            return op
        op.update(stop_cloid=res["cloid"], stop_oid=res.get("oid"), sl=op["sl_inicial"], stop_verificado_ts=time.time())
        if cs.get("sin_objetivo"):
            op["tpf"] = None
        else:
            tpf = entrada + s * cs["tp_final_r"] * r_dist
            tp1 = entrada + s * cs["tp1_r"] * r_dist
            tam1 = redondear_tamano_abajo(tam * cs["parcial_pct"] / 100, dec)
            parcial = bool(cs["parcial_activo"] and tam1 * entrada >= NOTIONAL_MINIMO_HL
                           and (tam - tam1) * entrada >= NOTIONAL_MINIMO_HL)
            op.update(parcial=parcial, tam_tp1=tam1 if parcial else 0.0, tp1=redondear_precio(tp1, dec) if parcial else None,
                      tpf=redondear_precio(tpf, dec))
            ok = (self.poner_tp(op, "tp1", tam1, tp1) and self.poner_tp(op, "tpf", redondear_tamano_abajo(tam - tam1, dec), tpf)) \
                if parcial else self.poner_tp(op, "tpf", tam, tpf)
            if not ok:
                self.avisar(f"No pude poner algún objetivo de {moneda}; queda protegida por el stop.", "aviso")
        self.guardar()
        return op

    # ------------------------------------------------------------ gestión
    def precio_breakeven(self, op):
        s = 1 if op["lado"] == "L" else -1
        com = self.c["comisiones"]
        return op["entrada"] + s * op["entrada"] * (com["taker_pct"] * 2 / 100) * 1.2

    def duracion_efectiva_min(self, op):
        return min(op.get("duracion_min") or 1e12, self.c["riesgo"]["duracion_max_global_h"] * 60)

    def gestionar(self, op, px, ahora=None):
        """Tiempo máximo, sin pérdidas y stop dinámico. 'px' es el precio elegido para el trailing."""
        ahora = ahora or time.time()
        if op.get("estado") not in ("abierta", "parcial") or op.get("entrada") is None:
            return
        dur = self.duracion_efectiva_min(op)
        if ahora - op["abierta_ts"] >= dur * 60:
            self.cerrar(op, f"tiempo máximo ({dur / 60:.0f} h)")
            return
        if px is None or op["moneda"] in self.ej.congelados:
            return
        cs, s = op["salida"], 1 if op["lado"] == "L" else -1
        if s * (px - op["mejor"]) > 0:
            op["mejor"] = px
        avance_r = s * (op["mejor"] - op["entrada"]) / op["r_dist"] if op.get("r_dist") else 0
        if (op.get("tp1_hecho") or avance_r >= cs["breakeven_tras_r"]) and not op.get("en_breakeven"):
            if self.reemplazar_stop(op, self.precio_breakeven(op), op["tam"], "sin pérdidas"):
                op["en_breakeven"] = True
        if op.get("tp1_hecho") and op.get("en_breakeven"):
            nuevo = op["mejor"] - s * cs["trailing_r"] * op["r_dist"]
            if s * (nuevo - op["sl"]) >= cs.get("mover_stop_min_r", 0.25) * op["r_dist"]:
                self.reemplazar_stop(op, nuevo, op["tam"], "stop dinámico")

    # ------------------------------------------------------------ cierre
    def cerrar(self, op, causa):
        """Cierra SOLO el tamaño del bot con órdenes IOC reduce-only y verifica en la cuenta.
        Mantiene el stop hasta confirmar el cierre. Devuelve True si quedó cerrada."""
        moneda = op["moneda"]
        if op.get("estado") != "cerrando":
            self._ev("salida_solicitada", op, causa=causa)
        op["estado"], op["causa_pendiente"] = "cerrando", causa
        for cl, o in list(op.get("ordenes", {}).items()):
            if o["rol"] in ("tp1", "tpf") and o["estado"] in ("abierta", "parcial"):
                cambiar_estado(o, self.ej.cancelar(op["trade_id"], o["rol"], moneda, o.get("oid"), cl), self.log)
        cuenta = self.leer()
        if cuenta is None:
            return False
        p = cuenta["posiciones"].get(moneda)
        if p and p["lado"] == op["lado"]:
            tam = min(p["tam"], op["tam"]) if op["tam"] else p["tam"]
            dec = self._dec(moneda)
            for desl in (self.c["ordenes"]["deslizamiento_max"], 0.02, 0.05):
                try:
                    mid = float(self.info.all_mids()[moneda])
                except Exception:  # noqa: BLE001
                    break
                s = 1 if op["lado"] == "L" else -1
                lim = redondear_precio(mid * (1 - s * desl), dec)
                res = self.ej.enviar(op["trade_id"], "cierre", moneda, op["lado"] == "S", tam, lim,
                                     {"limit": {"tif": "Ioc"}}, reduce_only=True, extra={"causa": causa})
                self._registrar_orden(op, "cierre", res, tam, lim)
                cuenta = self.leer()
                if cuenta is None:
                    return False
                p = cuenta["posiciones"].get(moneda)
                if not p or p["lado"] != op["lado"] or p["tam"] <= TOLERANCIA_TAM * tam:
                    break
                tam = min(p["tam"], tam)    # lo que queda por cerrar (nunca más de lo que era del bot)
        if p and p["lado"] == op["lado"] and p["tam"] > TOLERANCIA_TAM * max(op["tam"], 1e-12):
            self.avisar(f"No pude cerrar {moneda} ({causa}). Mantiene su stop; lo reintento en el siguiente ciclo.", "error")
            self.guardar()
            return False
        self.procesar_cierre(op, causa)
        return True

    def procesar_cierre(self, op, causa):
        """La posición del bot ya no está en la cuenta: se cancelan sus órdenes restantes (verificado), se
        calcula el resultado con fills y comisiones reales, se atribuye el funding y se registra."""
        moneda = op["moneda"]
        if not op.get("cierre_en_curso"):
            op["cierre_en_curso"] = True        # si el proceso cae a mitad, al repetir no se duplica nada
            self.guardar()
        for cl, o in list(op.get("ordenes", {}).items()):
            if o["estado"] in ("abierta", "parcial", "desconocida", "no_confirmada", "disparada") and o["rol"] != "entrada":
                cambiar_estado(o, self.ej.cancelar(op["trade_id"], o["rol"], moneda, o.get("oid"), cl), self.log)
        # Verificación en la CUENTA: ninguna orden del bot de esta operación puede seguir abierta.
        cuenta = self.leer()
        oids_op = {o.get("oid") for o in op.get("ordenes", {}).values() if o.get("oid") is not None}
        def restantes(cta):
            return [o for o in cta["ordenes"] if o["moneda"] == moneda and (
                (o.get("cloid") and o["cloid"] in op.get("ordenes", {})) or o.get("oid") in oids_op)]
        if cuenta is None:
            pendientes = ["no se pudo leer la cuenta para verificar las órdenes restantes"]
        else:
            for o in restantes(cuenta):
                cl = o.get("cloid") or self._cloid_por_oid(op, o.get("oid"))
                rol = op["ordenes"].get(cl, {}).get("rol", "stop_sobrante")
                estado = self.ej.cancelar(op["trade_id"], rol, moneda, o.get("oid"), cl)
                if cl in op["ordenes"]:
                    cambiar_estado(op["ordenes"][cl], estado, self.log)
            # La lista de órdenes abiertas manda: si siguen ahí, no están canceladas digan lo que digan.
            cuenta = self.leer() if restantes(cuenta) else cuenta
            pendientes = (["no se pudo leer la cuenta para verificar las órdenes restantes"] if cuenta is None else
                          [o.get("cloid") or str(o.get("oid")) for o in restantes(cuenta)])
        desde = int(op["abierta_ts"] * 1000) - 60_000
        oids = [o.get("oid") for o in op.get("ordenes", {}).values()]
        fills = self.ej.fills(oids=oids, cloids=list(op.get("ordenes", {})), desde_ms=desde)
        roles = {}
        for cl, o in op.get("ordenes", {}).items():
            roles[cl] = o["rol"]
            if o.get("oid") is not None:
                roles[o["oid"]] = o["rol"]
        ent, sal = [], []
        for f in fills or []:
            rol = roles.get(f.get("cloid")) or roles.get(f.get("oid")) or "salida"
            (ent if rol == "entrada" else sal).append(f)
            if rol != "entrada":
                self._registrar_fills(op, [f], rol)
        # Un stop disparado puede ejecutarse con otro oid: si en esta moneda no ha habido nada externo, los fills
        # de cierre de la moneda en la vida de la operación se atribuyen a ella (y se marca la atribución).
        if fills is not None and not sal and moneda not in self.externas_vistas:
            try:
                todos = self.info.user_fills(self.cuenta)
            except Exception:  # noqa: BLE001
                todos = []
            for f in todos:
                if f.get("coin") == moneda and f.get("time", 0) >= desde and str(f.get("dir", "Close")).startswith("Close") \
                        and f not in ent:
                    sal.append(f)
                    self._registrar_fills(op, [f], "salida", atribucion="por moneda y tiempo (stop disparado)")
        fin = int(time.time() * 1000)
        regs = self.ej.funding(int(op["abierta_ts"] * 1000), fin)
        # Un intervalo por cada tamaño que tuvo la posición (tras un cobro parcial el tamaño baja).
        tramos = op.get("tramos_tam") or [[int(op["abierta_ts"] * 1000), op.get("tam_inicial")]]
        intervalos = [{"trade_id": op["trade_id"], "moneda": moneda, "lado": op["lado"], "desde_ms": t0,
                       "hasta_ms": (tramos[k + 1][0] - 1) if k + 1 < len(tramos) else fin, "tam": tam_t}
                      for k, (t0, tam_t) in enumerate(tramos)]
        por_trade, sin_asignar = atribuir_funding(regs or [], intervalos)
        for pago in por_trade.get(op["trade_id"], []):
            self._ev("funding", op, usd=pago["usd"], tasa=pago["tasa"], estimado=False, simulado=pago["simulado"],
                     funding_id=f"{op['trade_id']}:{pago.get('ts', pago.get('time'))}:{pago['usd']}")
        if sin_asignar and regs:
            mios = [p for p in sin_asignar if p["moneda"] == moneda]
            if mios:
                self._ev("alerta", op, severidad="info", mensaje=f"{len(mios)} pagos de funding de {moneda} sin asignar "
                                                                  f"(no coinciden con la posición del bot)")
        res = resultado(ent, sal, por_trade.get(op["trade_id"]), op["lado"], op.get("sl_inicial"), op.get("precio_ref"))
        if causa is None:
            r = res["r_bruto"]
            causa = ("desconocida (sin fills de salida)" if r is None else
                     "objetivo o stop dinámico" if r >= 0.5 else
                     "stop sin pérdidas" if r > -0.5 and op.get("en_breakeven") else "stop-loss")
        datos_fills = "reales" if fills is not None else "no disponibles"
        self._ev("cierre", op, causa=causa, pnl_bruto=res["pnl_bruto"], comisiones=res["comisiones"], funding=res["funding"],
                 pnl_neto=res["pnl_neto"], r_bruto=res["r_bruto"], r_neto=res["r_neto"],
                 riesgo_inicial_usd=res["riesgo_inicial_usd"], precio_medio_entrada=res["precio_medio_entrada"],
                 precio_medio_salida=res["precio_medio_salida"], deslizamiento_entrada_pct=res["deslizamiento_entrada_pct"],
                 datos_fills=datos_fills, ordenes_sin_confirmar=pendientes, cierre_id=f"{op['trade_id']}:cierre")
        if pendientes:
            self.ej.congelar(moneda, "quedan órdenes del bot sin confirmar tras cerrar", "ambigua", op["trade_id"])
            self.avisar(f"⚠️ {moneda}: tras cerrar quedan órdenes del bot sin cancelación confirmada ({len(pendientes)}). "
                        f"Mercado congelado; revisa el incidente.", "error")
        self.ops.pop(moneda, None)
        self.estado.setdefault("enfriamiento", {})[moneda] = time.time() + self.c["enfriamiento_min"] * 60
        self.estado.setdefault("cerradas_recientes", []).append({"trade_id": op["trade_id"], "pnl_neto": res["pnl_neto"],
                                                                 "r_neto": res["r_neto"], "ts": time.time()})
        del self.estado["cerradas_recientes"][:-50]
        if self.memoria is not None and op.get("id_mem") and res["r_neto"] is not None:
            self.memoria.cerrar_real(op["id_mem"], res["r_neto"], causa, pnl_usd=res["pnl_neto"])
            self.memoria.guardar()
        if self.al_cerrar:
            try:
                self.al_cerrar(op, dict(res, datos_fills=datos_fills, ordenes_sin_confirmar=pendientes))
            except Exception as e:  # noqa: BLE001
                self.log.warning("al_cerrar: %s", e)
        pnl = res["pnl_neto"]
        texto = (f"{nombre_op(op['lado'], moneda)}: " +
                 (f"{pnl:+.2f} USD netos ({res['r_neto']:+.2f} R)" if pnl is not None and res["r_neto"] is not None
                  else "resultado desconocido") + f" · {causa}")
        self.log.info("RESULTADO %s", texto)
        self.avisar((("✅ " if (pnl or 0) > 0 else "🔴 ") + "CERRADA " + texto), "info")
        r = self.c["riesgo"]
        if pnl is not None and pnl < 0:
            self.estado["perdidas_seguidas"] = self.estado.get("perdidas_seguidas", 0) + 1
            n = r["pausa_tras_perdidas_seguidas"]
            if n and self.estado["perdidas_seguidas"] >= n:
                self.estado["pausa_hasta"] = time.time() + r["minutos_pausa_tras_racha"] * 60
                self.estado["perdidas_seguidas"] = 0
                self.avisar(f"{n} pérdidas seguidas: pausa de {r['minutos_pausa_tras_racha']} min.", "aviso")
        elif pnl is not None:
            self.estado["perdidas_seguidas"] = 0
        self.guardar()
        return res
