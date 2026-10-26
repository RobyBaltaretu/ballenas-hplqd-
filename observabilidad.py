"""
Observabilidad del SISTEMA (no del trading): salud de cada componente, latido, incidentes agrupados, métricas
del ciclo y registros estructurados con identificador de traza.

Aquí NO hay saldo, PnL, posiciones, órdenes ni fills: eso es de Operaciones/Riesgo. Los mensajes de trading
van por el logger "bot.operaciones" y se excluyen de esta vista.

Estados de un componente:
  correcto        funciona y sus datos son recientes
  degradado       funciona con problemas (reintentos, errores sueltos, parte de los datos)
  fallido         no funciona
  sin_datos       todavía no ha informado nada (no se presenta como "correcto")
  desactualizado  su último dato es más viejo que su umbral
  desactivado     apagado por configuración

Estado GLOBAL (proceso): se calcula con los componentes OBLIGATORIOS de cada modo (ciclo, API, precios,
almacenamiento y, si hay órdenes, ejecución y conciliación; más el panel):
  fallido      algún obligatorio ha fallado
  degradado    alguno degradado o desactualizado (o un opcional ha fallado)
  desconocido  algún obligatorio aún no ha informado (sin_datos): NUNCA se presenta como "correcto"
  correcto     todos los obligatorios informan y están bien
"""

import contextvars
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

ESTADOS = ("correcto", "degradado", "fallido", "sin_datos", "desactualizado", "desactivado", "desconocido")
OBLIGATORIOS_BASE = ("proceso", "ciclo", "api_rest", "datos_mercado", "almacenamiento", "panel")
COMPONENTES = {
    "proceso": "El propio proceso del bot",
    "ciclo": "Bucle principal (planificador)",
    "api_rest": "API REST de Hyperliquid",
    "websocket": "Conexión en tiempo real (websocket)",
    "datos_mercado": "Precios y velas",
    "ballenas": "Recolector de ballenas",
    "grabacion_mercado": "Grabación de interés abierto y operaciones grandes",
    "almacenamiento": "Disco y archivos de estado",
    "notificaciones": "Telegram",
    "ejecucion": "Envío y verificación de órdenes",
    "conciliacion": "Comparación registro ↔ exchange",
    "latido_externo": "Aviso de caída externo (Healthchecks)",
    "panel": "Panel web (proceso aparte; se vigila por su latido)",
}
traza_actual = contextvars.ContextVar("traza", default=None)

PATRONES_SECRETOS = [
    (re.compile(r"0x[0-9a-fA-F]{64}"), "0x<clave-oculta>"),                       # claves privadas
    (re.compile(r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}"), "<token-oculto>"),        # tokens de Telegram (también en .../bot<token>/...)
    (re.compile(r"(?i)(password|contrase[ñn]a|token|secret|api[_-]?key|private[_-]?key)\s*[=:]\s*\S+"), r"\1=<oculto>"),
    (re.compile(r"(?i)authorization:\s*\S+(\s+\S+)?"), "Authorization: <oculto>"),
    (re.compile(r"(?i)(hc-ping\.com/)[0-9a-z-]{8,}"), r"\1<oculto>"),                        # URL de Healthchecks
    # Direcciones de cuentas (0x + 40 hex): se enmascaran en registros, avisos, panel y exportaciones ordinarias
    (re.compile(r"0x([0-9a-fA-F]{4})[0-9a-fA-F]{32}([0-9a-fA-F]{4})(?![0-9a-fA-F])"), "0x\\1…\\2"),
]


def ocultar_secretos(texto):
    if not isinstance(texto, str):
        texto = str(texto)
    for patron, sustituto in PATRONES_SECRETOS:
        texto = patron.sub(sustituto, texto)
    return texto


def ahora_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


# ============================================================ logs
class FiltroSecretos(logging.Filter):
    def filter(self, record):
        record.msg = ocultar_secretos(record.getMessage())
        record.args = ()
        return True


class FormatoJSON(logging.Formatter):
    """Una línea JSON por registro: ts UTC, nivel, componente, mensaje, traza, trade_id, order_id."""

    def format(self, record):
        comp = getattr(record, "componente", None) or record.name.replace("bot.", "").replace("bot", "general")
        d = {"ts": ahora_iso(record.created), "nivel": record.levelname, "componente": comp,
             "mensaje": ocultar_secretos(record.getMessage()), "traza": getattr(record, "traza", None) or traza_actual.get()}
        for k in ("trade_id", "order_id"):
            v = getattr(record, k, None)
            if v:
                d[k] = v
        if record.exc_info:
            d["detalle"] = ocultar_secretos(self.formatException(record.exc_info))
        return json.dumps(d, ensure_ascii=False)


def configurar_logs(carpeta, nivel=logging.INFO, consola=True):
    """Log humano (bot.log, igual que siempre) + log estructurado (logs/bot.jsonl). Ambos ocultan secretos."""
    from logging.handlers import RotatingFileHandler
    os.makedirs(os.path.join(carpeta, "logs"), exist_ok=True)
    raiz = logging.getLogger("bot")
    raiz.setLevel(nivel)
    for h in list(raiz.handlers):
        raiz.removeHandler(h)
    humano = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    humano.converter = time.gmtime   # hora UTC también en el log humano
    manejadores = [(RotatingFileHandler(os.path.join(carpeta, "bot.log"), maxBytes=5_000_000, backupCount=3,
                                        encoding="utf-8"), humano),
                   (RotatingFileHandler(os.path.join(carpeta, "logs", "bot.jsonl"), maxBytes=10_000_000, backupCount=5,
                                        encoding="utf-8"), FormatoJSON())]
    if consola:
        manejadores.append((logging.StreamHandler(sys.stdout), humano))
    for h, fmt in manejadores:
        h.setFormatter(fmt)
        h.addFilter(FiltroSecretos())
        raiz.addHandler(h)
    return raiz


def leer_logs_estructurados(carpeta, max_lineas=20000, excluir_componentes=("operaciones",)):
    """Registros del sistema (sin los de trading), del más antiguo al más nuevo."""
    rutas = []
    base = os.path.join(carpeta, "logs", "bot.jsonl")
    for i in range(5, 0, -1):
        if os.path.exists(f"{base}.{i}"):
            rutas.append(f"{base}.{i}")
    if os.path.exists(base):
        rutas.append(base)
    out = deque(maxlen=max_lineas)
    for r in rutas:
        try:
            with open(r, encoding="utf-8", errors="replace") as fh:
                for linea in fh:
                    try:
                        d = json.loads(linea)
                    except ValueError:
                        continue
                    if d.get("componente") in excluir_componentes:
                        continue
                    out.append(d)
        except OSError:
            continue
    return list(out)


# ============================================================ salud e incidentes
class Sistema:
    def __init__(self, carpeta, umbrales=None, obligatorios=OBLIGATORIOS_BASE):
        self.carpeta = carpeta
        self.obligatorios = tuple(obligatorios)
        self.ruta = os.path.join(carpeta, "estado_sistema.json")
        self.ruta_inc = os.path.join(carpeta, "incidentes.json")
        self.bloqueo = threading.Lock()
        self.inicio = time.time()
        self.umbrales = umbrales or {}
        self.comp = {k: {"estado": "sin_datos", "detalle": "aún no ha informado", "ts": None, "latencia_ms": None}
                     for k in COMPONENTES}
        self.duraciones = deque(maxlen=500)
        self.ciclos = self.ciclos_fallidos = 0
        self.ultimo_ciclo_ok = None
        self.latido_ts = None
        self.contadores = {"reintentos_429": 0, "esperas_limite": 0, "reconexiones_ws": 0, "errores_api": 0}
        previo = self._leer(self.ruta, {})
        self.reinicios = (previo.get("proceso", {}).get("reinicios") or 0) + 1 if previo else 0
        self.incidentes = self._leer(self.ruta_inc, {"incidentes": []})["incidentes"]

    @staticmethod
    def _leer(ruta, defecto):
        try:
            with open(ruta, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return defecto

    def marcar(self, componente, estado, detalle="", latencia_ms=None, ts_dato=None):
        if estado not in ESTADOS:
            raise ValueError(estado)
        with self.bloqueo:
            self.comp[componente] = {"estado": estado, "detalle": ocultar_secretos(detalle), "ts": time.time(),
                                     "latencia_ms": latencia_ms, "ts_dato": ts_dato}

    def ciclo(self, duracion_s, ok):
        self.ciclos += 1
        self.duraciones.append(duracion_s)
        if ok:
            self.ultimo_ciclo_ok = time.time()
        else:
            self.ciclos_fallidos += 1
        self.latido_ts = time.time()

    # --------------------------------------------------- incidentes (agrupados por componente + clave)
    def incidente(self, componente, clave, severidad, mensaje, explicacion="", recomendacion=""):
        with self.bloqueo:
            for inc in self.incidentes:
                if inc["componente"] == componente and inc["clave"] == clave and inc["estado"] != "resuelto":
                    inc["repeticiones"] += 1
                    inc["ultimo_ts"] = time.time()
                    inc["mensaje"] = ocultar_secretos(mensaje)
                    return inc
            inc = {"id": f"inc-{int(time.time())}-{len(self.incidentes)}", "componente": componente, "clave": clave,
                   "severidad": severidad, "mensaje": ocultar_secretos(mensaje), "explicacion": explicacion,
                   "recomendacion": recomendacion, "primer_ts": time.time(), "ultimo_ts": time.time(),
                   "repeticiones": 1, "estado": "activo", "resuelto_ts": None}
            self.incidentes.append(inc)
            del self.incidentes[:-300]
            return inc

    def resolver(self, componente, clave):
        with self.bloqueo:
            for inc in self.incidentes:
                if inc["componente"] == componente and inc["clave"] == clave and inc["estado"] != "resuelto":
                    inc["estado"], inc["resuelto_ts"] = "resuelto", time.time()

    def reconocer(self, inc_id):
        with self.bloqueo:
            for inc in self.incidentes:
                if inc["id"] == inc_id and inc["estado"] == "activo":
                    inc["estado"] = "reconocido"
                    return True
        return False

    # --------------------------------------------------- foto
    def foto(self):
        ahora = time.time()
        comp = {}
        for k, v in self.comp.items():
            v = dict(v)
            umbral = self.umbrales.get(k)
            if v["ts"] is not None and umbral and v["estado"] in ("correcto", "degradado") and ahora - v["ts"] > umbral:
                v["estado"], v["detalle"] = "desactualizado", f"sin noticias desde hace {ahora - v['ts']:.0f} s"
            v["descripcion"] = COMPONENTES.get(k, k)
            comp[k] = v
        dur = sorted(self.duraciones)
        try:
            disco = shutil.disk_usage(self.carpeta)
            disco = {"libre_gb": round(disco.free / 1e9, 2), "total_gb": round(disco.total / 1e9, 2)}
        except OSError:
            disco = None
        ob = [k for k in self.obligatorios if k in comp]
        malos = [k for k in ob if comp[k]["estado"] == "fallido"]
        flojos = [k for k, v in comp.items() if v["estado"] in ("degradado", "desactualizado")
                  or (v["estado"] == "fallido" and k not in ob)]
        sin = [k for k in ob if comp[k]["estado"] in ("sin_datos", "desconocido")]
        estado = ("fallido" if malos else "degradado" if flojos else "desconocido" if sin or self.latido_ts is None
                  else "correcto")
        return {
            "ts": ahora,
            "estado_global": estado, "obligatorios": list(ob),
            "obligatorios_sin_datos": sin, "obligatorios_fallidos": malos,
            "proceso": {"estado": estado, "pid": os.getpid(), "inicio_ts": self.inicio, "activo_seg": ahora - self.inicio,
                        "reinicios": self.reinicios, "python": sys.version.split()[0], "sistema": sys.platform},
            "latido_ts": self.latido_ts, "ultimo_ciclo_ok_ts": self.ultimo_ciclo_ok,
            "ciclos": self.ciclos, "ciclos_fallidos": self.ciclos_fallidos,
            "ciclo_mediana_seg": dur[len(dur) // 2] if dur else None,
            "ciclo_p95_seg": dur[max(0, int(len(dur) * 0.95) - 1)] if len(dur) >= 20 else None,
            "componentes": comp, "contadores": dict(self.contadores), "disco": disco,
            "incidentes_activos": sum(1 for i in self.incidentes if i["estado"] == "activo"),
        }

    def escribir(self, extra=None):
        datos = self.foto()
        if extra:
            datos.update(extra)
        for ruta, contenido in ((self.ruta, datos), (self.ruta_inc, {"incidentes": self.incidentes})):
            tmp = ruta + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(contenido, fh, ensure_ascii=False, default=str)
                os.replace(tmp, ruta)
            except OSError:
                pass
        return datos
