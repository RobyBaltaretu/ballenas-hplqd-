"""
Bot de trading para Hyperliquid — motor de señales propio, flujo de ballenas (investigación) y aprendizaje.

MODOS (config.json "modo"; ver configuracion.py):
  simulado  cuenta ficticia local con precios reales (paper). Es el modo por defecto.
  shadow    solo observa y registra lo que HABRÍA hecho. No crea cliente de órdenes ni necesita claves.
  real      dinero real, solo en una SUBCUENTA dedicada, con habilitación explícita en config y entorno,
            estrategias promovidas por una persona (promocion.py) y los límites del piloto (piloto_real).

Cada ciclo:
  1. Concilia el registro del bot con la cuenta (posiciones, órdenes, stops). Lo que no es del bot es
     EXTERNO: se muestra, no se toca. Divergencias -> mercado congelado y alerta.
  2. Límites duros (pérdida diaria/semanal, caída, nº de operaciones), datos frescos, estado degradado...
     Cualquier bloqueo impide entradas nuevas; las abiertas se siguen gestionando.
  3. Gestiona las abiertas: tiempo máximo, sin pérdidas, stop dinámico (nuevo stop verificado antes de
     quitar el viejo).
  4. Señales nuevas -> filtros -> aprendizaje -> promoción (real) -> tamaño por riesgo -> límites agregados
     -> apertura con CLOID y verificación. Todo queda en el ledger (registro/ledger.jsonl).

Uso:
  python bot.py                     arrancar
  python bot.py --validar-config    solo comprobar la configuración (no conecta, no opera)
  python bot.py --migrar-config     pasar config.json al formato actual (con copia de seguridad)
Parar: Ctrl+C (o señal del sistema): deja de abrir, guarda el estado y conserva los stops en el exchange.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import configuracion as conf
import vigilancia
from aprendizaje import Memoria
from ballenas import FlujoBallenas, moneda_valida
from archivo import Archivo
from cerebro_vivo import CerebroVivo
from contexto_externo import ContextoExterno
from compat import consola_utf8, evitar_suspension, reemplazar
from ejecucion import Ejecutor, EjecutorShadow, ErrorShadow
from estrategia import correlacion, costes, factor_capital
from feed import Feed
from gestor import Gestor, leer_cuenta, nombre_op
from instancia import BloqueoInstancia, InstanciaDuplicada
from ledger import Ledger, nuevo_trade_id
from limitador import Limitador
from mercado import Mercado
from motor import Motor
from notificaciones import Telegram
from observabilidad import Sistema, configurar_logs, ocultar_secretos, traza_actual
from promocion import ErrorHuella, Registro, huella_segura
from riesgo import (
    funding_adverso_frac,
    rebasar_capital,
    actualizar_ventanas,
    bloqueos_duros,
    beta,
    comprobar_agregados,
    costes_excesivos,
    dimensionar,
    exposicion,
    exposicion_beta,
    impacto_pct,
    limites_efectivos,
    suelo_emergencia,
)
from seleccion import URL_LEADERBOARD, elegir_ballenas, monedas_liquidas

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA_CONFIG = os.path.join(AQUI, "config.json")
RUTA_ESTADO = os.path.join(AQUI, "estado.json")
RUTA_MEMORIA = os.path.join(AQUI, "aprendizaje.json")
RUTA_PANEL = os.path.join(AQUI, "panel_datos.json")
RUTA_COMANDOS_PANEL = os.path.join(AQUI, "comandos_panel.json")
RUTA_RESULTADOS_COMANDOS = os.path.join(AQUI, "comandos_resultado.json")
RUTA_PAPEL = os.path.join(AQUI, "papel.json")
RUTA_BALLENAS = os.path.join(AQUI, "ballenas.json")
RUTA_ELEGIDA = os.path.join(AQUI, "estrategia_elegida.json")
DIR_MERCADO = os.path.join(AQUI, "datos_mercado")
DIR_REGISTRO = os.path.join(AQUI, "registro")
DIR_ARCHIVO = os.path.join(AQUI, "datos_archivo")

log = logging.getLogger("bot")
log_op = logging.getLogger("bot.operaciones")     # trading: NO aparece en Observabilidad
log_sis = logging.getLogger("bot.sistema")
SALIDA_DEFINITIVA = 3      # el servicio NO reinicia el bot con este código (config mala, suelo de emergencia)
SALIDA_REINICIO = 4        # reinicio ordenado pedido (p. ej. para aplicar una configuración nueva): reiniciar ya
COMANDOS_PERMITIDOS = ("/estado", "/pausa", "/reanudar", "/cerrarbot", "/informe", "/ayuda")
CONFIRMACION_CIERRE = "CERRAR-BOT"
ORIGEN_POR_MODO = {"simulado": "simulado", "shadow": "shadow", "real": "real"}
LIMITADOR = None          # limitador de peticiones compartido (lo crea crear_cliente)


class ErrorArranque(Exception):
    """Algo impide arrancar con seguridad (el bot se detiene ANTES de crear el cliente de órdenes)."""


# ================================================================ utilidades
def cargar_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return defecto


def guardar_json(ruta, datos):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, default=str)
    reemplazar(tmp, ruta)


def claves_estrategias(intervalos=("1h", "4h")):
    from senales import MODULOS, usa_intervalo
    return {f"{m}_{iv}" for m in MODULOS for iv in intervalos if usa_intervalo(m, iv)} | {"ballenas_1h", "liquidaciones_1h"}


def cargar_config(entorno=None, migrar_disco=True):
    """Lee config.json, lo migra si es de una versión anterior (con copia de seguridad) y lo valida.
    Cualquier problema -> conf.ErrorConfig (no se arranca)."""
    if migrar_disco:
        cambios = conf.migrar_archivo(RUTA_CONFIG)
        for cmb in cambios:
            log_sis.warning("Configuración migrada: %s", cmb)
    c, _ = conf.migrar(conf.cargar(RUTA_CONFIG))
    return conf.validar(c, entorno, modulos=claves_estrategias(("1h", "4h")))


# ================================================================ arranque seguro del modo real
def verificar_subcuenta(info, cfg, cuenta, agente):
    """El modo real solo puede operar una SUBCUENTA dedicada, con una API wallet (agente) de su cuenta maestra.
    Si la respuesta de Hyperliquid no tiene la forma esperada, se bloquea (falla cerrado)."""
    sub = cfg["real"]["subcuenta"].lower()
    if cuenta.lower() != sub:
        raise ErrorArranque("la cuenta del cliente de ejecución no coincide con real.subcuenta")
    try:
        rol = info.user_role(sub)
    except Exception as e:  # noqa: BLE001
        raise ErrorArranque(f"no pude comprobar el tipo de cuenta en Hyperliquid: {e}") from None
    if not isinstance(rol, dict) or rol.get("role") != "subAccount":
        raise ErrorArranque(f"la cuenta configurada no es una subcuenta (Hyperliquid dice: {str(rol)[:80]}). "
                            f"El modo real solo opera una subcuenta dedicada al bot.")
    maestra = str((rol.get("data") or {}).get("master", "")).lower()
    if not maestra.startswith("0x") or maestra == sub:
        raise ErrorArranque("no se pudo identificar la cuenta maestra de la subcuenta")
    try:
        rol_agente = info.user_role(agente.lower())
    except Exception as e:  # noqa: BLE001
        raise ErrorArranque(f"no pude comprobar la API wallet: {e}") from None
    if not isinstance(rol_agente, dict) or rol_agente.get("role") != "agent":
        raise ErrorArranque("la clave configurada no es una API wallet (agente). Nunca uses la clave de la wallet principal.")
    if str((rol_agente.get("data") or {}).get("user", "")).lower() != maestra:
        raise ErrorArranque("la API wallet no pertenece a la cuenta maestra de esta subcuenta")
    return maestra


def verificar_propiedad_inicial(info, cuenta, estado):
    """Antes de operar en real: todo lo que hay en la cuenta debe ser atribuible al registro del bot."""
    from gestor import conciliar
    snap = leer_cuenta(info, cuenta)
    inf = conciliar(estado.get("posiciones", {}), snap)
    problemas = []
    for p in inf["externas"]:
        problemas.append(f"posición externa en {p['moneda']}")
    for o in inf["ordenes_externas"]:
        problemas.append(f"orden externa {o['moneda']} (oid {o.get('oid')})")
    for d in inf["tam_mayor"] + inf["lado_distinto"]:
        problemas.append(f"posición de {d['moneda']} no coincide con el registro")
    return problemas


def estrategias_promovidas(cfg, promociones, motor):
    return [e["clave"] for e in motor.todas_las_estrategias()
            if promociones.autorizada_real(e["clave"], huella_segura(e, cfg))]


# ================================================================ bot
class Bot:
    def __init__(self, c, info, ejecutor, cuenta, url, tg, ledger, sistema, promociones, avisar_inicio=True):
        self.c = c
        self.modo = c["modo"]
        self.origen = ORIGEN_POR_MODO[self.modo]
        self.info, self.ej, self.cuenta, self.url = info, ejecutor, cuenta, url
        self.tg = tg or Telegram(None, None)
        self.ledger, self.sistema, self.promociones = ledger, sistema, promociones
        self.cs, self.f = c["salida"], c["filtros_entrada"]
        universo = info.meta()["universe"]
        self.meta = {a["name"]: a for a in universo if not a.get("isDelisted")}
        self.memoria = Memoria(RUTA_MEMORIA, c["aprendizaje"], c["salida"], costes(c))
        self.estado = self._cargar_estado()
        papel = getattr(ejecutor, "ex", None)
        if self.modo == "simulado" and isinstance(getattr(papel, "d", None), dict):
            ajustes = papel.d.get("ajustes_capital") or []
            delta = rebasar_capital(self.estado, papel.d.get("saldo_inicial"), ajustes[-1]["ts"] if ajustes else None)
            if delta:
                log.info("Capital simulado ajustado en %+.2f USD: las referencias de día/semana/máximo se desplazan "
                         "igual (un depósito no es beneficio).", delta)
        self.parar = threading.Event()
        self.ballenas, self.ultima_seleccion = [], 0.0
        self.monedas = []
        self.flujo = FlujoBallenas()
        self.pos_ballenas, self.ballena_leida_ts, self.ballena_alta_ts = {}, {}, {}
        self.motor = Motor(c, logging.getLogger("bot.motor"), RUTA_ELEGIDA)
        self.mercado = Mercado(c, logging.getLogger("bot.mercado"), DIR_MERCADO)
        self.archivo = Archivo(DIR_ARCHIVO, c["archivo"], logging.getLogger("bot.archivo")) if c["archivo"]["activo"] else None
        # Binance y Bybit: SOLO datos públicos de mercado para informe e investigación (nunca opera allí).
        self.ctx_externo = ContextoExterno(c.get("contexto_externo"), DIR_MERCADO, logging.getLogger("bot.externo"))
        # El cerebro: ÚNICO que decide qué candidata se opera (la protección va después, igual que siempre).
        self.cerebro = CerebroVivo(c.get("cerebro"), os.path.join(AQUI, "cerebro_modelo.json"), logging.getLogger("bot.cerebro"))
        self.gestor = None
        if self.modo != "shadow":
            self.gestor = Gestor(c, ejecutor, info, cuenta, ledger, self.estado, self.meta, log_op, self.origen,
                                 self.avisar, self.guardar, self.memoria)
            self.gestor.al_cerrar = self._evidencia_cierre
        self._seleccion_pendiente, self._hilo_seleccion = None, None
        self._ultimo_despertar = 0.0
        self.vetadas_avisadas = set()
        self.despertar = threading.Event()
        self.ws_info, self.ws_subs, self.ws_ultimo_msg, self.ws_reintento = None, {}, None, 0.0
        self.ws_espera = 30.0
        self.mids, self.mids_ts, self.marks, self.marks_ts = {}, None, {}, {}
        self.reloj_desfase = None
        self.alertas = []                      # (ts, severidad, mensaje, trade_id) — operativas
        self.bloqueos = []
        self.conciliacion_ok = self.modo == "shadow"
        self.metricas = {"senales": 0, "aprobadas": 0, "abiertas": 0, "rechazadas": 0, "intenciones_shadow": 0}
        self.ultimo_riesgo = (False, ["arrancando"])
        self.latido_url, self.latido_ts, self.latido_hilo = "", 0.0, None
        self.vigilancia, self.vigilancia_ts = {}, 0.0
        # Feed de «Señales en vivo» (persistente, para el panel): recibe lo que el bot hace (vía el ledger) y lo que
        # observa del mercado y de las ballenas.
        self.feed = Feed(DIR_REGISTRO, c.get("feed"))
        if getattr(ledger, "observador", "no") is None:
            ledger.observador = self.feed.desde_ledger
        self.mercado.observador_grandes = self._feed_operacion_grande
        self.fuentes = {}                       # nombre -> True (ok) / False (perdida): para avisar solo en cambios
        self.fills_ballena_vistos = deque(maxlen=5000)
        self.ballena_fallos = {}                # ballena -> fallos de consulta seguidos
        self.ballena_pos_usd = {}               # ballena -> {moneda: tamaño con signo en USD} (posición OBSERVADA)
        self.ws_fills_recibidos = 0
        mi = self.motor.info_lab
        log_sis.info("Modo %s · estrategias: %s · %d con evidencia de laboratorio", self.modo.upper(), mi["fuente"],
                     mi["validadas"])

    # ------------------------------------------------------------ estado
    def _cargar_estado(self):
        e = cargar_json(RUTA_ESTADO, {})
        if e.get("modo") and e["modo"] != self.modo:
            # Cambio de modo: lo del otro modo (saldo, posiciones, límites) no vale aquí. Se ARCHIVA, no se borra.
            marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            guardar_json(os.path.join(AQUI, f"estado-{e['modo']}-{marca}.json"), e)
            log_sis.warning("Cambio de modo %s -> %s: archivo el estado anterior y empiezo de cero (la memoria "
                            "de aprendizaje se conserva).", e["modo"], self.modo)
            e = {}
        e["modo"] = self.modo
        for k, v in (("posiciones", {}), ("enfriamiento", {}), ("perdidas_seguidas", 0), ("pausa_hasta", 0),
                     ("pausa_manual", False), ("shadow_abiertas", {})):
            e.setdefault(k, v)
        e.pop("visto", None)
        # Posiciones guardadas por versiones anteriores (sin trade_id ni CLOID): propiedad incierta.
        for m, d in list(e["posiciones"].items()):
            if "trade_id" not in d:
                e.setdefault("posiciones_legado", {})[m] = e["posiciones"].pop(m)
                log_sis.warning("%s: registro antiguo sin identificadores: queda como propiedad INCIERTA (no se toca).", m)
        return e

    def guardar(self):
        guardar_json(RUTA_ESTADO, self.estado)

    def avisar(self, texto, severidad="info", trade_id=None):
        texto = ocultar_secretos(texto)
        if severidad in ("aviso", "error"):
            self.alertas.append((time.time(), severidad, texto, trade_id))
            del self.alertas[:-200]
        (log_op.error if severidad == "error" else log_op.warning if severidad == "aviso" else log_op.info)(texto)
        if severidad == "error":
            self.tg.error(texto[:40], "⚠️ " + texto, cada_min=10)
        else:
            self.tg.enviar(texto)

    # ------------------------------------------------------------ cuenta y precios
    # ------------------------------------------------------------ feed (panel «Señales en vivo»)
    def _fuente(self, nombre, ok, detalle=""):
        """Publica en el feed SOLO los cambios de estado de una fuente (perdida / recuperada)."""
        antes = self.fuentes.get(nombre)
        if antes == ok:
            return
        self.fuentes[nombre] = ok
        if antes is None and ok:
            return
        try:
            self.feed.publicar("fuente_recuperada" if ok else "fuente_perdida", fuente=nombre,
                               estado="ok" if ok else "perdida", detalle=detalle)
        except Exception:  # noqa: BLE001
            pass

    def _feed_operacion_grande(self, g):
        self.feed.publicar("operacion_grande", ts_exchange=g.get("t"), moneda=g.get("moneda"),
                           lado="compra" if g.get("lado") == "B" else "venta", fuente="websocket trades (público)",
                           precio=float(g["px"]) if g.get("px") else None, tipo_precio="trade", estado="observado",
                           detalle="operación agresiva grande: NO revela la posición neta de ninguna cuenta",
                           datos={"usd": g.get("usd"), "cuentas": [conf.enmascarar_direccion(x) for x in g.get("cuentas") or []]},
                           frescura_seg=round(time.time() - g["t"], 1) if g.get("t") else None)

    def leer_precios(self):
        t0 = time.time()
        try:
            self.mids = {k: float(v) for k, v in self.info.all_mids().items()}
            self.mids_ts = time.time()
            self.sistema.marcar("api_rest", "correcto", "precios leídos", latencia_ms=round((time.time() - t0) * 1000))
            self.sistema.marcar("datos_mercado", "correcto", f"{len(self.mids)} precios", ts_dato=self.mids_ts)
            self.sistema.resolver("api_rest", "precios")
            self._fuente("precios (API REST)", True)
        except Exception as e:  # noqa: BLE001
            self._fuente("precios (API REST)", False, f"no pude leer precios: {type(e).__name__}")
            self.sistema.marcar("api_rest", "fallido", f"no pude leer precios: {type(e).__name__}")
            self.sistema.incidente("api_rest", "precios", "error", f"fallo al leer precios: {e}",
                                   "Sin precios actuales el bot no puede decidir ni proteger bien.",
                                   "Comprueba la conexión a internet y el estado de Hyperliquid.")
            self.sistema.contadores["errores_api"] += 1
        viejos = bool(self.mids_ts) and time.time() - self.mids_ts > self.c["riesgo"]["datos_max_antiguedad_seg"]
        if viejos:
            self.sistema.marcar("datos_mercado", "desactualizado", f"precios de hace {time.time() - self.mids_ts:.0f} s")
        if viejos != getattr(self, "_precios_viejos", False):
            self._precios_viejos = viejos
            if viejos:
                self.feed.publicar("datos_obsoletos", fuente="precios", estado="obsoleto",
                                   frescura_seg=round(time.time() - self.mids_ts, 1),
                                   detalle="precios más viejos que el máximo: no se abren operaciones")

    def datos_frescos(self):
        return self.mids_ts is not None and time.time() - self.mids_ts <= self.c["riesgo"]["datos_max_antiguedad_seg"]

    def equity(self):
        """Valor de cuenta para calcular tamaños. En real se limita al capital del piloto."""
        if self.modo == "shadow":
            return self.c["shadow"]["saldo_teorico_usd"], self.c["shadow"]["saldo_teorico_usd"]
        cta = self.gestor.ultima_cuenta if self.gestor else None
        if cta is None:
            return None, None
        eq, libre = cta["equity"], cta["libre"]
        if self.modo == "real":
            tope = self.c["piloto_real"]["capital_max_usd"]
            eq, libre = min(eq, tope), min(libre, tope)
        return eq, libre

    # ------------------------------------------------------------ ballenas (opcional, aislado)
    def _post(self, cuerpo, peso):
        time.sleep(peso * 60 / 900)
        return self.info.post("/info", cuerpo)

    @staticmethod
    def _leaderboard():
        return requests.get(URL_LEADERBOARD, timeout=30).json().get("leaderboardRows", [])

    def _seleccionar(self):
        monedas = monedas_liquidas(self._post, self.c, log_sis)
        ballenas = elegir_ballenas(self.c, self._post, self._leaderboard, monedas, logging.getLogger("bot.ballenas"),
                                   RUTA_BALLENAS)
        return monedas, ballenas

    def _aplicar_seleccion(self, monedas, ballenas):
        if not ballenas:
            self.sistema.marcar("ballenas", "degradado", "la selección no encontró ballenas; mantengo las anteriores")
            return
        self.monedas = [m for m in monedas if m in self.meta]
        ballenas = list(dict.fromkeys(self.motor.info_lab["ballenas_predictivas"] + ballenas))
        for b in ballenas:
            self.ballena_alta_ts.setdefault(b, time.time())
        self.ballenas = ballenas
        self.ultima_seleccion = time.time()
        log_sis.info("Siguiendo %d ballenas.", len(ballenas))
        self.actualizar_websocket()

    def actualizar_ballenas(self):
        cb = self.c["ballenas"]
        if not cb.get("activo", True):
            self.sistema.marcar("ballenas", "desactivado", "desactivado en config")
            return
        try:
            if self._seleccion_pendiente is not None:
                monedas, ballenas = self._seleccion_pendiente
                self._seleccion_pendiente = None
                self._aplicar_seleccion(monedas, ballenas)
            if self.ballenas and time.time() - self.ultima_seleccion < self.c["refrescar_ballenas_horas"] * 3600:
                return
            if self._hilo_seleccion is None or not self._hilo_seleccion.is_alive():
                def tarea():
                    try:
                        self._seleccion_pendiente = self._seleccionar()
                    except Exception as e:  # noqa: BLE001
                        self.sistema.incidente("ballenas", "seleccion", "aviso", f"falló la selección de ballenas: {e}",
                                               "Es una función opcional: el resto del bot sigue.",
                                               "Revisa la conexión; se reintentará sola.")
                self.ultima_seleccion = time.time()
                self._hilo_seleccion = threading.Thread(target=tarea, daemon=True)
                self._hilo_seleccion.start()
        except Exception as e:
            if cb.get("requerido"):
                raise
            self.sistema.marcar("ballenas", "fallido", f"{type(e).__name__}: {e}")

    def leer_ballenas(self):
        if not self.ballenas or not self.mids:
            if self.c["ballenas"].get("activo", True) and not self.ballenas:
                self.sistema.marcar("ballenas", "sin_datos", "todavía no hay ballenas seleccionadas")
            return
        ahora = time.time()
        precios = {m: px for m, px in self.mids.items() if moneda_valida(m)}
        fallos = 0
        for b in self.ballenas:
            if self.memoria.vetada(b):
                if b not in self.vetadas_avisadas:
                    n, media = self.memoria.reputacion(b)
                    log_sis.warning("Ballena %s vetada: sus señales dieron %+.2f R de media en %d casos.",
                                    conf.enmascarar_direccion(b), media, n)
                    self.vetadas_avisadas.add(b)
                continue
            try:
                st = self.info.user_state(b)
            except Exception:  # noqa: BLE001
                fallos += 1
                self.ballena_fallos[b] = self.ballena_fallos.get(b, 0) + 1
                continue
            self.ballena_fallos[b] = 0
            pos = {}
            for p in st.get("assetPositions", []):
                szi = float(p["position"]["szi"])
                if szi:
                    pos[p["position"]["coin"]] = szi
            self.pos_ballenas[b] = {m: ("L" if s > 0 else "S") for m, s in pos.items()}
            self.ballena_pos_usd[b] = {m: round(s * precios[m]) for m, s in pos.items() if m in precios}
            minimo = self.c["feed"]["min_cambio_ballena_usd"]
            for m, delta, px in self.flujo.foto(ahora, b, pos, precios):
                if abs(delta) >= minimo:
                    self.feed.publicar("ballena_cambio_inferido", ts_recepcion=ahora, moneda=m,
                                       lado="aumenta largo / reduce corto" if delta > 0 else "aumenta corto / reduce largo",
                                       fuente="foto de posiciones (clearinghouseState)", precio=px, tipo_precio="mid",
                                       estado="inferido",
                                       detalle="cambio INFERIDO por diferencia entre dos fotos de posición; no es un fill",
                                       datos={"delta_usd": round(delta), "cuenta": conf.enmascarar_direccion(b)})
            self.ballena_leida_ts[b] = ahora
        self._fuente("ballenas (posiciones)", fallos < len(self.ballenas),
                     "no se pudo leer ninguna ballena" if fallos == len(self.ballenas) else "")
        if fallos == len(self.ballenas):
            self.sistema.marcar("ballenas", "fallido", "no se pudo leer ninguna ballena")
        elif fallos:
            self.sistema.marcar("ballenas", "degradado", f"{fallos} de {len(self.ballenas)} ballenas sin leer")
        else:
            self.sistema.marcar("ballenas", "correcto", f"{len(self.ballenas)} ballenas leídas", ts_dato=ahora)

    def ballenas_frescas(self):
        lim = self.c["ballenas"]["datos_max_antiguedad_seg"]
        frescas = [b for b in self.ballenas if time.time() - self.ballena_leida_ts.get(b, 0) <= lim]
        return bool(self.ballenas) and len(frescas) >= max(1, len(self.ballenas) // 2)

    # ------------------------------------------------------------ websocket (opcional, con vigilancia)
    def _al_recibir_fill(self, msg):
        recibido = time.time()
        self.ws_ultimo_msg = recibido
        datos = msg.get("data") or {}
        if datos.get("isSnapshot"):
            return                         # la foto inicial es histórica: no son fills nuevos
        cuenta = conf.enmascarar_direccion(datos.get("user") or "")
        minimo = self.c["feed"]["min_fill_ballena_usd"]
        for f in datos.get("fills", []):
            fid = f.get("tid") or f"{f.get('oid')}-{f.get('time')}-{f.get('sz')}"
            if fid in self.fills_ballena_vistos:
                continue                    # deduplicado (reconexiones, mensajes repetidos)
            self.fills_ballena_vistos.append(fid)
            self.ws_fills_recibidos += 1
            try:
                usd = float(f.get("sz", 0)) * float(f.get("px", 0))
            except (TypeError, ValueError):
                continue
            if usd < minimo:
                continue
            self.feed.publicar("ballena_fill", ts_exchange=(f.get("time") or 0) / 1000 or None, ts_recepcion=recibido,
                               moneda=f.get("coin"), lado="compra" if f.get("side") == "B" else "venta",
                               fuente="websocket userFills", precio=float(f["px"]), tipo_precio="fill",
                               tam=float(f["sz"]), estado="observado",
                               frescura_seg=round(recibido - f["time"] / 1000, 1) if f.get("time") else None,
                               detalle=f.get("dir"), datos={"usd": round(usd), "cuenta": cuenta, "dir": f.get("dir"),
                                                            "crossed": f.get("crossed")})
        notional = sum(float(f.get("sz", 0)) * float(f.get("px", 0)) for f in datos.get("fills", []))
        if notional < self.c["ballenas"]["aumento_min_usd"] / 5 or time.time() - self._ultimo_despertar < 10:
            return
        self._ultimo_despertar = time.time()
        self.despertar.set()

    def _ws(self):
        if not self.c["tiempo_real"]["websocket"] or not self.url:
            return None
        if self.ws_info is None:
            if time.time() < self.ws_reintento:
                return None
            from hyperliquid.info import Info
            self.ws_info = Info(self.url, skip_ws=False)
            self.ws_ultimo_msg = time.time()
        return self.ws_info

    def actualizar_websocket(self):
        cfg = self.c["tiempo_real"]
        if not cfg["websocket"] or not self.url:
            self.sistema.marcar("websocket", "desactivado", "tiempo real desactivado en config")
            return
        try:
            if self._ws() is None:
                return
            objetivo = self.ballenas[: min(10, cfg["max_ballenas_ws"])]     # límite de Hyperliquid: 10 usuarios por IP
            for b in list(self.ws_subs):
                if b not in objetivo:
                    self.ws_info.unsubscribe({"type": "userFills", "user": b}, self.ws_subs.pop(b))
            for b in objetivo:
                if b not in self.ws_subs:
                    self.ws_subs[b] = self.ws_info.subscribe({"type": "userFills", "user": b}, self._al_recibir_fill)
        except Exception as e:  # noqa: BLE001
            self._reconectar_ws(f"no se pudo suscribir: {e}")

    def _reconectar_ws(self, motivo):
        """Cierra el websocket y programa la reconexión con espera creciente (30 s -> 10 min)."""
        try:
            if self.ws_info is not None and hasattr(self.ws_info, "disconnect_websocket"):
                self.ws_info.disconnect_websocket()
        except Exception:  # noqa: BLE001
            pass
        self.ws_info, self.ws_subs = None, {}
        self.mercado.subs = {}
        self.ws_reintento = time.time() + self.ws_espera
        self.ws_espera = min(self.ws_espera * 2, 600)
        self.sistema.contadores["reconexiones_ws"] += 1
        self.sistema.marcar("websocket", "degradado", f"reconectando: {motivo}")
        try:
            self.feed.publicar("reconexion", fuente="websocket", estado="reconectando", detalle=motivo)
        except Exception:  # noqa: BLE001
            pass
        self.sistema.incidente("websocket", "conexion", "aviso", f"websocket perdido: {motivo}",
                               "Sin tiempo real, el bot sigue consultando cada pocos segundos (algo más lento).",
                               "Se reconecta solo; si persiste, revisa la conexión a internet.")

    def vigilar_ws(self):
        if not self.c["tiempo_real"]["websocket"] or not self.url:
            return
        ultimo = max(self.ws_ultimo_msg or 0, getattr(self.mercado, "ultimo_mensaje", 0) or 0)
        if self.ws_info is not None and (self.ws_subs or self.mercado.subs):
            silencio = time.time() - ultimo
            if silencio > self.c["tiempo_real"]["silencio_max_seg"]:
                self._reconectar_ws(f"{silencio:.0f} s sin mensajes")
            else:
                self.ws_espera = 30.0
                self.sistema.marcar("websocket", "correcto", f"{len(self.ws_subs)} ballenas y {len(self.mercado.subs)} "
                                    f"mercados en tiempo real", ts_dato=ultimo)
                self.sistema.resolver("websocket", "conexion")
        elif self.ws_info is None and time.time() >= self.ws_reintento:
            self.actualizar_websocket()

    # ------------------------------------------------------------ bloqueos de entrada
    def bloqueos_entrada(self, equity):
        motivos = []
        if self.estado.get("pausa_manual"):
            motivos.append("pausa manual")
        if not self.datos_frescos():
            motivos.append("datos de precios obsoletos o no disponibles")
        if self.ej.degradado:
            motivos.append(f"ejecución degradada: {self.ej.degradado}")
        if not self.conciliacion_ok:
            motivos.append("conciliación con el exchange pendiente o fallida")
        if self.gestor and self.gestor.sin_stop:
            motivos.append("posiciones del bot sin stop verificado: " + ", ".join(self.gestor.sin_stop))
        if self.gestor and self.gestor.riesgo_liquidacion:
            motivos.append("riesgo de liquidación/margen: " + "; ".join(f"{m}: {x}" for m, x in
                                                                         self.gestor.riesgo_liquidacion.items()))
        if self.gestor:
            cta = self.gestor.ultima_cuenta
            lim_cta = self.c["riesgo"].get("cuenta_max_antiguedad_seg", 90)
            if cta is None or time.time() - cta.get("ts", 0) > lim_cta:
                motivos.append("la lectura de la cuenta no es reciente: saldo/equity sin reconciliar")
        if self.estado.get("posiciones_legado") and self.modo == "real":
            motivos.append("hay posiciones de una versión anterior con propiedad incierta (revísalas)")
        if self.reloj_desfase is not None and abs(self.reloj_desfase) > 10:
            motivos.append(f"reloj desfasado {self.reloj_desfase:.0f} s respecto al exchange")
        lim = limites_efectivos(self.c)
        motivos += bloqueos_duros(self.estado, equity, self.c, lim, time.time())
        if self.modo == "real":
            perdida = (self.estado.get("saldo_inicial") or 0) - (equity or 0)
            if perdida >= self.c["piloto_real"]["perdida_max_usd"]:
                motivos.append(f"pérdida del piloto {perdida:.2f} USD ≥ {self.c['piloto_real']['perdida_max_usd']} USD")
        return motivos

    # ------------------------------------------------------------ evaluación de una señal
    def evaluar(self, cand):
        """Filtros de ejecución. Devuelve (fallos, plan, spread)."""
        moneda, lado = cand["moneda"], cand["lado"]
        s = 1 if lado == "L" else -1
        r = self.c["riesgo"]
        precio = self.mids.get(moneda)
        fallos, spread = [], None
        sl_pct = cand["sl_pct"]
        if not (r["sl_min_pct"] <= sl_pct <= r["sl_max_pct"]):
            fallos.append(f"stop {sl_pct:.2f}% fuera de límites ({r['sl_min_pct']}–{r['sl_max_pct']}%)")
        if precio is None:
            fallos.append("sin precio actual")
        else:
            avance_r = s * (precio - cand["precio_senal"]) / cand["precio_senal"] * 100 / sl_pct
            if avance_r > self.c["motor"]["persecucion_max_r"]:
                fallos.append(f"el precio ya avanzó {avance_r:.2f} R desde la señal")
        try:
            l2 = self.info.l2_snapshot(moneda)
            libro = l2["levels"]
            if l2.get("time"):
                # Desfase del reloj local frente al del exchange (incluye la latencia de la red, pequeña).
                self.reloj_desfase = time.time() - float(l2["time"]) / 1000
            bid, ask = float(libro[0][0]["px"]), float(libro[1][0]["px"])
            spread = (ask - bid) / ((ask + bid) / 2) * 100
            if spread > r["spread_max_pct"]:
                fallos.append(f"horquilla {spread:.3f}% demasiado ancha")
            lado_libro = libro[1] if lado == "L" else libro[0]
            profundidad = sum(float(x["px"]) * float(x["sz"]) for x in lado_libro[:5])
            niveles = [(float(x["px"]), float(x["sz"])) for x in lado_libro]
        except Exception:  # noqa: BLE001
            fallos.append("no pude leer el libro de órdenes")
            profundidad, niveles = None, None
        caro = costes_excesivos(self.c, sl_pct, spread or 0.0)
        if caro:
            fallos.append(caro)
        if cand["rasgos"].get("funding_contra", 0) > self.f["funding_max_en_contra_pct"]:
            fallos.append(f"funding {cand['rasgos']['funding_contra']:.4f}%/h en contra")
        ctx = self.motor.contextos.get((moneda, "1h"))
        if ctx and ctx[1].atr[-1]:
            c1 = ctx[1]
            rango = (c1.h[-1] - c1.l[-1]) / c1.atr[-1]
            if rango > r.get("volatilidad_extrema_atr", 4):
                fallos.append(f"volatilidad extrema: la última vela midió {rango:.1f} ATR")
        cb = self.c["ballenas"]
        if cand["modulo"] != "ballenas" and cb.get("usar_como_veto") and self.ballenas_frescas():
            contra = self.flujo.flujo(moneda, lado, time.time(), cb["ventana_min"] * 60, self.motor.predictivas)
            if contra <= -cb["veto_usd"]:
                fallos.append(f"veto de ballenas: cargan {-contra / 1000:.0f}k USD en contra")
        dur = min(cand["duracion_min"], r["duracion_max_global_h"] * 60)
        plan = {"moneda": moneda, "lado": lado, "precio": precio, "sl_pct": sl_pct, "duracion_min": dur, "duracion_estrategia_min": cand["duracion_min"],
                "salida": cand["salida"], "estrategia": cand["estrategia"], "modulo": cand["modulo"],
                "version": cand.get("version"), "motivo": cand["motivo"], "precio_ref": cand["precio_senal"],
                "origen": self.origen, "profundidad_usd": profundidad, "_niveles": niveles}
        return fallos, plan, spread

    def correlada(self, moneda, lado):
        c_nueva = self.motor.contextos.get((moneda, "1h"))
        if not c_nueva:
            return None
        abiertas = list(self.estado["posiciones"].values()) + list(self.estado["shadow_abiertas"].values())
        for d in abiertas:
            if d["lado"] != lado or d["moneda"] == moneda:
                continue
            c_otra = self.motor.contextos.get((d["moneda"], "1h"))
            if not c_otra:
                continue
            corr = correlacion(c_nueva[1].v, c_otra[1].v)
            if corr is not None and corr > self.c["riesgo"]["correlacion_max"]:
                return f"correlada {corr:.2f} con {d['moneda']} ya abierta"
        return None

    # ------------------------------------------------------------ ciclo
    def ciclo(self):
        traza_actual.set(f"ciclo-{self.sistema.ciclos + 1}")
        self.actualizar_ballenas()
        self.leer_precios()
        if self.gestor:
            inf = self.gestor.conciliar(self.mids)
            self.conciliacion_ok = inf is not None
            if inf is None:
                self.sistema.marcar("conciliacion", "fallido", "no pude leer la cuenta")
            else:
                legado = self.estado.get("posiciones_legado") or {}
                for m in [m for m in legado if m not in self.gestor.ultima_cuenta["posiciones"]]:
                    legado.pop(m)
                    log_sis.info("%s: la posición de la versión anterior ya no existe en la cuenta; deja de bloquear.", m)
                if not legado:
                    self.estado.pop("posiciones_legado", None)
                detalle = (f"{len(inf['propias'])} del bot · {len(inf['externas'])} externas · "
                           f"{len(inf['ordenes_externas'])} órdenes externas")
                malo = inf["tam_mayor"] or inf["lado_distinto"] or self.gestor.sin_stop
                self.sistema.marcar("conciliacion", "degradado" if malo else "correcto", detalle)
                self.sistema.marcar("ejecucion", "degradado" if self.ej.degradado else "correcto",
                                    self.ej.degradado or "sin incidencias")
        eq, _ = self.equity()
        if eq:
            if actualizar_ventanas(self.estado, eq):
                log_op.info("Nuevo día (UTC). Valor de cuenta: %.2f USD", eq)
            if self.gestor and suelo_emergencia(self.estado, eq, self.c):
                self.emergencia()
        self.leer_ballenas()
        self.vigilar_ws()
        # Gestión de las abiertas del bot
        if self.gestor:
            for op in list(self.estado["posiciones"].values()):
                px = self.marks.get(op["moneda"]) if self.cs.get("precio_trailing") == "mark" else self.mids.get(op["moneda"])
                try:
                    self.gestor.gestionar(op, px)
                except ErrorShadow:
                    raise
                except Exception as e:
                    log_op.exception("Error gestionando %s: %s", op["moneda"], e)
        self.actualizar_shadow()
        self.vigilar_estrategias()
        for r in self.memoria.actualizar_sombras(self.mids):
            if r["tipo"] == "shadow":
                self._cerrar_intencion_shadow(r)
            elif r["tipo"] == "aspirante":
                self._cerrar_aspirante(r)
        self.memoria.guardar()
        self.guardar()
        # Señales nuevas
        if self.motor.recargar_si_cambia():
            log_sis.info("Nuevo resultado del laboratorio (solo evidencia; no autoriza real).")
        self.motor.actualizar_universo(self.info, self.meta)
        self.motor.preparar_funding()
        self.marks = {m: v[-1][2] for m, v in self.mercado.ctx.items() if v}
        self.marks_ts = {m: v[-1][0] for m, v in self.mercado.ctx.items() if v}
        self.mercado.foto(self.info, self.motor.monedas)
        self.sistema.marcar("grabacion_mercado", "correcto" if self.mercado.ctx else "sin_datos",
                            f"{self.mercado.trades_recibidos} operaciones recibidas")
        try:
            self.mercado.suscribir(self._ws(), self.motor.monedas)
        except Exception as e:  # noqa: BLE001
            self._reconectar_ws(f"suscripción al mercado: {e}")
        candidatas = self.motor.nuevas_senales(self.info, self.flujo if self.ballenas_frescas() else None)
        self.archivar()
        if self.ballenas_frescas():
            candidatas += self.motor.senales_ballenas(self.info, self.flujo)
        for cand in self.mercado.senales_liquidaciones(self.motor.monedas, lambda m: self.motor.contexto(self.info, m, "1h")):
            self.feed.publicar("liquidacion_inferida", moneda=cand["moneda"], lado=cand["lado"], estrategia=cand.get("estrategia"),
                               fuente="interés abierto y precio (grabado por el bot)", precio=cand.get("precio_senal"),
                               tipo_precio="mid", estado="inferido", detalle=cand.get("motivo"),
                               datos={"etiqueta": "INFERENCIA: no es un evento de liquidación confirmado"})
            ctx = cand.pop("ctx")
            cand["rasgos"] = self.motor.rasgos(ctx, len(ctx) - 1, cand["lado"], cand["moneda"], cand["fuerza"],
                                               self.flujo, None, time.time())
            candidatas.append(cand)
        if candidatas:
            self.motor.refrescar_funding(self.info)
            self.procesar_candidatas(candidatas)

    def procesar_candidatas(self, candidatas):
        eq, libre = self.equity()
        bloqueos = self.bloqueos_entrada(eq)
        self.bloqueos = bloqueos
        lim = limites_efectivos(self.c)
        relajado = self.horas_sin_operar() >= self.c["gestion_capital"]["horas_sin_operar_para_relajar"]
        explorar = self.modo == "simulado" and self.c["aprendizaje"].get("explorar_en_simulado", True)
        aprobadas = []
        for cand in candidatas:
            moneda, lado, clave = cand["moneda"], cand["lado"], cand["estrategia"]
            if moneda not in self.meta:
                continue
            if cand.get("aspirante"):
                self._seguir_aspirante(cand)       # solo observación: ni órdenes, ni límites, ni aprendizaje
                continue
            self.metricas["senales"] += 1
            tid = nuevo_trade_id()
            cand["rasgos"]["funding_contra"] = round((1 if lado == "L" else -1) * self.motor.funding_actual.get(moneda, 0.0), 6)
            cand["rasgos"].update(self.mercado.rasgos(moneda, lado))
            contexto = {"rasgos": cand["rasgos"], "vela_ts": cand.get("vela_ts"), "intervalo": cand.get("intervalo"),
                        "mid": self.mids.get(moneda), "mark": self.marks.get(moneda),
                        "evidencia_ballenas": cand.get("evidencia_ballenas"), "etiqueta": cand.get("etiqueta")}
            self.ledger.registrar("senal", tid, self.origen, moneda=moneda, lado=lado, estrategia=clave,
                                  version=cand.get("version"), motivo=cand["motivo"], precio=cand["precio_senal"],
                                  sl_pct=cand["sl_pct"], contexto=contexto)
            fallos, plan, spread = self.evaluar(cand)
            plan["trade_id"] = tid
            val = None
            # Veredicto del aprendizaje para TODAS las señales (se guarde o no la operación): así se puede medir
            # después si las que habría aceptado salen mejor que las que habría rechazado.
            juicio = self.memoria.valorar(moneda, cand["rasgos"], modulo=clave, medir=True)
            plan["aprendizaje"] = {"abrir": juicio["abrir"], "r_esperado": juicio.get("r_esperado"), "n": juicio["n"],
                                   "juzgada": juicio.get("r_esperado") is not None}
            self.cerebro.recargar()
            dec = self.cerebro.decidir(cand, self.motor, self.ctx_externo)
            plan["cerebro"] = {k: dec[k] for k in ("abrir", "prediccion", "motivo", "version", "rasgos")}
            if cand.get("solo_investigacion"):
                fallos = ["solo investigación: hipótesis sin validar (no opera)"] + fallos
            if not fallos and self.cerebro.decide:
                if dec["abrir"]:
                    val = {"abrir": True, "factor": 1.0, "texto": dec["motivo"], "r_esperado": dec["prediccion"]}
                else:
                    fallos = [dec["motivo"]]
            elif not fallos:
                val = self.memoria.valorar(moneda, cand["rasgos"], modulo=clave, relajado=relajado)
                if not val["abrir"]:
                    if explorar:
                        val = dict(val, abrir=True, factor=self.c["aprendizaje"].get("factor_exploracion", 0.5),
                                   texto=val["texto"] + " · explorando (simulado)")
                        plan["explorando"] = True
                    else:
                        fallos = ["aprendizaje: " + val["texto"]]
            if not fallos and self.modo == "real":
                d = self.motor.definicion(clave)
                if not d or not self.promociones.autorizada_real(clave, self._huella(d)):
                    fallos = ["sin promoción vigente para real (promocion.py)"]
                elif moneda not in self.c["piloto_real"]["mercados"]:
                    fallos = [f"{moneda} no está en los mercados del piloto real"]
            ocupada = moneda in self.estado["posiciones"] or any(
                d["moneda"] == moneda for d in self.estado["shadow_abiertas"].values()) or (
                self.gestor and self.gestor.ultima_cuenta and moneda in self.gestor.ultima_cuenta["posiciones"])
            if not fallos:
                if bloqueos:
                    fallos = ["entradas bloqueadas: " + "; ".join(bloqueos)]
                elif moneda in self.ej.congelados:
                    fallos = [f"{moneda} congelado: {self.ej.congelados[moneda]['motivo']}"]
                elif ocupada:
                    fallos = ["ya hay una posición en esa moneda"]
                elif time.time() < self.estado["enfriamiento"].get(moneda, 0):
                    fallos = ["enfriamiento tras cerrar en esa moneda"]
            dim = None
            if not fallos:
                f_cap, motivos_cap, _ = self.factor_capital(eq)
                factor = min(1.0, (val or {}).get("factor", 1.0) * f_cap)
                factor *= self.estado.get("factor_estrategia", {}).get(clave, 1.0)   # vigilancia: solo reduce
                fund = funding_adverso_frac((cand.get("rasgos") or {}).get("funding_contra"),
                                            cand.get("duracion_min") or plan.get("duracion_min"))
                plan["funding_previsto_pct"] = round(fund * 100, 4)
                dim = dimensionar(eq, libre, plan["precio"], plan["sl_pct"], self.meta[moneda]["szDecimals"],
                                  self.meta[moneda].get("maxLeverage", 1), self.c, spread or 0.0, factor, limites=lim,
                                  funding_frac=fund)
                if not dim["ok"]:
                    fallos = [dim["motivo"]]
                elif plan.get("profundidad_usd") is not None and plan["profundidad_usd"] < 3 * dim["notional"]:
                    fallos = [f"liquidez insuficiente: {plan['profundidad_usd']:.0f} USD en el libro"]
                else:
                    # Impacto real del tamaño en el libro (entrada) y el mismo supuesto a la salida: entra en los
                    # costes del tamaño. Si el libro no cubre el tamaño, no se entra.
                    imp = impacto_pct(plan.get("_niveles") or [], dim["notional"])
                    if imp is None:
                        fallos = ["el libro no tiene profundidad suficiente para ese tamaño"]
                    elif imp > self.c["riesgo"].get("impacto_max_pct", 0.15):
                        fallos = [f"impacto estimado {imp:.3f}% > {self.c['riesgo'].get('impacto_max_pct', 0.15)}%"]
                    else:
                        plan["impacto_pct"] = round(imp, 4)
                        dim = dimensionar(eq, libre, plan["precio"], plan["sl_pct"], self.meta[moneda]["szDecimals"],
                                          self.meta[moneda].get("maxLeverage", 1), self.c, (spread or 0.0) + 2 * imp,
                                          factor, limites=lim, funding_frac=fund)
                        if not dim["ok"]:
                            fallos = [dim["motivo"]]
                if not fallos:
                    plan.update(tam=dim["tam"], apalancamiento=dim["apalancamiento"], riesgo_usd=dim["riesgo_usd"],
                                notional=dim["notional"], factor=factor, motivos_capital=motivos_cap)
            plan.pop("_niveles", None)
            decision = {"aprobada": not fallos, "motivos": fallos, "parametros": cand.get("parametros"),
                        "plan": {k: plan.get(k) for k in ("tam", "apalancamiento", "riesgo_usd", "notional", "sl_pct",
                                                          "duracion_min", "duracion_estrategia_min", "factor",
                                                          "impacto_pct")},
                        "aprendizaje": (val or {}).get("texto"), "tamano": dim}
            if fallos:
                self.metricas["rechazadas"] += 1
                self.ledger.registrar("decision", tid, self.origen, moneda=moneda, lado=lado, estrategia=clave, **decision)
                log_op.info("SEÑAL %s [%s]: %s -> DESCARTADA: %s", nombre_op(lado, moneda), clave, cand["motivo"],
                            "; ".join(fallos))
                if plan.get("precio"):
                    self.memoria.abrir_sombra(moneda, lado, cand["rasgos"], cand.get("ballenas", []), plan, fallos)
                continue
            aprobadas.append((val.get("factor", 1.0), val.get("r_esperado") or 0, cand["fuerza"], cand, plan, decision))
        self.memoria.guardar()

        aprobadas.sort(key=lambda x: x[:3], reverse=True)
        for _, _, _, cand, plan, decision in aprobadas:
            moneda, lado, tid = cand["moneda"], cand["lado"], plan["trade_id"]
            abiertas = [self._resumen_riesgo(d) for d in self.estado["posiciones"].values()] + \
                       [self._resumen_riesgo(d) for d in self.estado["shadow_abiertas"].values()]
            externas = [{"moneda": p["moneda"], "lado": p["lado"], "notional": p["notional"]}
                        for p in (self.gestor.ultimo_informe or {}).get("externas", [])] if self.gestor else []
            betas = self.betas([moneda] + [a["moneda"] for a in abiertas + externas])
            motivos = comprobar_agregados({"moneda": moneda, "lado": lado, "notional": plan["notional"],
                                           "riesgo_usd": plan["riesgo_usd"]}, abiertas, externas, eq, self.c, lim, betas)
            corr = self.correlada(moneda, lado)
            if corr:
                motivos.append(corr)
            if moneda in self.estado["posiciones"] or \
                    any(d.get("moneda") == moneda for d in self.estado["shadow_abiertas"].values()):
                # Dos estrategias pueden proponer la misma moneda en el mismo ciclo: solo entra la primera (la mejor).
                motivos.append("ya hay una posición en esa moneda (este ciclo)")
            if motivos:
                self.metricas["rechazadas"] += 1
                decision.update(aprobada=False, motivos=motivos)
                self.ledger.registrar("decision", tid, self.origen, moneda=moneda, lado=lado, estrategia=plan["estrategia"],
                                      **decision)
                log_op.info("DESCARTADA %s [%s]: %s", nombre_op(lado, moneda), plan["estrategia"], "; ".join(motivos))
                self.memoria.abrir_sombra(moneda, lado, cand["rasgos"], cand.get("ballenas", []), plan, motivos)
                continue
            self.ledger.registrar("decision", tid, self.origen, moneda=moneda, lado=lado, estrategia=plan["estrategia"],
                                  **decision)
            self.metricas["aprobadas"] += 1
            if self.modo == "shadow":
                self._intencion_shadow(cand, plan)
                continue
            plan["id_mem"] = self.memoria.registrar_real(moneda, lado, cand["rasgos"], cand.get("ballenas", []), plan)
            self.memoria.guardar()
            log_op.info("SEÑAL APROBADA %s [%s] tamaño %s (%.2f USD de riesgo)", nombre_op(lado, moneda),
                        plan["estrategia"], plan["tam"], plan["riesgo_usd"])
            try:
                op = self.gestor.abrir(plan)
            except ErrorShadow:
                raise
            except Exception as e:
                log_op.exception("Error abriendo %s: %s", moneda, e)
                self.ledger.registrar("error", tid, self.origen, moneda=moneda, mensaje=f"error al abrir: {e}",
                                      estado="error")
                self.ej.congelar(moneda, f"error inesperado al abrir: {e}", "ambigua", tid)
                continue
            if op:
                self.metricas["abiertas"] += 1
                self.estado["ops_hoy"] = self.estado.get("ops_hoy", 0) + 1
                self.avisar(f"🟢 ABIERTA {nombre_op(lado, moneda)} {op['tam']} a {op['entrada']} · stop {op.get('sl')} · "
                            f"{plan['estrategia']} · riesgo ≈ {op.get('riesgo_usd') or 0:.2f} USD", "info", tid)
            self.guardar()

    def _cobertura_ballenas(self, ahora):
        """Qué se ve de las ballenas y con qué frescura: elegidas, suscritas en tiempo real (Hyperliquid admite como
        mucho 10 usuarios por IP en suscripciones de usuario), frescura y fallos por cuenta (enmascarada), posición
        OBSERVADA y flujo agregado de la última hora por moneda (INFERIDO de fotos de posición)."""
        lim = self.c["ballenas"]["datos_max_antiguedad_seg"]
        cuentas = []
        for b in self.ballenas:
            leida = self.ballena_leida_ts.get(b)
            cuentas.append({"cuenta": conf.enmascarar_direccion(b), "tiempo_real": b in self.ws_subs,
                            "leida_ts": leida, "frescura_seg": round(ahora - leida, 1) if leida else None,
                            "fresca": bool(leida and ahora - leida <= lim), "fallos_seguidos": self.ballena_fallos.get(b, 0),
                            "posicion_observada_usd": dict(sorted(self.ballena_pos_usd.get(b, {}).items(),
                                                                  key=lambda x: -abs(x[1]))[:6])})
        flujo = {}
        for m in list(self.flujo.eventos):
            neto = sum(d for _, _, d, _ in self.flujo._ventana(m, ahora, 3600))
            if abs(neto) >= 1000:
                flujo[m] = round(neto)
        return {"elegidas": len(self.ballenas), "suscritas_tiempo_real": len(self.ws_subs),
                "limite_tiempo_real": min(10, self.c["tiempo_real"]["max_ballenas_ws"]),
                "frescas": sum(1 for x in cuentas if x["fresca"]), "fills_ws_recibidos": self.ws_fills_recibidos,
                "cuentas": cuentas, "flujo_1h_usd": dict(sorted(flujo.items(), key=lambda x: -abs(x[1]))[:15]),
                "nota": "posición observada = última foto; flujo = cambios INFERIDOS entre fotos (no fills)."}

    def _exposicion_beta(self, abiertas, externas, eq):
        todas = abiertas + [{"moneda": x["moneda"], "lado": x["lado"], "notional": x.get("notional") or 0} for x in externas]
        if not todas:
            return {"usd": 0.0, "pct": 0.0, "sin_beta": [], "limite_pct": self.c["riesgo"].get("exposicion_beta_btc_max_pct")}
        total, sin = exposicion_beta(todas, self.betas([x["moneda"] for x in todas]),
                                     self.c["riesgo"].get("beta_desconocida", 1.5))
        return {"usd": round(total, 2), "pct": round(total / eq * 100, 1) if eq else None, "sin_beta": sin,
                "limite_pct": self.c["riesgo"].get("exposicion_beta_btc_max_pct"),
                "nota": "lado × tamaño × beta frente a BTC (30 días de velas de 1 h); incluye externas. No es una medida "
                        "completa del riesgo: las betas cambian y no captan saltos."}

    def betas(self, monedas):
        """Beta de cada moneda frente a BTC con las velas de 1 h que ya tiene el motor (sin peticiones extra).
        Las que no se pueden calcular no aparecen (riesgo.beta_desconocida se aplica en comprobar_agregados)."""
        btc = self.motor.contextos.get(("BTC", "1h"))
        if not btc:
            return {}
        cb = dict(zip(btc[1].t, btc[1].c))
        cache = getattr(self, "_betas_cache", {})
        out = {}
        for m in set(monedas):
            if m == "BTC":
                out[m] = 1.0
                continue
            ctx = self.motor.contextos.get((m, "1h"))
            if not ctx:
                continue
            clave = (m, ctx[0], btc[0])
            if clave not in cache:
                cache[clave] = beta(dict(zip(ctx[1].t, ctx[1].c)), cb)
            if cache[clave] is not None:
                out[m] = cache[clave]
        self._betas_cache = {k: v for k, v in cache.items() if k[0] in out or len(cache) < 500}
        return out

    @staticmethod
    def _resumen_riesgo(d):
        notional = (d.get("tam") or 0) * (d.get("entrada") or d.get("precio") or 0)
        return {"moneda": d["moneda"], "lado": d["lado"], "notional": notional, "riesgo_usd": d.get("riesgo_usd") or 0}

    # ------------------------------------------------------------ shadow (sin órdenes)
    def _intencion_shadow(self, cand, plan):
        """Registra lo que el bot HABRÍA hecho. Separa lo observado de lo modelado."""
        moneda, lado = cand["moneda"], cand["lado"]
        s = 1 if lado == "L" else -1
        com = self.c["comisiones"]
        try:
            libro = self.info.l2_snapshot(moneda)["levels"]
            bid, ask = float(libro[0][0]["px"]), float(libro[1][0]["px"])
        except Exception:  # noqa: BLE001
            bid = ask = None
        desl = self.c["riesgo"]["deslizamiento_estimado_pct"] / 100
        ref = (ask if lado == "L" else bid) or self.mids.get(moneda)
        fill = ref * (1 + s * desl) if ref else None
        sl = fill * (1 - s * plan["sl_pct"] / 100) if fill else None
        observado = {"mid": self.mids.get(moneda), "mark": self.marks.get(moneda), "bid": bid, "ask": ask,
                     "funding_h": self.motor.funding_actual.get(moneda), "ts": time.time()}
        modelado = {"fill": fill, "tam": plan["tam"], "stop": sl, "comision_entrada_supuesta": com["taker_pct"],
                    "deslizamiento_supuesto_pct": desl * 100, "riesgo_usd": plan["riesgo_usd"], "estimado": True}
        self.ledger.registrar("shadow_intencion", plan["trade_id"], "shadow", moneda=moneda, lado=lado,
                              estrategia=plan["estrategia"], version=plan.get("version"), motivo=plan["motivo"],
                              observado=observado, modelado=modelado, version_config=conf.VERSION_CONFIG,
                              contexto={"rasgos": cand["rasgos"]})
        self.metricas["intenciones_shadow"] += 1
        self.estado["ops_hoy"] = self.estado.get("ops_hoy", 0) + 1     # mismos límites que una operación real
        plan_m = dict(plan, precio=fill or plan["precio"], fuente="shadow", origen="shadow")
        r = self.memoria.abrir_sombra(moneda, lado, cand["rasgos"], cand.get("ballenas", []), plan_m, [], tipo="shadow")
        if r:
            r["trade_id"] = plan["trade_id"]
        self.estado["shadow_abiertas"][plan["trade_id"]] = {"trade_id": plan["trade_id"], "moneda": moneda, "lado": lado,
                                                             "tam": plan["tam"], "entrada": fill, "riesgo_usd": plan["riesgo_usd"],
                                                             "estrategia": plan["estrategia"], "ts": time.time(),
                                                             "id_mem": r["id"] if r else None}
        log_op.info("SHADOW: habría abierto %s [%s] (sin órdenes)", nombre_op(lado, moneda), plan["estrategia"])

    def vigilar_estrategias(self, cada_seg=3600):
        """Cada hora: ¿alguna estrategia rinde claramente mal? (bootstrap por bloques, corregido por el número de
        estrategias vigiladas). Solo reduce: ×0,5 al degradarse y recuperación GRADUAL con persistencia (×0,75 y
        luego normal). Si tenía autorización para real, se le retira y NO se devuelve sola (ver vigilancia.py)."""
        if time.time() - self.vigilancia_ts < cada_seg:
            return
        self.vigilancia_ts = time.time()
        esperado = {d["clave"]: (d.get("validacion") or {}).get("r_medio") for d in self.motor.todas_las_estrategias()}
        self.vigilancia = vigilancia.evaluar(self.memoria.datos["registros"], esperado)
        # Compatibilidad: el formato anterior guardaba {clave: 0.5}; se migra a fases (reversible: mismo factor).
        previas = self.estado.get("vigilancia_fases")
        if previas is None:
            previas = {k: {"fase": "degradada", "desde": time.time()} for k in self.estado.get("factor_estrategia", {})}
        nuevas, cambios = vigilancia.fases(previas, self.vigilancia)
        self.estado["vigilancia_fases"] = nuevas
        self.estado["factor_estrategia"] = {k: v["factor"] for k, v in nuevas.items()}     # solo factores ≤ 1
        for clave, de, a, texto in cambios:
            if a == "degradada":
                d_def = self.motor.definicion(clave)
                if d_def and self.promociones.autorizada_real(clave, self._huella(d_def)):
                    self.promociones.revocar(clave, "vigilancia automática (rendimiento claramente negativo)")
                    self.promociones.guardar()
                    self.avisar(f"🛑 {clave}: rendimiento claramente negativo. Le he RETIRADO la autorización para dinero "
                                f"real ({texto}). Volver a darla es decisión tuya.", "error")
                self.avisar(f"📉 {clave} rinde claramente mal ({texto}): opera a la mitad de tamaño.", "aviso")
            elif a == "recuperacion":
                self.avisar(f"↗️ {clave} ya no está degradada ({texto}). Sigue a la mitad de tamaño: recupera poco a poco "
                            f"si aguanta 7 días.", "info")
            elif a == "recuperacion_avanzada":
                self.avisar(f"↗️ {clave}: 7 días sin degradarse: pasa a ×0,75 de tamaño.", "info")
            elif a == "normal":
                self.avisar(f"📈 {clave}: recuperada del todo: vuelve a su tamaño normal (nunca por encima del autorizado).",
                            "info")
        self.guardar()

    def _seguir_aspirante(self, cand):
        """Idea del departamento de investigación: se sigue en papel con precios reales, SIN órdenes ni dinero.
        No ocupa huecos, no cuenta para los límites ni para el aprendizaje del bot."""
        moneda, lado = cand["moneda"], cand["lado"]
        r = self.c["riesgo"]
        if not r["sl_min_pct"] <= cand["sl_pct"] <= r["sl_max_pct"] or costes_excesivos(self.c, cand["sl_pct"]):
            return       # el examen tampoco las cuenta
        precio = self.mids.get(moneda) or cand["precio_senal"]
        tid = nuevo_trade_id()
        plan = {"precio": precio, "sl_pct": cand["sl_pct"], "salida": cand["salida"], "duracion_min": cand["duracion_min"],
                "estrategia": cand["estrategia"], "version": "inv", "origen": "shadow", "fuente": "aspirante",
                "trade_id": tid}
        reg = self.memoria.abrir_sombra(moneda, lado, {}, [], plan, [], tipo="aspirante")
        if reg is None:
            return
        self.ledger.registrar("shadow_intencion", tid, "shadow", moneda=moneda, lado=lado, estrategia=cand["estrategia"],
                              version="inv", motivo=cand["motivo"], aspirante=cand["aspirante"],
                              observado={"mid": self.mids.get(moneda), "ts": time.time()},
                              modelado={"fill": precio, "estimado": True, "sl_pct": cand["sl_pct"]},
                              version_config=conf.VERSION_CONFIG)

    def _cerrar_aspirante(self, r):
        clave = r.get("modulo") or ""
        self.ledger.registrar("shadow_resultado", r.get("trade_id"), "shadow", moneda=r["moneda"], lado=r["lado"],
                              estrategia=clave, r=r["resultado_r"], causa=r["cierre"], estimado=True,
                              nota="aspirante del departamento de investigación: resultado MODELADO, sin órdenes")
        self.promociones.registrar_evidencia(clave, "shadow", clave.split(":", 1)[-1], r.get("trade_id"),
                                             {"r_modelado": r["resultado_r"]})
        self.promociones.guardar()

    def _cerrar_intencion_shadow(self, r):
        for tid, d in list(self.estado["shadow_abiertas"].items()):
            if d.get("id_mem") == r["id"]:
                self.estado["shadow_abiertas"].pop(tid, None)
                self.ledger.registrar("shadow_resultado", tid, "shadow", moneda=d["moneda"], lado=d["lado"],
                                      estrategia=d["estrategia"], r=r["resultado_r"], causa=r["cierre"], estimado=True,
                                      nota="resultado MODELADO con precios reales; no es una ejecución")
                dd = self.motor.definicion(d["estrategia"])
                if dd:
                    self.promociones.registrar_evidencia(d["estrategia"], "shadow", self._huella(dd), tid,
                                                         {"r_modelado": r["resultado_r"]})
                    self.promociones.guardar()

    def _evidencia_cierre(self, op, res):
        """Una operación simulada cerrada es evidencia 'paper' para la promoción (nunca cambia la etapa)."""
        if self.origen != "simulado":
            return
        d = self.motor.definicion(op.get("estrategia"))
        if d:
            self.promociones.registrar_evidencia(op["estrategia"], "paper", self._huella(d), op["trade_id"], {
                "r_neto": res.get("r_neto"), "r_bruto": res.get("r_bruto"), "pnl_neto": res.get("pnl_neto"),
                "datos_fills": res.get("datos_fills", "reales"), "ordenes_sin_confirmar": bool(res.get("ordenes_sin_confirmar")),
                "conciliacion_ok": not op.get("entrada_incierta") and op["moneda"] not in self.ej.congelados})
            self.promociones.guardar()

    def actualizar_shadow(self):
        if self.modo != "shadow":
            return
        # Las intenciones sin registro de memoria (p. ej. tras borrarla) se cierran como desconocidas.
        vivos = {r["id"] for r in self.memoria.datos["registros"] if r["tipo"] == "shadow" and r["resultado_r"] is None}
        for tid, d in list(self.estado["shadow_abiertas"].items()):
            if d.get("id_mem") is None and time.time() - d["ts"] > 60:
                # Ya había otra intención igual en seguimiento: esta no se modela por separado.
                self.estado["shadow_abiertas"].pop(tid, None)
                self.ledger.registrar("shadow_resultado", tid, "shadow", moneda=d["moneda"], lado=d["lado"],
                                      estrategia=d["estrategia"], r=None, causa="sin seguimiento propio",
                                      estimado=True, nota="duplicada de otra intención abierta en la misma moneda y lado")
            elif d.get("id_mem") not in vivos and time.time() - d["ts"] > 60 and d.get("id_mem") is not None:
                cerrados = {r["id"] for r in self.memoria.datos["registros"] if r["tipo"] == "shadow"}
                if d["id_mem"] not in cerrados:
                    self.estado["shadow_abiertas"].pop(tid, None)

    # ------------------------------------------------------------ emergencia y comandos
    def emergencia(self):
        """Suelo de emergencia: cierra SOLO las posiciones del bot y se apaga. Nunca toca lo externo."""
        tope = (f"−{self.c['riesgo']['perdida_max_total_pct']}%" + (f" o −{self.c['piloto_real']['perdida_max_usd']} USD del piloto"
                                                                    if self.modo == "real" else ""))
        texto = (f"SUELO DE EMERGENCIA ({tope}): cierro las posiciones DEL BOT "
                 f"y me apago. Las posiciones externas no se tocan.")
        self.avisar("🛑 " + texto, "error")
        if self.cerrar_posiciones_bot("suelo de emergencia"):
            raise SystemExit(SALIDA_DEFINITIVA)

    def cerrar_posiciones_bot(self, causa):
        if not self.gestor:
            return True
        ok = True
        for op in list(self.estado["posiciones"].values()):
            ok &= bool(self.gestor.cerrar(op, causa))
        return ok

    def leer_comandos_panel(self):
        try:
            with open(RUTA_COMANDOS_PANEL, encoding="utf-8") as fh:
                cmds = json.load(fh)
            os.remove(RUTA_COMANDOS_PANEL)
        except (OSError, ValueError):
            return []
        out = []
        for c in cmds if isinstance(cmds, list) else []:
            if isinstance(c, str):
                c = {"cmd": c, "id": None}
            if isinstance(c, dict) and c.get("cmd") in COMANDOS_PERMITIDOS + ("/cerrartodo", "/aplicarconfig", "/laboratorio"):
                c["origen"] = "panel"
                out.append(c)
        return out

    def _resultado_comando(self, cid, ok, mensaje):
        if not cid:
            return
        res = cargar_json(RUTA_RESULTADOS_COMANDOS, {})
        res[cid] = {"ok": ok, "mensaje": mensaje, "ts": time.time()}
        if len(res) > 50:
            for k in sorted(res, key=lambda k: res[k]["ts"])[:-50]:
                res.pop(k)
        guardar_json(RUTA_RESULTADOS_COMANDOS, res)

    def atender_comandos(self):
        try:
            with open(os.path.join(AQUI, "incidentes_reconocidos.json"), encoding="utf-8") as fh:
                for inc_id in json.load(fh):
                    self.sistema.reconocer(str(inc_id))
            os.remove(os.path.join(AQUI, "incidentes_reconocidos.json"))
        except (OSError, ValueError, TypeError):
            pass
        pendientes = [{"cmd": t.split()[0], "args": t.split()[1:], "origen": "telegram", "id": None}
                      for t in self.tg.comandos()] + self.leer_comandos_panel()
        for c in pendientes:
            cmd, cid = c["cmd"], c.get("id")
            try:
                ok, msg = self.ejecutar_comando(c)
            except ErrorShadow as e:
                ok, msg = False, str(e)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"error con {cmd}: {e}"
            log_sis.info("Comando %s desde %s: %s", cmd, c.get("origen"), "ok" if ok else msg)
            self._resultado_comando(cid, ok, msg)
            if c.get("origen") == "telegram":
                self.tg.enviar(msg)

    def ejecutar_comando(self, c):
        cmd = c["cmd"]
        if cmd == "/estado":
            eq, _ = self.equity()
            lineas = [(f"Modo {self.modo.upper()} · valor {eq if eq is not None else 'desconocido'} USD · "
                       f"{self.estado.get('ops_hoy', 0)} operaciones hoy"),
                      "Entradas permitidas" if not self.bloqueos else "Entradas bloqueadas: " + "; ".join(self.bloqueos)]
            for m, d in self.estado["posiciones"].items():
                lineas.append(f"• {nombre_op(d['lado'], m)} {d.get('tam')} desde {d.get('entrada')} · stop {d.get('sl')}")
            return True, "\n".join(lineas)
        if cmd == "/pausa":
            self.estado["pausa_manual"] = True
            self.guardar()
            return True, "Pausado: no abriré operaciones nuevas. Las abiertas siguen gestionándose."
        if cmd == "/reanudar":
            self.estado["pausa_manual"] = False
            self.estado["pausa_hasta"] = 0
            self.guardar()
            return True, "Reanudado."
        if cmd in ("/cerrarbot", "/cerrartodo"):
            if self.modo == "shadow":
                return False, "SHADOW: este modo no tiene órdenes; no hay posiciones que cerrar."
            alcance = sorted(self.estado["posiciones"])
            confirmado = (c.get("confirmacion") == CONFIRMACION_CIERRE or CONFIRMACION_CIERRE in (c.get("args") or [])
                          or "CONFIRMAR" in (c.get("args") or []))
            if not confirmado:
                return False, (f"Para cerrar SOLO las posiciones del bot ({', '.join(alcance) or 'ninguna'}) envía "
                               f"/cerrarbot CONFIRMAR. Las posiciones externas no se tocan nunca.")
            if c.get("alcance") is not None and sorted(c["alcance"]) != alcance:
                return False, "las posiciones del bot han cambiado desde que confirmaste: vuelve a revisarlas"
            self.estado["pausa_manual"] = True
            ok = self.cerrar_posiciones_bot("orden manual")
            self.guardar()
            return ok, ("Cerradas las posiciones del bot y bot en pausa." if ok else
                        "No se pudo cerrar alguna posición del bot: mantiene su stop. Lo reintento.")
        if cmd == "/aplicarconfig":
            return self.aplicar_config_nueva()
        if cmd == "/laboratorio":
            return self.lanzar_laboratorio()
        if cmd == "/informe":
            partes = []
            for tipo, nombre in (("real", "Operaciones del bot"), ("sombra", "Descartadas (papel)"),
                                 ("shadow", "Shadow (modelado)"), ("backtest", "Laboratorio")):
                n, gan, media = self.memoria.resumen(tipo)
                if n:
                    partes.append(f"{nombre}: {n} · {gan:.0f}% ganadoras · {media:+.2f} R de media")
            return True, "\n".join(partes) or "Todavía no hay resultados."
        return True, "Comandos: /estado /pausa /reanudar /cerrarbot CONFIRMAR /informe"

    def lanzar_laboratorio(self):
        """Lanza el laboratorio (laboratorio_semanal.py: registro, bloqueo contra duplicados y aviso) en un proceso
        APARTE que sigue aunque el bot se reinicie. Solo añade evidencia: nunca activa nada ni sube límites."""
        import subprocess
        lock = os.path.join(AQUI, "laboratorio_semanal.lock")
        try:
            if time.time() - os.path.getmtime(lock) < 6 * 3600:
                return False, "Ya hay un laboratorio en marcha."
        except OSError:
            pass
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen([sys.executable, os.path.join(AQUI, "laboratorio_semanal.py"), "--presupuesto", "600"],
                             cwd=AQUI, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        except OSError as e:
            return False, f"No pude lanzar el laboratorio: {e}"
        log_sis.info("Laboratorio lanzado en segundo plano (registro/laboratorio_semanal_*.log).")
        return True, "Laboratorio lanzado en segundo plano. Tarda horas; el resultado queda en registro/ y en el panel."

    def aplicar_config_nueva(self):
        """El panel ha guardado un config.json nuevo. Se valida OTRA VEZ aquí (con las mismas reglas que al arrancar)
        y, si es válido y no cambia el modo, el bot se reinicia de forma ordenada para aplicarlo. Si no es válido, se
        sigue con la configuración actual."""
        try:
            nueva = cargar_config(dict(os.environ))
        except conf.ErrorConfig as e:
            return False, f"La configuración guardada NO es válida; sigo con la actual. Motivo: {e}"
        for k in ("modo", "red", "real", "piloto_real"):
            if nueva.get(k) != self.c.get(k):
                return False, f"La configuración nueva cambia «{k}»: eso no se aplica desde el panel. Sigo con la actual."
        self.reinicio_pedido = True
        self.parar.set()
        self.despertar.set()
        log_sis.info("Configuración nueva válida: reinicio ordenado para aplicarla.")
        return True, "Configuración válida. Me reinicio de forma ordenada para aplicarla (unos segundos)."

    # ------------------------------------------------------------ métricas para el panel
    def horas_sin_operar(self):
        reales = [r["ts"] for r in self.memoria.datos["registros"] if r["tipo"] in ("real", "shadow")]
        desde = max(reales) if reales else self.sistema.inicio
        return (time.time() - desde) / 3600

    def factor_capital(self, saldo):
        ultimos = [r["resultado_r"] for r in sorted(self.memoria.cerrados(con_lab=False), key=lambda x: x["cierre_ts"])
                   if r["tipo"] == "real"]
        e = self.estado
        return factor_capital(saldo or 0, e.get("saldo_maximo"), ultimos, self.c["gestion_capital"],
                              e.get("saldo_inicio_dia"), e.get("saldo_inicio_semana"), self.horas_sin_operar())

    def escribir_panel(self):
        """Foto del estado de TRADING para el panel (la del sistema va en estado_sistema.json)."""
        try:
            e = self.estado
            eq, libre = self.equity()
            cta = self.gestor.ultima_cuenta if self.gestor else None
            hist = e.setdefault("historial_saldo", [])
            if eq is not None and (not hist or time.time() - hist[-1][0] >= 300):
                hist.append([int(time.time()), round(eq, 4)])
                del hist[:-3000]
            inf = (self.gestor.ultimo_informe if self.gestor else None) or {}
            posiciones = []
            for m, d in e["posiciones"].items():
                p = (cta or {}).get("posiciones", {}).get(m, {})
                px = self.mids.get(m)
                liq = p.get("liq")
                mark = self.marks.get(m) or ((p.get("notional") or 0) / p["tam"] if p.get("tam") else None)
                mark_ts = self.marks_ts.get(m) or ((cta or {}).get("ts") if p.get("tam") else None)
                posiciones.append({
                    "trade_id": d["trade_id"], "moneda": m, "lado": d["lado"], "estado": d.get("estado"),
                    "entrada": d.get("entrada"), "precio_mid": px, "precio_mark": self.marks.get(m), "tam": d.get("tam"),
                    "pnl_no_realizado": p.get("pnl"), "sl": d.get("sl"), "sl_inicial": d.get("sl_inicial"),
                    "tp1": d.get("tp1"), "tpf": d.get("tpf"), "tp1_hecho": d.get("tp1_hecho"),
                    "en_breakeven": d.get("en_breakeven"), "stop_verificado": m not in (self.gestor.sin_stop if self.gestor else {}),
                    "stop_verificado_ts": d.get("stop_verificado_ts"), "abierta_ts": d.get("abierta_ts"),
                    "limite_ts": (d.get("abierta_ts") or 0) + (self.gestor.duracion_efectiva_min(d) * 60 if self.gestor else 0),
                    "duracion_estrategia_min": d.get("duracion_min"), "r_usd": d.get("r_usd"), "riesgo_usd": d.get("riesgo_usd"),
                    "notional": p.get("notional"), "margen": p.get("margen"), "apalancamiento": p.get("apal"),
                    "liquidacion": liq, "liquidacion_estimada": self.modo != "real",
                    # Referencias de precio EXPLÍCITAS: el stop del exchange se activa con la MARCA; el stop dinámico
                    # local usa salida.precio_trailing. Distancias medidas desde la marca (la que activa el stop).
                    "referencia_stop": "mark", "referencia_trailing": self.cs.get("precio_trailing", "mid"),
                    "precio_mark_ts": mark_ts, "precio_mid_ts": self.mids_ts,
                    "divergencia_mark_mid_pct": (abs(mark - px) / px * 100) if mark and px else None,
                    "distancia_liquidacion_pct": (abs(mark - liq) / mark * 100) if liq and mark else None,
                    "distancia_stop_pct": (abs(mark - d["sl"]) / mark * 100) if mark and d.get("sl") else None,
                    "riesgo_liquidacion": (self.gestor.riesgo_liquidacion.get(m) if self.gestor else None),
                    "estrategia": d.get("estrategia"), "version": d.get("version"), "motivo": d.get("motivo"),
                    "explorando": d.get("explorando", False), "congelada": m in self.ej.congelados,
                })
            externas = [dict(x, moneda=x["moneda"]) for x in inf.get("externas", [])]
            abiertas = [{"moneda": x["moneda"], "lado": x["lado"], "notional": x.get("notional") or 0,
                         "riesgo_usd": x.get("riesgo_usd") or 0} for x in posiciones]
            expo = exposicion(abiertas, [{"moneda": x["moneda"], "lado": x["lado"], "notional": x["notional"]}
                                         for x in externas], eq)
            lim = limites_efectivos(self.c)
            ini_d, ini_s, mx = e.get("saldo_inicio_dia"), e.get("saldo_inicio_semana"), e.get("saldo_maximo")
            limites = {
                "dia": {"valor_pct": (eq / ini_d - 1) * 100 if eq and ini_d else None, "limite_pct": -lim["perdida_diaria_max_pct"]},
                "semana": {"valor_pct": (eq / ini_s - 1) * 100 if eq and ini_s else None, "limite_pct": -lim["perdida_semanal_max_pct"]},
                "caida": {"valor_pct": -(1 - eq / mx) * 100 if eq and mx else None, "limite_pct": -lim["drawdown_max_pct"]},
                "operaciones_hoy": {"valor": e.get("ops_hoy", 0), "limite": lim["max_operaciones_dia"]},
                "riesgo_abierto": {"valor_pct": expo["riesgo_abierto_pct"], "limite_pct": lim["riesgo_abierto_max_pct"]},
            }
            estrategias = []
            for d in self.motor.todas_las_estrategias():
                v = self.promociones.vigente(d["clave"], self._huella(d))
                estrategias.append({"clave": d["clave"], "version": d.get("version"), "activa": d["activa"],
                                    "etapa": v["etapa"], "invalidada": v["invalidada"], "motivo": v["motivo"],
                                    "aprobado_por": v.get("aprobado_por"), "aprobado_ts": v.get("aprobado_ts"),
                                    "informe": v.get("informe"), "laboratorio_superado": d.get("validada"),
                                    "validacion": d.get("validacion"), "params": d.get("params"),
                                    "duracion_min": d.get("duracion_min"),
                                    "duracion_efectiva_min": min(d.get("duracion_min") or 1e12, lim["duracion_max_global_h"] * 60),
                                    "progreso": self.promociones.progreso(d["clave"], self._huella(d))})
            ahora = time.time()
            ballenas = []
            for b in self.ballenas:
                n, media = self.memoria.reputacion(b)
                pt = self.motor.info_lab["puntuacion_ballenas"].get(b) or {}
                cambios = []
                for m in set(self.flujo.eventos):
                    neto = sum(d for t, bb, d, _ in self.flujo._ventana(m, ahora, 3600) if bb == b)
                    if abs(neto) >= 1000:
                        cambios.append({"moneda": m, "neto_1h_usd": round(neto)})
                ballenas.append({
                    "direccion": conf.enmascarar_direccion(b),
                    "regla": "predictiva según el laboratorio" if b in self.motor.info_lab["ballenas_predictivas"]
                             else ("manual" if b in [x.lower() for x in self.c.get("ballenas_manuales", [])]
                                   else "ranking de Hyperliquid (filtros de selección)"),
                    "alta_ts": self.ballena_alta_ts.get(b), "actualizada_ts": self.ballena_leida_ts.get(b),
                    "fresca": ahora - self.ballena_leida_ts.get(b, 0) <= self.c["ballenas"]["datos_max_antiguedad_seg"],
                    "casos": n, "r_medio": round(media, 3) if n else None, "vetada": self.memoria.vetada(b),
                    "puntuacion": pt, "etiqueta": ("validada fuera de muestra" if b in self.motor.info_lab["ballenas_predictivas"]
                                                   and self.motor.info_lab["ballenas_validada"] else
                                                   "inferencia" if pt else "observación"),
                    "tiempo_real": b in self.ws_subs, "abiertas": sorted(f"{m}:{lado}" for m, lado in self.pos_ballenas.get(b, {}).items()),
                    "cambios_1h": sorted(cambios, key=lambda x: -abs(x["neto_1h_usd"]))[:8]})
            alertas = [{"ts": t, "severidad": s, "mensaje": m, "trade_id": tid}
                       for t, s, m, tid in (self.alertas + list(self.ej.alertas))[-100:]]
            alertas.sort(key=lambda x: x["ts"], reverse=True)
            datos = {
                "ts": ahora, "modo": self.modo, "origen": self.origen, "red": self.c["red"],
                "cuenta": conf.enmascarar_direccion(self.cuenta) if self.modo == "real" else self.modo,
                "conciliacion_ok": self.conciliacion_ok, "conciliacion_ts": (cta or {}).get("ts"),
                "saldo": eq, "libre": libre, "saldo_ts": (cta or {}).get("ts") if self.modo != "shadow" else None,
                "margen_usado": (cta or {}).get("margen_usado"), "margen_mantenimiento": (cta or {}).get("mantenimiento"),
                "saldo_inicial": e.get("saldo_inicial", eq), "saldo_inicio_dia": ini_d, "saldo_maximo": mx,
                "ops_hoy": e.get("ops_hoy", 0), "operando": not self.bloqueos, "bloqueos": self.bloqueos,
                "posiciones": posiciones, "externas": externas, "posiciones_legado": list(e.get("posiciones_legado", {})),
                "ordenes_externas": inf.get("ordenes_externas", []), "sin_stop": dict(self.gestor.sin_stop) if self.gestor else {},
                "congelados": self.ej.congelados, "exposicion": expo, "limites": limites, "alertas": alertas,
                "shadow_abiertas": list(e["shadow_abiertas"].values()), "intentos_shadow": len(getattr(self.ej, "intentos", [])),
                "historial_saldo": hist, "estrategias": estrategias, "ballenas": ballenas,
                "config": conf.sin_secretos(self.c), "efectiva": conf.efectiva(self.c, self.motor.todas_las_estrategias()),
                "motor": self.motor.resumen(), "mercado": self.mercado.resumen(self.motor.monedas),
                "mercados_disponibles": sorted(self.meta)[:1000],
                "datos_acumulados": self.datos_acumulados(),
                "cobertura_ballenas": self._cobertura_ballenas(ahora),
                "feed": {"ultimo_seq": self.feed.seq, "escritos": self.feed.escritos, "activo": self.feed.activo,
                         "fuentes": dict(self.fuentes)},
                "riesgo_liquidacion": dict(self.gestor.riesgo_liquidacion) if self.gestor else {},
                "margen": dict(self.gestor.margen) if self.gestor else {},
                "exposicion_beta": self._exposicion_beta(abiertas, externas, eq),
                "comisiones": dict(self.c["comisiones"], nota=(
                    "leídas de la cuenta al arrancar" if self.c["comisiones"].get("fuente") == "cuenta" else
                    "ESTIMACIÓN: tarifa manual de config.json (nivel 0 de Hyperliquid sin descuentos); "
                    "la real depende del volumen de 14 días y del HYPE en staking")),
                "vigilancia": self.vigilancia, "factor_estrategia": self.estado.get("factor_estrategia", {}),
                "vigilancia_fases": self.estado.get("vigilancia_fases", {}),
                "investigacion": self.resumen_investigacion(),
                "contexto_externo": self.ctx_externo.resumen(),
                "cerebro": self.cerebro.resumen(),
                "metricas": self.metricas, "datos_frescos": self.datos_frescos(), "precios_ts": self.mids_ts,
                "gestion": dict(zip(("factor", "motivos", "filtros_relajados"), self.factor_capital(eq))),
                "aviso_stops": "Un stop se activa con el precio de marca (mark) y se ejecuta a mercado: el precio real "
                               "de salida puede ser peor que el del stop (deslizamiento), sobre todo con movimientos bruscos.",
            }
            guardar_json(RUTA_PANEL, datos)
            self.sistema.marcar("almacenamiento", "correcto", "estado y panel guardados")
        except Exception as ex:
            self.sistema.marcar("almacenamiento", "degradado", f"no pude escribir los datos del panel: {ex}")
            log_sis.debug("panel: %s", ex, exc_info=True)

    def archivar(self):
        """Guarda las velas cerradas que el motor ya tiene (sin peticiones extra) y, cada pocos minutos, el libro.
        Un fallo aquí nunca afecta a la operativa."""
        if self.archivo is None:
            try:
                self.ctx_externo.actualizar(self.motor.monedas, self.mids, self.motor.funding_actual, self.motor.volumen)
            except Exception as e:  # noqa: BLE001
                log_sis.warning("Contexto externo: %s", e)
            return
        try:
            for (moneda, iv), (_, ctx) in list(self.motor.contextos.items()):
                self.archivo.guardar_velas(moneda, iv, ctx.v)
            self.archivo.foto_libro(self.info, self.motor.monedas)
        except Exception as e:  # noqa: BLE001
            log_sis.warning("Archivo de datos: %s", e)
        try:
            self.ctx_externo.actualizar(self.motor.monedas, self.mids, self.motor.funding_actual, self.motor.volumen)
        except Exception as e:  # noqa: BLE001
            log_sis.warning("Contexto externo: %s", e)

    def resumen_investigacion(self):
        """Para el panel: aspirantes en observación con su progreso en vivo, y el último informe (cada 5 min)."""
        cache = getattr(self, "_cache_inv", None)
        if cache and time.time() - cache[0] < 300:
            return cache[1]
        datos = self._resumen_investigacion()
        self._cache_inv = (time.time(), datos)
        return datos

    def _resumen_investigacion(self):
        import investigacion as inv
        reg = inv.cargar_registro(os.path.join(AQUI, "investigacion.json"))
        regs = self.memoria.datos["registros"]
        asp = [{"id": k, "descripcion": v.get("descripcion"), "estado": v["estado"], "fecha": v.get("fecha"),
                "t_lab": (v.get("metricas") or {}).get("t"), "r_lab": (v.get("metricas") or {}).get("r_medio"),
                "vivo": inv.resultados_shadow(regs, "inv:" + k), "objetivo": inv.OPS_JUICIO_SHADOW}
               for k, v in inv.aspirantes(reg).items()]
        informe = cargar_json(os.path.join(DIR_REGISTRO, "investigacion_informe.json"), {})
        cementerio = sorted(((v.get("fecha", ""), v.get("descripcion"), v.get("motivo"))
                             for v in reg["ideas"].values() if v["estado"] == "cementerio"), reverse=True)[:20]
        import validacion as vd
        variantes = vd.RegistroVariantes(os.path.join(AQUI, "laboratorio_variantes.json")).total()
        n_var = max(1, variantes, reg["probadas_total"])
        return {"probadas_total": reg["probadas_total"], "variantes_totales": n_var,
                "t_requerido": round(vd.t_requerido(n_var, n_ops=60), 2),
                "aspirantes": asp, "en_cementerio": sum(1 for v in reg["ideas"].values() if v["estado"] == "cementerio"),
                "ultimo_informe": {k: informe.get(k) for k in ("fecha", "probadas_hoy", "minutos", "cambios")},
                "cementerio_reciente": [{"fecha": f, "descripcion": dsc, "motivo": m} for f, dsc, m in cementerio],
                "en_revision": [{"id": k, "descripcion": v.get("descripcion"), "fecha": v.get("fecha"), "motivo": v.get("motivo")}
                                for k, v in reg["ideas"].items() if v["estado"] == "lista_revision"]}

    def datos_acumulados(self):
        """Cuántos datos propios lleva acumulados el bot (lo que permite evaluar estrategias nuevas)."""
        def dias(prefijo, carpeta):
            try:
                return len({f.rsplit("_", 1)[1][:10] for f in os.listdir(carpeta) if f.startswith(prefijo + "_")})
            except OSError:
                return 0
        out = {"dias_mercado": dias("ctx", DIR_MERCADO), "min_dias_mercado": 14}
        if getattr(self, "archivo", None) is not None:
            out.update(self.archivo.resumen())
        return out

    # ------------------------------------------------------------ bucle
    def conciliacion_inicial(self):
        """Al arrancar: conciliar ANTES de abrir nada. Si algo no cuadra, se queda en modo seguro."""
        if not self.gestor:
            return True
        inf = self.gestor.conciliar(self.mids)
        if inf is None:
            self.avisar("No pude conciliar al arrancar: no abro nada hasta poder leer la cuenta.", "error")
            return False
        problemas = inf["tam_mayor"] + inf["lado_distinto"]
        if problemas:
            self.avisar(f"Al arrancar, la cuenta no cuadra con el registro ({problemas}): modo seguro.", "error")
        self.ledger.registrar("conciliacion", None, self.origen, accion="arranque", resultado={
            k: (len(v) if isinstance(v, list) else v) for k, v in inf.items()})
        return not problemas

    def ejecutar(self):
        self.leer_precios()
        self.conciliacion_ok = self.conciliacion_inicial()
        eq, _ = self.equity()
        banner = {"simulado": "SIMULADO (dinero ficticio)", "shadow": "SHADOW · órdenes deshabilitadas",
                  "real": f"REAL ({self.c['red']}) · subcuenta {conf.enmascarar_direccion(self.cuenta)}"}[self.modo]
        log_sis.info("Bot en marcha · %s · valor %s USD", banner, f"{eq:.2f}" if eq else "desconocido")
        self.tg.enviar(f"🤖 Bot en marcha · {banner}. /ayuda para ver comandos.")
        while not self.parar.is_set():
            inicio = time.time()
            ok = True
            try:
                self.atender_comandos()
                if not self.parar.is_set():          # p. ej. reinicio pedido para aplicar configuración
                    self.ciclo()
            except SystemExit:
                raise
            except ErrorShadow as e:
                ok = False
                self.sistema.incidente("ejecucion", "shadow", "error", str(e),
                                       "Algo intentó enviar órdenes en modo shadow y fue bloqueado.",
                                       "Es un fallo del bot: avisa y revisa el registro.")
            except Exception as e:
                ok = False
                log_sis.exception("Error en el ciclo (sigo en el siguiente): %s", e)
                self.sistema.incidente("ciclo", type(e).__name__, "error", f"error en el ciclo: {e}",
                                       "Un ciclo falló; el siguiente lo reintenta. Las posiciones mantienen su stop.",
                                       "Si se repite, revisa el registro de Observabilidad con su traza.")
                if "429" in str(e)[:20]:
                    self.sistema.contadores["reintentos_429"] += 1
            if LIMITADOR is not None:
                self.sistema.contadores["reintentos_429"] = LIMITADOR.reintentos_429
                self.sistema.contadores["esperas_limite"] = LIMITADOR.esperas
            self.sistema.ciclo(time.time() - inicio, ok)
            if ok:
                self.latido_externo()
            self.sistema.marcar("ciclo", "correcto" if ok else "degradado", f"último ciclo {time.time() - inicio:.1f} s")
            self.sistema.marcar("proceso", "correcto", "en marcha")
            self.vigilar_panel()
            self.sistema.marcar("notificaciones", "correcto" if getattr(self.tg, "activo", False) else "desactivado",
                                "Telegram activo" if getattr(self.tg, "activo", False) else "Telegram no configurado")
            self.escribir_panel()
            self.sistema.escribir({"modo": self.modo, "version_config": conf.VERSION_CONFIG,
                                   "version_codigo": self._version_codigo()})
            if self.despertar.wait(timeout=self.c["segundos_entre_ciclos"]):
                time.sleep(max(0.0, 5 - (time.time() - inicio)))
            self.despertar.clear()
        self.apagar()

    def _huella(self, d):
        """Huella de la estrategia con el código que está EJECUTANDO este proceso (calculado una vez).
        None si no se puede calcular: entonces nada está autorizado para real."""
        codigo = self._version_codigo()
        return huella_segura(d, self.c, codigo=codigo) if codigo else None

    def vigilar_panel(self, max_seg=120):
        """El panel es OTRO proceso: se vigila por su latido (panel_latido.json, lo escribe cada 30 s)."""
        lat = cargar_json(os.path.join(AQUI, "panel_latido.json"), {})
        if not lat.get("ts"):
            self.sistema.marcar("panel", "sin_datos", "el panel no ha dado señales (¿está arrancado?)")
            return
        edad = time.time() - lat["ts"]
        if edad > max_seg:
            self.sistema.marcar("panel", "fallido", f"el panel no da señales desde hace {edad:.0f} s")
            self.sistema.incidente("panel", "latido", "aviso", "el panel no responde",
                                   "El bot sigue operando; solo deja de verse el panel.",
                                   "Reinicia el panel (servidor: sudo systemctl restart bot-ballenas-panel).")
        else:
            self.sistema.marcar("panel", "correcto", f"latido hace {edad:.0f} s · {lat.get('peticiones', 0)} peticiones · "
                                f"{lat.get('errores', 0)} errores", ts_dato=lat["ts"])
            self.sistema.resolver("panel", "latido")

    def latido_externo(self, cada_seg=300):
        """Señal de vida a un vigilante externo (Healthchecks.io u otro que reciba un GET). Si el servidor o el
        bot se caen, la señal deja de llegar y ESE servicio avisa: el bot no puede avisar de su propia caída."""
        if not self.latido_url:
            self.sistema.marcar("latido_externo", "desactivado", "HEALTHCHECK_URL no configurada en .env")
            return
        if time.time() - self.latido_ts < cada_seg or (self.latido_hilo and self.latido_hilo.is_alive()):
            return
        self.latido_ts = time.time()

        def enviar():
            try:
                r = requests.get(self.latido_url, timeout=10)
                r.raise_for_status()
                self.sistema.marcar("latido_externo", "correcto", "señal enviada", ts_dato=time.time())
            except Exception as e:  # noqa: BLE001
                self.sistema.marcar("latido_externo", "degradado", f"no se pudo enviar la señal: {type(e).__name__}")
        self.latido_hilo = threading.Thread(target=enviar, daemon=True)
        self.latido_hilo.start()

    def _version_codigo(self):
        if not hasattr(self, "_vc"):
            from promocion import hash_codigo
            try:
                self._vc = hash_codigo()
            except ErrorHuella as e:
                log_sis.error("No puedo calcular la huella del código (%s): ninguna estrategia queda autorizada.", e)
                self._vc = None
        return self._vc

    def apagar(self):
        """Apagado ordenado: no se abren más operaciones, se guarda todo, se intenta conciliar una última vez
        y se CONSERVAN los stops y objetivos en el exchange (no se cancela nada)."""
        log_sis.info("Apagando: dejo de abrir, guardo el estado y conservo stops y objetivos en el exchange.")
        try:
            if self.gestor:
                self.gestor.conciliar(self.mids)
        except Exception as e:  # noqa: BLE001
            log_sis.warning("Conciliación final fallida: %s", e)
        self.guardar()
        self.memoria.guardar()
        self.ledger.registrar("conciliacion", None, self.origen, accion="apagado",
                              posiciones_bot=sorted(self.estado["posiciones"]))
        self.sistema.marcar("proceso", "fallido", "detenido")
        self.sistema.escribir()
        self.tg.enviar("⏹️ Bot detenido. Las posiciones del bot conservan su stop y objetivos en el exchange.")
        # El websocket de la librería de Hyperliquid usa hilos que no son 'daemon': si no se cierra, el proceso
        # no termina y un reinicio ordenado se queda colgado.
        try:
            if self.ws_info is not None and hasattr(self.ws_info, "disconnect_websocket"):
                self.ws_info.disconnect_websocket()
        except Exception:  # noqa: BLE001
            pass


# ====================================================================== main
def crear_cliente(c, entorno, estado):
    """Crea los clientes según el modo. En shadow NO se crea ningún cliente de órdenes."""
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    url = constants.TESTNET_API_URL if c["red"] == "testnet" else constants.MAINNET_API_URL
    global LIMITADOR
    lim = LIMITADOR = Limitador(log=log_sis)
    if c["modo"] == "shadow":
        info = lim.envolver(Info(url, skip_ws=True))
        return info, EjecutorShadow(log_op), None, url
    ledger = Ledger(DIR_REGISTRO)
    if c["modo"] == "simulado":
        from papel import CuentaPapel, InfoConPapel
        info_real = lim.envolver(Info(constants.MAINNET_API_URL, skip_ws=True))
        cuenta = "simulado"
        papel = CuentaPapel(info_real, RUTA_PAPEL, c["saldo_simulado_usd"], c["comisiones"]["maker_pct"] / 100,
                            c["comisiones"]["taker_pct"] / 100, log_op)
        info = InfoConPapel(info_real, papel, cuenta)
        ej = Ejecutor(papel, info, cuenta, ledger, "simulado", log_op, c["ordenes"], c["riesgo"]["errores_seguidos_para_degradar"])
        return info, ej, cuenta, constants.MAINNET_API_URL
    # ----- REAL: todas las comprobaciones ANTES de crear el cliente firmante
    import eth_account
    cuenta = entorno["HL_DIRECCION_CUENTA"].strip().lower()
    info = lim.envolver(Info(url, skip_ws=True))
    agente = eth_account.Account.from_key(entorno["HL_API_WALLET_KEY"].strip())
    verificar_subcuenta(info, c, cuenta, agente.address)
    problemas = verificar_propiedad_inicial(info, cuenta, estado)
    if problemas:
        raise ErrorArranque("la subcuenta tiene posiciones u órdenes que el bot no puede atribuirse: " +
                            "; ".join(problemas) + ". Ciérralas a mano o usa otra subcuenta.")
    if c["comisiones"]["fuente"] == "cuenta":
        try:
            fees = info.user_fees(cuenta)
            c["comisiones"].update(maker_pct=float(fees["userAddRate"]) * 100, taker_pct=float(fees["userCrossRate"]) * 100)
        except Exception as e:  # noqa: BLE001
            raise ErrorArranque(f"no pude leer las comisiones de la cuenta: {e}") from None
    from hyperliquid.exchange import Exchange
    exchange = Exchange(agente, url, vault_address=cuenta)      # se crea el ÚLTIMO, tras todas las comprobaciones
    return info, Ejecutor(exchange, info, cuenta, ledger, "real", log_op, c["ordenes"],
                          c["riesgo"]["errores_seguidos_para_degradar"]), cuenta, url


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bot de Hyperliquid")
    ap.add_argument("--validar-config", action="store_true", help="solo valida la configuración y sale")
    ap.add_argument("--migrar-config", action="store_true", help="migra config.json (con copia de seguridad) y sale")
    args = ap.parse_args(argv)
    consola_utf8()
    configurar_logs(AQUI)
    load_dotenv(os.path.join(AQUI, ".env"))
    entorno = dict(os.environ)
    if args.migrar_config:
        cambios = conf.migrar_archivo(RUTA_CONFIG)
        print("\n".join(cambios) if cambios else "La configuración ya está en el formato actual.")
        return 0
    try:
        c = cargar_config(entorno)
    except conf.ErrorConfig as e:
        log_sis.error("%s", e)
        raise SystemExit(SALIDA_DEFINITIVA) from None
    if args.validar_config:
        ef = conf.efectiva(c)
        print(f"Configuración válida. Modo: {c['modo']}.")
        for k, v in ef["riesgo"].items():
            print(f"  riesgo.{k} = {v}  (fuente: {ef['fuente'][k]})")
        return 0
    # Una sola instancia por carpeta y por cuenta (bloqueo del sistema operativo; se libera solo si el proceso muere).
    clave_cuenta = (entorno.get("HL_DIRECCION_CUENTA", "").strip().lower() if c["modo"] == "real"
                    else f"{c['modo']}:{AQUI}")
    try:
        bloqueo = BloqueoInstancia(AQUI, clave_cuenta).tomar()
    except InstanciaDuplicada as e:
        log_sis.error("NO ARRANCO: %s. Nunca deben operar dos bots la misma cuenta.", e)
        raise SystemExit(SALIDA_DEFINITIVA) from None
    from observabilidad import OBLIGATORIOS_BASE
    obligatorios = OBLIGATORIOS_BASE + (() if c["modo"] == "shadow" else ("ejecucion", "conciliacion"))
    sistema = Sistema(AQUI, umbrales={"datos_mercado": c["riesgo"]["datos_max_antiguedad_seg"] * 2,
                                      "ballenas": c["ballenas"]["datos_max_antiguedad_seg"] * 2,
                                      "ciclo": c["segundos_entre_ciclos"] * 6},
                      obligatorios=obligatorios)
    estado = cargar_json(RUTA_ESTADO, {})
    if estado.get("modo") and estado["modo"] != c["modo"]:
        estado = {}
    try:
        info, ej, cuenta, url = crear_cliente(c, entorno, estado)
    except ErrorArranque as e:
        log_sis.error("NO ARRANCO: %s", e)
        sistema.incidente("proceso", "arranque", "error", f"arranque bloqueado: {e}",
                          "El bot se ha negado a arrancar para no operar en condiciones inseguras.",
                          "Lee el motivo y corrige la configuración o la cuenta.")
        sistema.escribir()
        raise SystemExit(SALIDA_DEFINITIVA) from None
    ledger = ej.ledger if isinstance(ej, Ejecutor) else Ledger(DIR_REGISTRO)
    tg = Telegram(entorno.get("TELEGRAM_TOKEN"), entorno.get("TELEGRAM_CHAT_ID"), logging.getLogger("bot.notificaciones"))
    promociones = Registro()
    bot = Bot(c, info, ej, cuenta, url, tg, ledger, sistema, promociones)
    latido = entorno.get("HEALTHCHECK_URL", "").strip()
    if latido and not latido.startswith("https://"):
        log_sis.warning("HEALTHCHECK_URL debe empezar por https:// ; no se usará.")
        latido = ""
    bot.latido_url = latido
    if c["modo"] == "real":
        promovidas = estrategias_promovidas(c, promociones, bot.motor)
        if not promovidas:
            log_sis.error("NO ARRANCO EN REAL: ninguna estrategia tiene una promoción 'real_habilitado' vigente "
                          "(python promocion.py estado). El laboratorio no autoriza nada por sí solo.")
            raise SystemExit(SALIDA_DEFINITIVA)
        log_sis.warning("*** MODO REAL *** subcuenta %s · estrategias autorizadas: %s · límites del piloto: %s",
                        conf.enmascarar_direccion(cuenta), ", ".join(promovidas), c["piloto_real"])

    def detener(signum, frame):
        log_sis.info("Señal %s recibida: apagado ordenado.", signum)
        bot.parar.set()
        bot.despertar.set()
    for nombre in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, nombre):
            try:
                signal.signal(getattr(signal, nombre), detener)
            except (ValueError, OSError):
                pass
    evitar_suspension(log_sis)
    try:
        bot.ejecutar()
    finally:
        bloqueo.soltar()
    if getattr(bot, "reinicio_pedido", False):
        return SALIDA_REINICIO
    return 0


def _salir(codigo):
    """Sale SIEMPRE, aunque quede algún hilo ajeno vivo (p. ej. el ping del websocket de la librería): el estado ya
    está guardado y el lanzador decide si reinicia según el código."""
    logging.shutdown()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(codigo)


if __name__ == "__main__":
    try:
        _codigo = main() or 0
    except SystemExit as e:
        _codigo = e.code
        if isinstance(_codigo, str):
            print(ocultar_secretos(_codigo), file=sys.stderr)
            _codigo = SALIDA_DEFINITIVA
    except KeyboardInterrupt:
        _codigo = 0
    except BaseException:  # noqa: BLE001  (fallo inesperado: se muestra y se sale con 1 para que el lanzador reinicie)
        import traceback
        print(ocultar_secretos(traceback.format_exc()), file=sys.stderr)
        _codigo = 1
    _salir(_codigo if isinstance(_codigo, int) else 0)
