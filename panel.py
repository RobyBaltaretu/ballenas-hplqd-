"""
Panel web del bot.   Uso:  python panel.py   ->  abre http://localhost:8080

Pestañas: Resumen (modo, cuenta, riesgo, posiciones del bot y externas, alertas) · Operaciones (registro
versionado del ledger con filtros, paginación, CSV y detalle de cada operación) · Aprendizaje · Estrategias
(etapa de promoción) · Ballenas · Observabilidad (SOLO salud del sistema: componentes, incidentes y registros
técnicos; nada de saldo ni operaciones) · Configuración. Cada indicador tiene un botón ⓘ con su explicación
(glosario central en /api/glosario).

Funciona aparte del bot: solo LEE los archivos que el bot escribe. Los comandos se dejan en
comandos_panel.json y el bot los ejecuta (y deja el resultado en comandos_resultado.json).

Seguridad:
  - Por defecto escucha solo en este equipo (127.0.0.1). Fuera de él exige PANEL_PASSWORD (Basic).
  - Las acciones (POST) exigen: token del panel en la cabecera X-Panel-Token (va dentro de la página),
    mismo origen (cabecera Origin), JSON de menos de 4 KB y un máximo de 10 por minuto.
  - Visto desde fuera (otra IP o a través de un túnel), el panel es de SOLO LECTURA salvo que
    panel.comandos_remotos sea true.
  - Solo comandos de una lista cerrada. Cerrar posiciones exige confirmación escrita y el alcance exacto;
    nunca toca posiciones externas. En SHADOW no hay órdenes: el cierre se rechaza con un error explícito.
  - Ningún botón ni endpoint puede saltarse las comprobaciones del modo real ni convertir shadow en real.
  - Sin CORS. Las respuestas ocultan secretos.
"""

import base64
import csv
import hmac
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from compat import consola_utf8, reemplazar
import config_panel
from configuracion import enmascarar_direccion, sin_secretos
from feed import TIPOS as TIPOS_FEED
from feed import LectorFeed, filtro_desde_parametros
from ledger import Ledger
from observabilidad import COMPONENTES, leer_logs_estructurados, ocultar_secretos

AQUI = os.path.dirname(os.path.abspath(__file__))
MAX_CUERPO = 4096
MAX_CUERPO_CONFIG = 16384
MAX_POST_MINUTO = 10
COMANDOS = ("/pausa", "/reanudar", "/cerrarbot")
CONFIRMACION_CIERRE = "CERRAR-BOT"
ANTIGUEDAD_MAX_DATOS = 120          # s: más viejo que esto -> "datos desactualizados"


def ruta(nombre):
    return os.path.join(AQUI, nombre)


def leer_json(nombre, defecto):
    try:
        with open(ruta(nombre), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):  # no existe o se está escribiendo justo ahora
        return defecto


def limpio(obj):
    """Lo que sale del panel nunca lleva secretos ni direcciones completas (0x + 40 hex se enmascaran)."""
    from feed import _limpiar
    return _limpiar(json.loads(json.dumps(sin_secretos(obj), default=str, ensure_ascii=False)))


# ================================================================ glosario (ayuda ⓘ)
GLOSARIO = {
    "modo": ("Modo de funcionamiento",
             "SIMULADO: dinero ficticio con precios reales. SHADOW: solo observa y apunta lo que habría hecho; no puede "
             "enviar órdenes. REAL: dinero real en una subcuenta dedicada, con estrategias aprobadas por una persona."),
    "equity": ("Valor de la cuenta (equity)",
               "Saldo más el resultado de las posiciones abiertas. En real se limita al capital del piloto para "
               "calcular tamaños. La hora indica cuándo se leyó."),
    "riesgo_abierto": ("Riesgo abierto",
                       "Lo que se perdería, en % del valor de la cuenta, si saltaran a la vez todos los stops de las "
                       "posiciones del bot (incluye comisiones y un margen por deslizamiento)."),
    "exposicion_bruta": ("Exposición bruta", "Suma del tamaño en USD de todas las posiciones (bot y externas), sin "
                                              "importar el lado, en % del valor de la cuenta."),
    "exposicion_neta": ("Exposición neta", "Largos menos cortos, en % del valor de la cuenta. Cerca de 0 = cubierto."),
    "externa": ("Posición externa",
                "Posición o orden que el bot no puede atribuirse (no está en su registro). Se muestra y cuenta como "
                "riesgo, pero el bot NUNCA la cierra, ni le pone stop, ni cancela sus órdenes."),
    "stop_verificado": ("Stop verificado",
                        "El bot comprueba en cada ciclo que el stop existe en el exchange con el tamaño, el lado y el "
                        "precio correctos. Si no, alerta, bloquea entradas nuevas y lo repone."),
    "stop_mark": ("Stop y precio de marca",
                  "Los stops de Hyperliquid se activan con el precio de marca (mark) y se ejecutan a mercado: el "
                  "precio real de salida puede ser peor que el del stop (deslizamiento)."),
    "liquidacion": ("Precio de liquidación",
                    "Si el precio llega aquí, el exchange cierra la posición. El bot exige que esté bastante más lejos "
                    "que el stop (distancia mínima configurable)."),
    "r": ("R (unidades de riesgo)",
          "Resultado dividido por lo que se arriesgaba al entrar (distancia al stop inicial × tamaño). +1 R = se ganó "
          "lo mismo que se arriesgaba. R bruto: antes de comisiones y funding; R neto: después."),
    "pnl_neto": ("Resultado neto", "PnL de los fills (closedPnl) menos comisiones más/menos funding. Los depósitos, "
                                   "retiradas y transferencias NO cuentan como resultado."),
    "funding": ("Funding", "Pago horario entre largos y cortos. Solo se asigna a una operación cuando no hay ambigüedad; "
                           "si no, queda como 'sin asignar'."),
    "deslizamiento": ("Deslizamiento", "Diferencia entre el precio de referencia de la señal y el precio medio real de "
                                       "entrada, en %. Positivo = entrada peor de lo previsto."),
    "congelado": ("Mercado congelado",
                  "El bot no está seguro del estado de ese mercado (respuesta ambigua del exchange o la cuenta no cuadra "
                  "con el registro). No abre nada ahí hasta que la conciliación lo aclare."),
    "conciliacion": ("Conciliación", "Comparación en cada ciclo entre el registro del bot y lo que hay de verdad en la "
                                     "cuenta (posiciones, órdenes y stops)."),
    "bloqueos": ("Entradas bloqueadas", "Motivos por los que ahora mismo no se abren operaciones nuevas. Las abiertas "
                                        "siguen gestionándose y protegidas."),
    "limite_diario": ("Pérdida diaria / semanal / caída", "Límites duros en hora UTC. Al alcanzarlos se bloquean las "
                                                          "entradas hasta el día o semana siguiente."),
    "etapa": ("Etapa de promoción",
              "investigación → laboratorio → shadow → paper → apta para revisión → real habilitado. Solo una persona "
              "aprueba las dos últimas (promocion.py). Cualquier cambio de código, parámetros, costes o universo la "
              "invalida."),
    "huella": ("Huella", "Resumen de la definición exacta de la estrategia, de toda la configuración que decide o "
                         "arriesga (riesgo, órdenes, salidas, costes, universo, filtros, límites del piloto...) y del "
                         "código. Si cambia algo, la aprobación anterior deja de valer; si no se puede calcular, nada "
                         "está autorizado."),
    "feed": ("Señales en vivo",
             "Flujo persistente de eventos (registro/feed.jsonl): señales, decisiones con sus motivos, órdenes y su estado, "
             "protección, entradas, salidas y cierres, y lo observado del mercado y de las ballenas. Cada evento lleva su "
             "procedencia, hora del exchange (si existe), hora de recepción y de proceso y frescura. 'Inferido' = deducido, "
             "no confirmado: un cambio de posición de una ballena entre dos fotos NO es un fill, una operación pública grande "
             "NO revela la posición neta de nadie y una 'posible liquidación' es una inferencia por precio e interés abierto. "
             "Se actualiza por push (SSE) y, si no es posible, por sondeo cada 3 s; al reconectar continúa desde el último "
             "evento recibido, sin perder ni repetir. No es una fuente de predicciones."),
    "investigacion": ("Departamento de investigación",
                      "Cada noche el bot genera ideas nuevas combinando disparadores, filtros y salidas, y las somete al "
                      "laboratorio completo (validación fuera de muestra, holdout, comparación con azar y costes al doble). "
                      "Como probar muchas ideas hace que alguna salga bien por suerte, la t exigida sube con el total probado. "
                      "Las que pasan se siguen en vivo sin operar durante 30 operaciones; si fallan, al cementerio (no se "
                      "vuelven a probar); si aguantan, quedan pendientes de tu revisión. Nunca se activa nada solo."),
    "vigilancia": ("Vigilancia de estrategias",
                   "Cada hora, con las operaciones cerradas de los últimos 60 días (al menos 30), se calcula una cota "
                   "superior del R medio con bootstrap por bloques al 95 % corregido por las estrategias vigiladas. Si "
                   "incluso esa cota es negativa: tamaño ×0,5, se retira la autorización para real si la tenía y avisa. "
                   "Recupera poco a poco: ×0,5 al dejar de estar degradada, ×0,75 tras 7 días sana y normal tras otros 7. "
                   "Nunca por encima del tamaño autorizado; la autorización solo la devuelves tú."),
    "filtro_aprendizaje": ("Evaluación del filtro de aprendizaje",
                           "El bot apunta, para CADA señal, si el aprendizaje la habría aceptado o rechazado, y sigue su "
                           "resultado (operada o en papel). Si las aceptadas no salen claramente mejor que las rechazadas, "
                           "el filtro no aporta y conviene desactivarlo (aprendizaje.activo = false). Lo decides tú."),
    "laboratorio": ("Laboratorio", "Prueba con datos históricos usando walk-forward, holdout final, purga y embargo. "
                                   "Superarlo es evidencia, no permiso para operar con dinero real."),
    "shadow": ("Resultados shadow", "Lo que habría pasado según precios reales y costes SUPUESTOS. Son estimaciones "
                                    "modeladas, no ejecuciones."),
    "ballena_etiqueta": ("Observación / inferencia / validada",
                         "Observación: lo que una ballena tiene o ha movido. Inferencia: una puntuación calculada sobre "
                         "su historial. Validada: superó la prueba fuera de muestra del laboratorio. Ninguna es por sí "
                         "sola una recomendación."),
    "latido": ("Latido", "Última vez que el bucle principal completó un ciclo. Si es viejo, el bot puede estar parado o "
                         "bloqueado."),
    "estado_componente": ("Estados de un componente",
                          "correcto · degradado (funciona con problemas) · fallido · sin datos (aún no informó) · "
                          "desactualizado (su último dato es viejo) · desactivado (apagado en config)."),
    "incidente": ("Incidente", "Problema técnico agrupado: se cuenta cuántas veces se repite y desde cuándo. Puedes "
                               "marcarlo como reconocido; se resuelve solo cuando el componente vuelve a estar bien."),
    "traza": ("Traza", "Identificador de un ciclo del bot. Filtra los registros por traza para ver todo lo que pasó en "
                       "ese ciclo."),
    "ciclo_p95": ("Duración del ciclo", "Mediana y percentil 95 de lo que tarda un ciclo. Si crece mucho, el bot "
                                        "reacciona más tarde."),
    "contadores": ("Contadores", "429: respuestas 'demasiadas peticiones' de Hyperliquid. Esperas: pausas del limitador "
                                 "para no llegar a ese límite. Reconexiones del websocket. Errores de la API."),
}


# ================================================================ aprendizaje (igual que antes)
def resumen(lista):
    if not lista:
        return {"n": 0, "ganan": 0, "media": 0, "total": 0}
    n = len(lista)
    return {"n": n, "ganan": round(sum(1 for x in lista if x > 0) / n * 100),
            "media": round(sum(lista) / n, 3), "total": round(sum(lista), 2)}


_CACHE_FILTRO = {"firma": None, "datos": None}


def evaluacion_filtro(regs):
    """Veredicto sobre el filtro de aprendizaje (se recalcula solo si aprendizaje.json cambia)."""
    from aprendizaje import evaluar_filtro
    try:
        firma = os.path.getmtime(ruta("aprendizaje.json"))
    except OSError:
        firma = None
    if firma != _CACHE_FILTRO["firma"]:
        _CACHE_FILTRO.update(firma=firma, datos=evaluar_filtro(regs))
    return _CACHE_FILTRO["datos"]


def datos_aprendizaje():
    regs = [r for r in leer_json("aprendizaje.json", {"registros": []})["registros"] if r["tipo"] != "backtest"]
    lab = leer_json("aprendizaje_lab.json", {"registros": []})["registros"]
    cerrados = [r for r in regs + lab if r.get("resultado_r") is not None]
    por_tipo = {t: resumen([r["resultado_r"] for r in cerrados if r["tipo"] == t])
                for t in ("real", "sombra", "shadow", "backtest")}
    filtro = defaultdict(list)
    estrategia_todo = defaultdict(lambda: defaultdict(list))
    for r in cerrados:
        if r["tipo"] == "sombra":
            for m in r.get("motivos_descarte") or []:
                filtro[re.sub(r"\s+", " ", re.sub(r"[-+]?\d[\d.,]*(\s*%)?", "N", m.split(":")[0])).strip()[:60]].append(
                    r["resultado_r"])
        estrategia_todo[r.get("modulo") or "ballenas (versión antigua)"][r["tipo"]].append(r["resultado_r"])
    sombras = sorted([r for r in regs if r["tipo"] == "sombra"], key=lambda r: r["ts"], reverse=True)[:100]
    campos = ("id", "tipo", "moneda", "lado", "ts", "cierre_ts", "resultado_r", "cierre", "motivos_descarte", "modulo",
              "trade_id")
    return {"por_tipo": por_tipo, "por_filtro": {k: resumen(v) for k, v in filtro.items()},
            "evaluacion_filtro": evaluacion_filtro(regs),
            "por_estrategia": {k: {t: resumen(x) for t, x in v.items()} for k, v in estrategia_todo.items()},
            "descartes": [{k: r.get(k) for k in campos} for r in sombras],
            "nota": "real = ejecutadas por el bot (en simulado: ficticias) · sombra = descartadas, seguidas en papel · "
                    "shadow = intenciones del modo shadow (modeladas) · backtest = laboratorio"}


# ================================================================ ledger (operaciones)
class CacheLedger:
    """Reconstruye las operaciones solo cuando el ledger cambia."""

    def __init__(self):
        self.ledger = Ledger(ruta("registro"))
        self.firma, self.ops, self.problemas = None, {}, []
        self.bloqueo = threading.Lock()

    def obtener(self):
        try:
            st = os.stat(self.ledger.ruta)
            firma = (st.st_mtime, st.st_size)
        except OSError:
            firma = None
        with self.bloqueo:
            if firma != self.firma:
                self.ops, self.problemas = self.ledger.operaciones()
                self.firma = firma
            return self.ops, self.problemas


CACHE = CacheLedger()
COLUMNAS_CSV = ("trade_id", "origen", "estado", "moneda", "lado", "estrategia", "version", "abierta_ts", "cerrada_ts",
                "precio_medio_entrada", "precio_medio_salida", "tam", "comisiones", "funding_total", "pnl_bruto",
                "pnl_neto", "r_bruto", "r_neto", "riesgo_inicial_usd", "deslizamiento_entrada_pct", "causa_salida",
                "motivo")


def a_epoch(v):
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


NO_EJECUTADAS = ("senal", "aprobada", "rechazada", "shadow", "shadow_cerrada")


def fila_operacion(op):
    f = {k: op.get(k) for k in COLUMNAS_CSV}
    f["abierta_ts"] = a_epoch(op.get("abierta_ts")) or a_epoch((op.get("eventos") or [{}])[0].get("ts"))
    f["cerrada_ts"] = a_epoch(op.get("cerrada_ts"))
    f["r_modelado"] = op.get("r_modelado")
    f["tam"] = op.get("tam_entrada") or op.get("tam")
    f["motivos_descarte"] = (op.get("decision") or {}).get("motivos") if op.get("estado") == "rechazada" else None
    f["n_alertas"] = len(op.get("alertas") or [])
    f["comisiones_estimadas"] = op.get("comisiones_estimadas")
    return f


def _f(params, k, defecto=""):
    return (params.get(k) or [defecto])[0]


def filtrar_operaciones(params):
    ops, problemas = CACHE.obtener()
    filas = [fila_operacion(o) for o in ops.values()]
    for campo in ("estado", "moneda", "lado", "estrategia", "origen"):
        v = _f(params, campo)
        if v:
            filas = [x for x in filas if str(x.get(campo) or "") == v]
    if _f(params, "solo_ejecutadas") == "1":
        filas = [x for x in filas if x["estado"] not in NO_EJECUTADAS]
    texto = _f(params, "q").lower()
    if texto:
        filas = [x for x in filas if texto in json.dumps(x, default=str, ensure_ascii=False).lower()]
    desde, hasta = float(_f(params, "desde", 0) or 0), float(_f(params, "hasta", 0) or 0)

    def ts_de(x):
        return x.get("abierta_ts") or x.get("cerrada_ts") or 0
    if desde:
        filas = [x for x in filas if (ts_de(x) or 0) >= desde]
    if hasta:
        filas = [x for x in filas if (ts_de(x) or 0) <= hasta]
    orden = _f(params, "orden", "reciente")
    filas.sort(key=lambda x: (x.get("abierta_ts") or 0, x["trade_id"]), reverse=orden != "antigua")
    return filas, problemas


def pagina_operaciones(params):
    filas, problemas = filtrar_operaciones(params)
    por = max(1, min(int(_f(params, "por_pagina", 50) or 50), 500))
    pagina = max(1, int(_f(params, "pagina", 1) or 1))
    total = len(filas)
    ejecutadas = [x for x in filas if x.get("estado") == "cerrada"]
    netos = [x["pnl_neto"] for x in ejecutadas if x.get("pnl_neto") is not None]
    rs = [x["r_neto"] for x in ejecutadas if x.get("r_neto") is not None]
    return {"total": total, "pagina": pagina, "por_pagina": por, "paginas": max(1, -(-total // por)),
            "filas": filas[(pagina - 1) * por: pagina * por],
            "resumen": {"cerradas": len(ejecutadas), "pnl_neto": round(sum(netos), 4) if netos else None,
                        "r_neto_medio": round(sum(rs) / len(rs), 3) if rs else None,
                        "ganadoras_pct": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1) if rs else None},
            "problemas_ledger": problemas[:20],
            "valores": {k: sorted({str(x.get(k)) for x in filas if x.get(k)}) for k in ("moneda", "estrategia", "origen")}}


def csv_operaciones(params):
    filas, _ = filtrar_operaciones(params)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(COLUMNAS_CSV), extrasaction="ignore")
    w.writeheader()
    from feed import _limpiar
    for f in filas:
        f = _limpiar(dict(f))           # exportación ordinaria: sin secretos ni direcciones completas
        for k in ("abierta_ts", "cerrada_ts"):
            if f.get(k):
                f[k] = datetime.fromtimestamp(f[k], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        w.writerow(f)
    return "﻿" + buf.getvalue()     # BOM: Excel en español abre bien los acentos


def detalle_operacion(trade_id):
    ops, _ = CACHE.obtener()
    op = ops.get(trade_id)
    if op is None:
        return None
    mem = next((r for r in leer_json("aprendizaje.json", {"registros": []})["registros"]
                if r.get("trade_id") == trade_id), None)
    return dict(op, aprendizaje=mem)


# ================================================================ detalle: estrategias, laboratorio, casos
def _sin_direcciones(obj):
    from feed import _limpiar
    return _limpiar(limpio(obj))


def detalle_estrategia(clave):
    """Todo lo que se sabe de una estrategia: laboratorio (fuera de muestra, holdout, líneas base, estrés, por
    moneda, por régimen...), su etapa y evidencia, lo aprendido y sus últimas operaciones."""
    lab = leer_json("estrategia_elegida.json", {})
    res = dict((lab.get("resultados") or {}).get(clave) or {})
    res.pop("elecciones_por_tramo", None)
    panel = leer_json("panel_datos.json", {})
    est = next((e for e in panel.get("estrategias") or [] if e.get("clave") == clave), None)
    if not res and est is None:
        return None
    regs = [r for r in leer_json("aprendizaje.json", {"registros": []})["registros"]
            if r.get("modulo") == clave and r.get("resultado_r") is not None]
    por_tipo = {t: resumen([r["resultado_r"] for r in regs if r["tipo"] == t]) for t in ("real", "sombra", "shadow")}
    ops, _ = CACHE.obtener()
    filas = sorted((fila_operacion(o) for o in ops.values() if o.get("estrategia") == clave),
                   key=lambda x: x.get("abierta_ts") or 0, reverse=True)[:30]
    return _sin_direcciones({"clave": clave, "laboratorio": res, "lab_generado": lab.get("generado"),
                             "lab_run_id": lab.get("run_id"), "estado": est,
                             "vigilancia": (panel.get("vigilancia") or {}).get(clave),
                             "factor": (panel.get("factor_estrategia") or {}).get(clave),
                             "aprendizaje": por_tipo, "operaciones": filas})


def resumen_laboratorio():
    lab = leer_json("estrategia_elegida.json", {})
    filas = []
    for k, r in (lab.get("resultados") or {}).items():
        oos, ho = r.get("fuera_de_muestra") or r.get("validacion") or {}, r.get("holdout") or {}
        filas.append({"clave": k, "estado": r.get("estado_evaluacion") or ("supera" if r.get("supera") else "no_supera"),
                      "motivo": (r.get("veredicto") or {}).get("motivo"), "oos_n": oos.get("n"),
                      "oos_r": oos.get("r_medio"), "oos_t": oos.get("t"), "oos_fb": oos.get("factor_beneficio"),
                      "oos_caida": oos.get("caida_max_r"), "holdout_n": ho.get("n"), "holdout_r": ho.get("r_medio"),
                      "variantes": r.get("variantes")})
    return {"generado": lab.get("generado"), "run_id": lab.get("run_id"), "elegidos": lab.get("elegidos"),
            "filas": sorted(filas, key=lambda x: -(x["oos_r"] if x["oos_r"] is not None else -9))}


def detalle_caso(caso_id):
    for nombre in ("aprendizaje.json", "aprendizaje_lab.json"):
        for r in leer_json(nombre, {"registros": []})["registros"]:
            if str(r.get("id")) == str(caso_id):
                return _sin_direcciones(r)
    return None


def esquema_config():
    cfg = leer_json("config.json", {})
    panel = leer_json("panel_datos.json", {})
    claves = [e["clave"] for e in panel.get("estrategias") or [] if e.get("clave")]
    if not claves:
        claves = sorted((leer_json("estrategia_elegida.json", {}).get("resultados") or {}).keys())
    conocidas = list(panel.get("mercados_disponibles") or []) or list((panel.get("motor") or {}).get("monedas") or [])
    return config_panel.esquema(cfg, claves, conocidas), claves


# ================================================================ observabilidad (solo sistema)
def datos_observabilidad(params):
    estado = leer_json("estado_sistema.json", {})
    incidentes = leer_json("incidentes.json", {"incidentes": []})["incidentes"]
    logs = leer_logs_estructurados(AQUI)
    nivel = _f(params, "nivel").upper()
    comp = _f(params, "componente")
    traza = _f(params, "traza")
    texto = _f(params, "q").lower()
    limite = max(1, min(int(_f(params, "limite", 300) or 300), 2000))
    orden = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    minimo = orden.get(nivel, 0)
    out = []
    for e in reversed(logs):
        if orden.get(e.get("nivel"), 1) < minimo:
            continue
        if comp and e.get("componente") != comp:
            continue
        if traza and e.get("traza") != traza:
            continue
        if texto and texto not in json.dumps(e, ensure_ascii=False).lower():
            continue
        out.append(e)
        if len(out) >= limite:
            break
    latido = estado.get("latido_ts")
    return {"ahora": time.time(), "sistema": estado, "incidentes": sorted(incidentes, key=lambda i: i["ultimo_ts"], reverse=True),
            "logs": out, "componentes_conocidos": COMPONENTES,
            "latido_antiguedad_seg": (time.time() - latido) if latido else None,
            "componentes_log": sorted({e.get("componente") for e in logs if e.get("componente")})}


def diagnostico():
    """Exportación para pedir ayuda: estado del sistema, incidentes y los últimos registros técnicos.
    Sin saldo, posiciones, operaciones ni secretos."""
    estado = leer_json("estado_sistema.json", {})
    return limpio({"generado": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sistema": estado,
                   "incidentes": leer_json("incidentes.json", {"incidentes": []})["incidentes"][-100:],
                   "logs": leer_logs_estructurados(AQUI, max_lineas=500),
                   "python": sys.version.split()[0], "plataforma": sys.platform})


# ================================================================ resumen
def todos_los_datos():
    bot = leer_json("panel_datos.json", {})
    sistema = leer_json("estado_sistema.json", {})
    edad = time.time() - bot.get("ts", 0) if bot else None
    if bot.get("ballenas"):
        for b in bot["ballenas"]:
            b.pop("direccion_completa", None)      # en pantalla, enmascarada
            b["direccion"] = enmascarar_direccion(b.get("direccion", ""))
    return limpio({
        "ahora": time.time(),
        "bot": bot,
        "edad_datos_seg": edad,
        "bot_vivo": edad is not None and edad < ANTIGUEDAD_MAX_DATOS,
        "sistema_resumen": {"estado": (sistema.get("proceso") or {}).get("estado"), "latido_ts": sistema.get("latido_ts"),
                            "incidentes_activos": sistema.get("incidentes_activos")},
        "aprendizaje": datos_aprendizaje(),
        "resultados_comandos": leer_json("comandos_resultado.json", {}),
    })


# ================================================================ feed «Señales en vivo»
LECTOR_FEED = LectorFeed(os.path.join(AQUI, "registro"))
SSE_DURACION_MAX = 300          # s: cada conexión SSE se cierra a los 5 min (el navegador reconecta solo, sin perder nada)
SSE_INTERVALO = 1.0             # s entre comprobaciones de eventos nuevos
SSE_LATIDO = 15                 # s entre comentarios de vida (para proxies/túneles)


def datos_feed(q):
    """Página incremental del feed. desde=<seq> -> eventos con seq mayor (orden creciente, sin duplicados).
    Sin 'desde' -> los últimos 'limite' (carga inicial). Incluye la frescura del bot para mostrar 'obsoleto'."""
    try:
        desde = int(_f(q, "desde", "-1"))
    except ValueError:
        desde = -1
    try:
        limite = max(1, min(1000, int(_f(q, "limite", "200"))))
    except ValueError:
        limite = 200
    evs, ultimo, primero, hay_mas = LECTOR_FEED.consulta(desde, limite, filtro_desde_parametros(q))
    panel = leer_json("panel_datos.json", {})
    return {"eventos": evs, "ultimo_seq": ultimo, "primer_seq_disponible": primero, "hay_mas": hay_mas,
            "perdidos": bool(desde >= 0 and primero and desde < primero - 1),
            "servidor_ts": time.time(), "bot_ts": panel.get("ts"),
            "bot_antiguedad_seg": round(time.time() - panel["ts"], 1) if panel.get("ts") else None,
            "obsoleto": not panel.get("ts") or time.time() - panel["ts"] > ANTIGUEDAD_MAX_DATOS}


# ================================================================ servidor
REGISTRO_ACCESO = None


def configurar_registro_acceso():
    global REGISTRO_ACCESO
    import logging
    from logging.handlers import RotatingFileHandler
    os.makedirs(ruta("logs"), exist_ok=True)
    lg = logging.getLogger("panel.acceso")
    lg.propagate = False
    if not lg.handlers:
        h = RotatingFileHandler(ruta(os.path.join("logs", "panel_acceso.log")), maxBytes=2_000_000, backupCount=3,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(h)
    lg.setLevel(logging.INFO)
    REGISTRO_ACCESO = lg


def latido_panel():
    """Señal de vida del panel para que el bot (y comprobar.py) lo vigilen como proceso aparte."""
    while True:
        try:
            _escribir("panel_latido.json", {"ts": time.time(), "pid": os.getpid(), "peticiones": Manejador.peticiones,
                                            "errores": Manejador.errores})
        except OSError:
            pass
        time.sleep(30)


class Limite:
    def __init__(self, n, ventana):
        self.n, self.ventana, self.eventos = n, ventana, defaultdict(deque)
        self.bloqueo = threading.Lock()

    def permitir(self, clave):
        ahora = time.time()
        with self.bloqueo:
            q = self.eventos[clave]
            while q and ahora - q[0] > self.ventana:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(ahora)
            return True


class Manejador(BaseHTTPRequestHandler):
    clave = ""
    token = secrets.token_urlsafe(24)
    comandos_habilitados = True
    comandos_remotos = False
    limite = Limite(MAX_POST_MINUTO, 60)
    server_version = "panel"
    sys_version = ""

    peticiones = 0
    errores = 0

    def log_message(self, formato, *args):
        """Registro de accesos y errores (logs/panel_acceso.log). Sin cabeceras (ni credenciales ni token) y con
        secretos y direcciones ocultos."""
        Manejador.peticiones += 1
        try:
            texto = formato % args
        except (TypeError, ValueError):
            texto = str(formato)
        if " 5" in texto[-12:] or " 4" in texto[-12:]:
            Manejador.errores += 1
        if REGISTRO_ACCESO is not None:
            from feed import _limpiar
            REGISTRO_ACCESO.info("%s %s", self.client_address[0], _limpiar(texto))

    # ---------------------------------------------------- utilidades
    def es_local(self):
        ip = self.client_address[0]
        por_tunel = any(self.headers.get(h) for h in ("X-Forwarded-For", "Cf-Connecting-Ip", "X-Real-Ip", "Forwarded"))
        return ip in ("127.0.0.1", "::1") and not por_tunel

    def _autorizado(self):
        if not self.clave:
            return True
        cab = self.headers.get("Authorization", "")
        if cab.startswith("Basic "):
            try:
                _, pw = base64.b64decode(cab[6:]).decode().split(":", 1)
                if hmac.compare_digest(pw.encode(), self.clave.encode()):
                    return True
            except Exception:  # noqa: BLE001
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Panel del bot", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _enviar(self, codigo, cuerpo, tipo, extra=None):
        datos = cuerpo.encode("utf-8") if isinstance(cuerpo, str) else cuerpo
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(datos)

    def _json(self, codigo, obj):
        self._enviar(codigo, json.dumps(obj, default=str, ensure_ascii=False), "application/json; charset=utf-8")

    # ---------------------------------------------------- GET
    def do_GET(self):
        if not self._autorizado():
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                with open(ruta("panel.html"), encoding="utf-8") as f:
                    html = f.read().replace("{{TOKEN}}", self.token)
                    html = html.replace("{{SOLO_LECTURA}}", "0" if self.puede_mandar() else "1")
                self._enviar(200, html, "text/html; charset=utf-8")
            elif url.path == "/api/datos":
                self._json(200, todos_los_datos())
            elif url.path == "/api/operaciones":
                if _f(q, "formato") == "csv":
                    nombre = f"operaciones-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.csv"
                    self._enviar(200, csv_operaciones(q), "text/csv; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="{nombre}"'})
                else:
                    self._json(200, limpio(pagina_operaciones(q)))
            elif url.path == "/api/operacion":
                d = detalle_operacion(_f(q, "id"))
                self._json(200 if d else 404, limpio(d) if d else {"error": "operación no encontrada"})
            elif url.path == "/api/observabilidad":
                self._json(200, limpio(datos_observabilidad(q)))
            elif url.path == "/api/diagnostico":
                self._enviar(200, json.dumps(diagnostico(), indent=1, ensure_ascii=False, default=str),
                             "application/json; charset=utf-8",
                             {"Content-Disposition": 'attachment; filename="diagnostico-bot.json"'})
            elif url.path == "/api/feed":
                self._json(200, datos_feed(q))
            elif url.path == "/api/feed/tipos":
                self._json(200, TIPOS_FEED)
            elif url.path == "/api/feed/stream":
                self._sse(q)
            elif url.path == "/api/config":
                esq, _ = esquema_config()
                esq["puede_editar"] = self.puede_mandar()
                self._json(200, esq)
            elif url.path == "/api/config/historial":
                self._json(200, {"cambios": config_panel.historial(AQUI)})
            elif url.path == "/api/estrategia":
                d = detalle_estrategia(_f(q, "clave"))
                self._json(200, d) if d else self._json(404, {"error": "estrategia desconocida"})
            elif url.path == "/api/laboratorio":
                self._json(200, resumen_laboratorio())
            elif url.path == "/api/caso":
                d = detalle_caso(_f(q, "id"))
                self._json(200, d) if d else self._json(404, {"error": "caso no encontrado"})
            elif url.path == "/api/glosario":
                self._json(200, {k: {"titulo": t, "texto": x} for k, (t, x) in GLOSARIO.items()})
            elif url.path == "/api/resultado":
                self._json(200, leer_json("comandos_resultado.json", {}).get(_f(q, "id"), {"pendiente": True}))
            else:
                self._enviar(404, "No encontrado", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": ocultar_secretos(str(e))})

    sse_activas = 0
    sse_max = 8
    sse_bloqueo = threading.Lock()

    def _sse(self, q):
        with Manejador.sse_bloqueo:
            if Manejador.sse_activas >= Manejador.sse_max:
                return self._json(503, {"error": "demasiadas conexiones en vivo; usa el sondeo"})
            Manejador.sse_activas += 1
        try:
            return self._sse_bucle(q)
        finally:
            with Manejador.sse_bloqueo:
                Manejador.sse_activas -= 1

    def _sse_bucle(self, q):
        """Server-Sent Events: empuja los eventos nuevos en cuanto el bot los escribe (comprueba cada segundo).
        Reanuda desde el cursor (?desde= o la cabecera Last-Event-ID) sin perder ni repetir eventos."""
        try:
            desde = int(self.headers.get("Last-Event-ID") or _f(q, "desde", "0"))
        except ValueError:
            desde = 0
        filtro = filtro_desde_parametros(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        inicio = latido = time.time()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while time.time() - inicio < SSE_DURACION_MAX:
                evs, ultimo, _, _ = LECTOR_FEED.consulta(desde, 500, filtro)
                for ev in evs:
                    datos = json.dumps(ev, ensure_ascii=False, default=str)
                    self.wfile.write(f"id: {ev['seq']}\nevent: evento\ndata: {datos}\n\n".encode("utf-8"))
                    desde = ev["seq"]
                if not evs and ultimo > desde:
                    desde = ultimo           # hay eventos nuevos que no pasan el filtro: el cursor avanza igual
                if evs:
                    self.wfile.flush()
                if time.time() - latido >= SSE_LATIDO:
                    panel = leer_json("panel_datos.json", {})
                    estado = {"servidor_ts": time.time(), "bot_ts": panel.get("ts"), "ultimo_seq": ultimo}
                    self.wfile.write(f"event: estado\ndata: {json.dumps(estado)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    latido = time.time()
                time.sleep(SSE_INTERVALO)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ---------------------------------------------------- POST
    def puede_mandar(self):
        return self.comandos_habilitados and (self.es_local() or self.comandos_remotos)

    def _origen_valido(self):
        origen = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origen is None:
            return self.headers.get("Sec-Fetch-Site") in (None, "same-origin")
        return origen in (f"http://{host}", f"https://{host}")

    def do_POST(self):
        if not self._autorizado():
            return
        url = urlparse(self.path)
        if url.path not in ("/api/comando", "/api/incidente", "/api/config", "/api/config/restaurar"):
            return self._enviar(404, "No encontrado", "text/plain; charset=utf-8")
        if not hmac.compare_digest(self.headers.get("X-Panel-Token", ""), self.token):
            return self._json(403, {"ok": False, "error": "falta el token del panel (recarga la página)"})
        if not self._origen_valido():
            return self._json(403, {"ok": False, "error": "origen no permitido"})
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(415, {"ok": False, "error": "se esperaba JSON"})
        if not self.limite.permitir(self.client_address[0]):
            return self._json(429, {"ok": False, "error": "demasiadas acciones seguidas; espera un minuto"})
        try:
            largo = int(self.headers.get("Content-Length", 0))
        except ValueError:
            largo = -1
        maximo = MAX_CUERPO_CONFIG if url.path.startswith("/api/config") else MAX_CUERPO
        if largo < 0 or largo > maximo:
            return self._json(413, {"ok": False, "error": "petición demasiado grande"})
        try:
            cuerpo = json.loads(self.rfile.read(largo) or b"{}")
            if not isinstance(cuerpo, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"ok": False, "error": "JSON no válido"})
        if url.path == "/api/incidente":
            return self._reconocer(cuerpo)
        if not self.puede_mandar():
            return self._json(403, {"ok": False, "error": "panel en solo lectura desde fuera de este equipo "
                                                          "(panel.comandos_remotos = false)"})
        if url.path == "/api/config":
            return self._config(cuerpo)
        if url.path == "/api/config/restaurar":
            return self._restaurar(cuerpo)
        return self._comando(cuerpo)

    def _pedir_aplicar(self):
        orden = {"cmd": "/aplicarconfig", "id": secrets.token_hex(8), "ts": time.time()}
        pendientes = leer_json("comandos_panel.json", [])
        if not isinstance(pendientes, list):
            pendientes = []
        pendientes.append(orden)
        _escribir("comandos_panel.json", pendientes[-20:])
        return orden["id"]

    def _origen_txt(self):
        return "panel (este equipo)" if self.es_local() else "panel (remoto)"

    def _config(self, cuerpo):
        """Dos pasos: 'previsualizar' devuelve qué cambiaría (y si sube el riesgo) sin guardar; después se guarda
        con 'confirmar': true. Todo pasa por la lista blanca y la validación completa."""
        _, claves = esquema_config()
        try:
            actual = config_panel.conf.cargar(ruta("config.json"))
            nuevo, lista = config_panel.aplicar_cambios(actual, cuerpo.get("cambios"), claves)
        except config_panel.ErrorCambio as e:
            return self._json(400, {"ok": False, "errores": e.errores, "error": "; ".join(e.errores)})
        except config_panel.conf.ErrorConfig as e:
            return self._json(400, {"ok": False, "error": str(e)})
        if not cuerpo.get("confirmar"):
            return self._json(200, {"ok": True, "previsualizacion": True, "cambios": lista,
                                    "sube_riesgo": any(x.get("sube_riesgo") for x in lista)})
        copia = config_panel.guardar(AQUI, nuevo, lista, self._origen_txt())
        cid = self._pedir_aplicar()
        return self._json(202, {"ok": True, "id": cid, "cambios": lista, "copia_anterior": copia,
                                "mensaje": "Guardado. El bot lo valida y se reinicia para aplicarlo."})

    def _restaurar(self, cuerpo):
        _, claves = esquema_config()
        try:
            copia, lista = config_panel.restaurar(AQUI, str(cuerpo.get("copia", ""))[:64], self._origen_txt(), claves)
        except config_panel.ErrorCambio as e:
            return self._json(400, {"ok": False, "error": "; ".join(e.errores)})
        cid = self._pedir_aplicar()
        return self._json(202, {"ok": True, "id": cid, "cambios": lista, "copia_anterior": copia,
                                "mensaje": "Copia restaurada. El bot la valida y se reinicia para aplicarla."})

    def _reconocer(self, cuerpo):
        if not self.puede_mandar():
            return self._json(403, {"ok": False, "error": "solo lectura"})
        inc_id = str(cuerpo.get("id", ""))[:64]
        datos = leer_json("incidentes.json", {"incidentes": []})
        ok = False
        for inc in datos["incidentes"]:
            if inc["id"] == inc_id and inc["estado"] == "activo":
                inc["estado"], ok = "reconocido", True
        if ok:
            pendientes = leer_json("incidentes_reconocidos.json", [])
            pendientes.append(inc_id)
            _escribir("incidentes_reconocidos.json", pendientes[-200:])
        return self._json(200 if ok else 404, {"ok": ok})

    def _comando(self, cuerpo):
        cmd = cuerpo.get("cmd")
        if cmd not in COMANDOS:
            return self._json(400, {"ok": False, "error": "comando no permitido"})
        datos = leer_json("panel_datos.json", {})
        orden = {"cmd": cmd, "id": secrets.token_hex(8), "ts": time.time()}
        if cmd == "/cerrarbot":
            if datos.get("modo") == "shadow":
                return self._json(409, {"ok": False, "error": "SHADOW: órdenes deshabilitadas. Este modo no tiene "
                                                              "posiciones que cerrar."})
            alcance = sorted(p["moneda"] for p in datos.get("posiciones", []))
            pedido = cuerpo.get("alcance")
            if cuerpo.get("confirmacion") != CONFIRMACION_CIERRE:
                return self._json(400, {"ok": False, "error": f"escribe {CONFIRMACION_CIERRE} para confirmar",
                                        "alcance": alcance})
            if not isinstance(pedido, list) or sorted(str(x) for x in pedido) != alcance:
                return self._json(409, {"ok": False, "error": "las posiciones del bot han cambiado: revisa el alcance",
                                        "alcance": alcance})
            orden.update(confirmacion=CONFIRMACION_CIERRE, alcance=alcance)
        pendientes = leer_json("comandos_panel.json", [])
        if not isinstance(pendientes, list):
            pendientes = []
        pendientes.append(orden)
        _escribir("comandos_panel.json", pendientes[-20:])
        return self._json(202, {"ok": True, "id": orden["id"], "mensaje": "enviado al bot; espera su confirmación"})


def _escribir(nombre, datos):
    tmp = ruta(nombre + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f)
    reemplazar(tmp, ruta(nombre))


def ip_local():
    """IP del equipo en la red de casa (no envía nada por internet)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("8.8.8.8", 80))
            return sk.getsockname()[0]
    except OSError:
        return None


def main():
    consola_utf8()
    try:
        from dotenv import load_dotenv
        load_dotenv(ruta(".env"))
    except ImportError:
        pass
    cfg = leer_json("config.json", {}).get("panel", {})
    host, puerto = cfg.get("host", "127.0.0.1"), int(cfg.get("puerto", 8080))
    Manejador.clave = os.getenv("PANEL_PASSWORD", "").strip()
    Manejador.comandos_habilitados = bool(cfg.get("comandos_habilitados", True))
    Manejador.comandos_remotos = bool(cfg.get("comandos_remotos", False))
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Fuera de este equipo el panel iría por HTTP SIN cifrar: la contraseña (Basic) viajaría legible.
        # Lo seguro es dejarlo en 127.0.0.1 y entrar por un túnel cifrado (Cloudflare Tunnel, ver la guía).
        if not Manejador.clave:
            raise SystemExit("El panel escucha fuera de tu equipo: pon PANEL_PASSWORD=... en .env antes de arrancarlo.")
        if not cfg.get("permitir_http_red", False):
            raise SystemExit("El panel solo admite 127.0.0.1 salvo que pongas panel.permitir_http_red = true en "
                             "config.json. Mejor: déjalo en 127.0.0.1 y usa un túnel cifrado (Cloudflare Tunnel). "
                             "Por HTTP directo la contraseña viaja SIN cifrar y no es seguro.")
        print("AVISO: panel por HTTP sin cifrar en la red local. La contraseña viaja legible; úsalo solo en tu red de "
              "confianza y nunca abras el puerto en el router.")
    configurar_registro_acceso()
    threading.Thread(target=latido_panel, daemon=True).start()
    servidor = ThreadingHTTPServer((host, puerto), Manejador)
    if "--abrir" in sys.argv:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, [f"http://localhost:{puerto}"]).start()
    print(f"Panel en http://localhost:{puerto}  (Ctrl+C para cerrar)")
    if host == "0.0.0.0":
        ip = ip_local()
        if ip:
            print(f"Desde otro equipo de tu wifi: http://{ip}:{puerto}  (pide PANEL_PASSWORD; solo lectura salvo "
                  f"panel.comandos_remotos)")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
