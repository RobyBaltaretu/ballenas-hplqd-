"""
Editar la configuración desde el panel, con seguridad.

Qué se puede cambiar: SOLO los campos de CAMPOS (lista blanca), cada uno con su tipo y su rango. Lo demás de
config.json no se toca desde el panel. En concreto, NUNCA se puede cambiar desde aquí:
  - el modo (simulado / shadow / real), la red, el bloque "real" ni los límites del piloto real;
  - la seguridad del propio panel (host, puerto, comandos remotos...);
  - el saldo ficticio, las comisiones supuestas ni los parámetros estadísticos del laboratorio.
Así ningún botón puede convertir el bot en real ni saltarse las comprobaciones de promoción.

Cómo se aplica:
  1. Se parte del config.json ACTUAL, se aplican los cambios pedidos y se valida la configuración completa con
     las mismas reglas que al arrancar (configuracion.validar). Si algo no es válido, no se guarda nada.
  2. Se guarda una copia del config.json anterior en config_copias/ (las 30 últimas) y se escribe el nuevo de
     forma atómica. Cada cambio queda registrado (registro/cambios_config.jsonl): cuándo, desde dónde, qué campo,
     valor anterior y nuevo (las direcciones, enmascaradas).
  3. El panel pide al bot que aplique la configuración: el bot la vuelve a validar y se reinicia de forma
     ordenada (las posiciones conservan sus stops y se concilian al arrancar).
  4. Cualquier copia anterior se puede restaurar desde el panel (mismo proceso: validar, copiar, aplicar).

Aviso: cambiar algo que decide (riesgo, salidas, órdenes, monedas...) cambia la HUELLA de las estrategias, y las
aprobaciones y la evidencia shadow/paper acumulada con la huella anterior dejan de valer (promocion.py).
"""

import copy
import json
import os
import re
import time
from datetime import datetime, timezone

import configuracion as conf

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_COPIAS = "config_copias"
MAX_COPIAS = 30
RE_MONEDA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_\-]{0,23}$")
RE_DIRECCION = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Bloques que el panel NUNCA puede tocar (aunque alguien envíe la ruta a mano).
PROHIBIDOS = ("modo", "red", "real", "piloto_real", "panel", "saldo_simulado_usd", "shadow", "comisiones",
              "laboratorio", "version_config")


def _c(ruta, seccion, etiqueta, tipo, ayuda="", minimo=None, maximo=None, paso=None, opciones=None, unidad="",
       riesgo=None):
    """riesgo: 'sube' si un valor MAYOR aumenta el riesgo, 'baja' si un valor MENOR lo aumenta (para avisar)."""
    return {"ruta": ruta, "seccion": seccion, "etiqueta": etiqueta, "tipo": tipo, "ayuda": ayuda, "min": minimo,
            "max": maximo, "paso": paso, "opciones": opciones, "unidad": unidad, "riesgo": riesgo}


SECCIONES = [
    ("monedas", "Monedas", "Qué mercados vigila y opera el bot."),
    ("ballenas", "Ballenas", "Cuántas cuentas grandes sigue y cómo las elige. Las ballenas son investigación: "
                             "solo operan en papel si pones su modo en «simulado»."),
    ("estrategias", "Estrategias", "Qué estrategias generan señales en simulado/shadow. Para dinero real hace falta "
                                   "ADEMÁS una aprobación humana con promocion.py."),
    ("riesgo", "Riesgo y tamaño", "Cuánto arriesga cada operación y los límites que bloquean entradas."),
    ("salidas", "Salidas", "Objetivos, parcial, stop dinámico y punto de equilibrio (en R = distancia al stop)."),
    ("ordenes", "Órdenes", "Cómo entra: orden límite, espera y reserva a mercado."),
    ("aprendizaje", "Aprendizaje", "Filtro por casos parecidos del pasado."),
    ("capital", "Gestión del capital", "Reducciones automáticas de tamaño tras pérdidas o al cumplir el objetivo."),
    ("ritmo", "Ritmo y avisos", "Frecuencia del ciclo y umbrales de lo que se muestra en «Señales en vivo»."),
]

CAMPOS = [
    # ---- monedas
    _c("universo.max_monedas", "monedas", "Número máximo de monedas", "int",
       "Las de más volumen que cumplen el mínimo, más las que fuerces abajo.", 1, 100, 1),
    _c("universo.volumen_min_24h_usd", "monedas", "Volumen mínimo en 24 h", "num",
       "Mercados con menos volumen se ignoran (más difíciles de ejecutar y más deslizamiento).", 0, 1e11, 1e6, "USD",
       riesgo="baja"),
    _c("universo.incluir_siempre", "monedas", "Incluir siempre", "monedas",
       "Se vigilan aunque no estén entre las de más volumen (si existen en Hyperliquid)."),
    _c("universo.excluir", "monedas", "Excluir", "monedas", "Nunca se vigilan ni se operan."),
    _c("motor.intervalos", "monedas", "Velas que se evalúan", "multi",
       "Las estrategias generan señal al cierre de estas velas.", opciones=["1h", "4h"]),
    # ---- ballenas
    _c("seleccion_automatica", "ballenas", "Elegir ballenas automáticamente", "bool",
       "Del ranking de Hyperliquid, con los filtros de abajo."),
    _c("filtros_ballenas.numero_ballenas", "ballenas", "Número de ballenas a seguir", "int",
       "Más ballenas = más datos, pero más peticiones. En tiempo real solo caben 10 (límite de Hyperliquid); el "
       "resto se consulta periódicamente.", 1, 50, 1),
    _c("tiempo_real.max_ballenas_ws", "ballenas", "Ballenas en tiempo real", "int",
       "Seguidas por websocket (fills al instante). Hyperliquid admite 10 por IP.", 0, 10, 1),
    _c("filtros_ballenas.valor_cuenta_min_usd", "ballenas", "Valor mínimo de la cuenta", "num", "", 0, 1e10, 1e5, "USD"),
    _c("filtros_ballenas.pnl_historico_min_usd", "ballenas", "Beneficio histórico mínimo", "num", "", 0, 1e10, 1e5, "USD"),
    _c("filtros_ballenas.dias_max_sin_operar", "ballenas", "Días máximos sin operar", "int",
       "Descarta cuentas inactivas.", 1, 365, 1),
    _c("filtros_ballenas.candidatas_a_analizar", "ballenas", "Candidatas a analizar", "int",
       "Cuántas del ranking se estudian antes de elegir.", 5, 200, 5),
    _c("refrescar_ballenas_horas", "ballenas", "Renovar la selección cada", "num", "", 1, 168, 1, "h"),
    _c("ballenas_manuales", "ballenas", "Ballenas añadidas a mano", "direcciones",
       "Direcciones 0x… de cuentas públicas de Hyperliquid que quieres seguir siempre."),
    _c("ballenas.activo", "ballenas", "Seguir ballenas", "bool"),
    _c("ballenas.modo_senal", "ballenas", "Señales de ballenas", "opcion",
       "«investigación» solo las registra; «simulado» las opera en papel. Nunca operan en real sin aprobación.",
       opciones=["investigacion", "simulado"]),
    _c("ballenas.usar_como_veto", "ballenas", "Usar ballenas como veto", "bool",
       "Si una ballena grande va fuerte en contra, se descarta la señal de otra estrategia."),
    _c("ballenas.aumento_min_usd", "ballenas", "Aumento mínimo para señal", "num", "", 1000, 1e9, 10000, "USD"),
    _c("ballenas.flujo_min_usd", "ballenas", "Flujo mínimo agregado para señal", "num", "", 1000, 1e9, 10000, "USD"),
    # ---- estrategias (se completan con las del motor: estrategias.<clave>.activa)
    # ---- riesgo
    _c("riesgo.riesgo_por_operacion_pct", "riesgo", "Riesgo por operación", "num",
       "Lo que se pierde si salta el stop, como % del valor de la cuenta.", 0.05, 5, 0.05, "%", riesgo="sube"),
    _c("riesgo.riesgo_abierto_max_pct", "riesgo", "Riesgo abierto máximo", "num",
       "Suma del riesgo de todas las posiciones abiertas.", 0.1, 20, 0.1, "%", riesgo="sube"),
    _c("riesgo.max_posiciones_abiertas", "riesgo", "Posiciones abiertas a la vez", "int", "", 1, 20, 1, riesgo="sube"),
    _c("riesgo.max_mismo_lado", "riesgo", "Máximo en el mismo lado", "int",
       "Cuántas posiciones largas (o cortas) a la vez.", 1, 20, 1, riesgo="sube"),
    _c("riesgo.max_operaciones_dia", "riesgo", "Operaciones por día", "int", "", 1, 200, 1, riesgo="sube"),
    _c("riesgo.apalancamiento_max", "riesgo", "Apalancamiento máximo", "num",
       "No cambia el riesgo por operación (lo fija el stop), pero acerca la liquidación.", 1, 10, 1, "×", riesgo="sube"),
    _c("riesgo.sl_min_pct", "riesgo", "Stop más cercano admitido", "num",
       "Stops más cortos se descartan: las comisiones pesarían demasiado.", 0.05, 20, 0.05, "%", riesgo="baja"),
    _c("riesgo.sl_max_pct", "riesgo", "Stop más lejano admitido", "num", "", 0.5, 50, 0.5, "%", riesgo="sube"),
    _c("riesgo.duracion_max_global_h", "riesgo", "Duración máxima de una operación", "num", "", 1, 720, 1, "h"),
    _c("riesgo.perdida_diaria_max_pct", "riesgo", "Bloqueo por pérdida diaria", "num",
       "Al llegar, no se abre nada más hasta el día siguiente (UTC).", 0.5, 50, 0.5, "%", riesgo="sube"),
    _c("riesgo.perdida_semanal_max_pct", "riesgo", "Bloqueo por pérdida semanal", "num", "", 1, 80, 0.5, "%", riesgo="sube"),
    _c("riesgo.drawdown_max_pct", "riesgo", "Bloqueo por caída desde el máximo", "num", "", 1, 90, 1, "%", riesgo="sube"),
    _c("riesgo.perdida_max_total_pct", "riesgo", "Suelo de emergencia", "num",
       "Pérdida total que cierra las posiciones del bot y lo apaga.", 1, 90, 1, "%", riesgo="sube"),
    _c("riesgo.pausa_tras_perdidas_seguidas", "riesgo", "Pausa tras pérdidas seguidas", "int",
       "0 = desactivado.", 0, 100, 1),
    _c("riesgo.minutos_pausa_tras_racha", "riesgo", "Duración de esa pausa", "int", "", 0, 10080, 15, "min"),
    _c("riesgo.spread_max_pct", "riesgo", "Horquilla máxima", "num", "", 0.005, 5, 0.005, "%", riesgo="sube"),
    _c("riesgo.correlacion_max", "riesgo", "Correlación máxima con lo abierto", "num", "", 0, 1, 0.05, riesgo="sube"),
    _c("riesgo.coste_max_fraccion_riesgo", "riesgo", "Coste máximo frente al riesgo", "num",
       "Descarta si entrar y salir cuesta más de esta fracción de lo arriesgado. 0 = sin filtro.", 0, 1, 0.05,
       riesgo="sube"),
    _c("riesgo.impacto_max_pct", "riesgo", "Impacto máximo en el libro", "num", "", 0.01, 5, 0.01, "%", riesgo="sube"),
    _c("riesgo.exposicion_beta_btc_max_pct", "riesgo", "Exposición máxima a BTC (beta)", "num", "", 10, 2000, 10, "%",
       riesgo="sube"),
    # ---- salidas
    _c("salida.parcial_activo", "salidas", "Cobrar una parte en el primer objetivo", "bool"),
    _c("salida.tp1_r", "salidas", "Primer objetivo", "num", "", 0.25, 20, 0.25, "R"),
    _c("salida.parcial_pct", "salidas", "Parte que se cobra", "num", "", 5, 95, 5, "%"),
    _c("salida.tp_final_r", "salidas", "Objetivo final", "num", "", 0.5, 50, 0.25, "R"),
    _c("salida.trailing_r", "salidas", "Distancia del stop dinámico", "num", "", 0.25, 20, 0.25, "R"),
    _c("salida.breakeven_tras_r", "salidas", "Stop a equilibrio tras", "num", "", 0.25, 20, 0.25, "R"),
    _c("salida.precio_trailing", "salidas", "Precio del stop dinámico", "opcion",
       "El stop del exchange siempre se activa con la marca.", opciones=["mid", "mark"]),
    # ---- órdenes
    _c("ordenes.entrada_limite", "ordenes", "Entrar con orden límite (maker)", "bool",
       "Más barato; si no se llena a tiempo, se cancela (confirmado) y se completa a mercado."),
    _c("ordenes.espera_limite_seg", "ordenes", "Espera de la orden límite", "int", "", 2, 600, 1, "s"),
    _c("ordenes.reserva_a_mercado", "ordenes", "Completar a mercado si no se llena", "bool"),
    _c("ordenes.persecucion_max_pct", "ordenes", "Persecución máxima del precio", "num",
       "Si el precio se ha ido más que esto, no se completa a mercado.", 0, 10, 0.05, "%", riesgo="sube"),
    _c("ordenes.politica_sin_stop", "ordenes", "Si una posición queda sin stop", "opcion", "",
       opciones=["reponer_y_alertar", "cerrar"]),
    # ---- aprendizaje
    _c("aprendizaje.activo", "aprendizaje", "Filtro de aprendizaje activo", "bool",
       "Mira en «Aprendizaje» si de verdad aporta antes de decidir."),
    _c("aprendizaje.r_minimo_para_entrar", "aprendizaje", "R mínimo esperado para entrar", "num", "", -2, 2, 0.05, "R"),
    _c("aprendizaje.min_casos_parecidos", "aprendizaje", "Casos parecidos mínimos", "int", "", 1, 200, 1),
    _c("aprendizaje.explorar_en_simulado", "aprendizaje", "Explorar en simulado", "bool",
       "En simulado, deja pasar a tamaño reducido algunas señales que el filtro rechazaría (para seguir aprendiendo)."),
    # ---- capital
    _c("gestion_capital.perdida_diaria_pct", "capital", "Reducir tamaño tras perder en el día", "num", "", 0.5, 50, 0.5,
       "%"),
    _c("gestion_capital.factor_tras_perdida_diaria", "capital", "Tamaño tras esa pérdida", "num", "", 0.1, 1, 0.05, "×"),
    _c("gestion_capital.objetivo_diario_pct", "capital", "Objetivo diario", "num", "", 0.5, 50, 0.5, "%"),
    _c("gestion_capital.factor_tras_objetivo", "capital", "Tamaño tras cumplir el objetivo", "num", "", 0.1, 1, 0.05, "×"),
    # ---- ritmo y avisos
    _c("segundos_entre_ciclos", "ritmo", "Segundos entre ciclos", "int",
       "Menos = reacciona antes pero hace más peticiones.", 5, 600, 1, "s"),
    _c("enfriamiento_min", "ritmo", "Enfriamiento tras cerrar en una moneda", "int", "", 0, 1440, 5, "min"),
    _c("mercado.operacion_grande_usd", "ritmo", "Operación grande (feed)", "num", "", 1000, 1e9, 10000, "USD"),
    _c("feed.min_fill_ballena_usd", "ritmo", "Fill de ballena mínimo (feed)", "num", "", 0, 1e9, 1000, "USD"),
    _c("feed.min_cambio_ballena_usd", "ritmo", "Cambio de ballena mínimo (feed)", "num", "", 0, 1e9, 1000, "USD"),
]
POR_RUTA = {c["ruta"]: c for c in CAMPOS}


class ErrorCambio(Exception):
    def __init__(self, errores):
        super().__init__("; ".join(errores))
        self.errores = errores


# ------------------------------------------------------------------ utilidades
def _leer(cfg, ruta):
    o = cfg
    for p in ruta.split("."):
        if not isinstance(o, dict) or p not in o:
            return None
        o = o[p]
    return o


def _poner(cfg, ruta, valor):
    partes = ruta.split(".")
    o = cfg
    for p in partes[:-1]:
        o = o.setdefault(p, {})
    o[partes[-1]] = valor


def campo_estrategia(clave, investigacion=False):
    return _c(f"estrategias.{clave}.activa", "estrategias", clave, "bool",
              "Hipótesis de investigación: activarla solo la hace operar en papel." if investigacion else "")


def campos(estrategias=()):
    """Lista blanca completa: los campos fijos + un interruptor por estrategia conocida."""
    out = list(CAMPOS)
    for clave in estrategias:
        out.append(campo_estrategia(clave, clave in conf.ESTRATEGIAS_INVESTIGACION))
    return out


def esquema(cfg, estrategias=(), monedas_conocidas=()):
    """Para el panel: secciones y campos con su valor ACTUAL. Las direcciones van enmascaradas (con un índice para
    poder quitarlas sin reenviarlas)."""
    salida = []
    for c in campos(estrategias):
        v = _leer(cfg, c["ruta"])
        if c["tipo"] == "bool" and c["seccion"] == "estrategias" and v is None:
            v = conf.estrategia_activa(cfg, c["etiqueta"])
        if c["tipo"] == "direcciones":
            v = [{"i": i, "direccion": conf.enmascarar_direccion(d)} for i, d in enumerate(v or [])]
        salida.append(dict(c, valor=v))
    return {"secciones": [{"id": s, "titulo": t, "texto": x} for s, t, x in SECCIONES], "campos": salida,
            "monedas_conocidas": sorted(set(monedas_conocidas)),
            "aviso": "Al guardar, el bot vuelve a validar la configuración y se reinicia de forma ordenada (unos "
                     "segundos; las posiciones conservan sus stops). Cambiar algo que decide invalida las "
                     "aprobaciones y la evidencia acumulada con la configuración anterior (huella)."}


def _convertir(c, valor, actual):
    t = c["tipo"]
    if t == "bool":
        if not isinstance(valor, bool):
            raise ValueError("debe ser sí o no")
        return valor
    if t in ("num", "int"):
        if isinstance(valor, bool):
            raise ValueError("debe ser un número")
        try:
            x = float(str(valor).replace(",", "."))
        except ValueError:
            raise ValueError("debe ser un número") from None
        if x != x or x in (float("inf"), float("-inf")):
            raise ValueError("número no válido")
        if t == "int":
            if abs(x - round(x)) > 1e-9:
                raise ValueError("debe ser un número entero")
            x = int(round(x))
        if c["min"] is not None and x < c["min"]:
            raise ValueError(f"mínimo {c['min']}")
        if c["max"] is not None and x > c["max"]:
            raise ValueError(f"máximo {c['max']}")
        return x
    if t == "opcion":
        if valor not in c["opciones"]:
            raise ValueError("opción no válida: " + ", ".join(c["opciones"]))
        return valor
    if t == "multi":
        if not isinstance(valor, list) or not valor or any(v not in c["opciones"] for v in valor):
            raise ValueError("elige al menos una de: " + ", ".join(c["opciones"]))
        return [v for v in c["opciones"] if v in valor]
    if t == "monedas":
        if not isinstance(valor, list) or len(valor) > 100:
            raise ValueError("debe ser una lista (máximo 100)")
        out = []
        for v in valor:
            v = str(v).strip()
            if not RE_MONEDA.match(v):
                raise ValueError(f"nombre de mercado no válido: {v[:30]!r}")
            if v not in out:
                out.append(v)
        return out
    if t == "direcciones":
        # {"quitar": [índices], "anadir": ["0x..."]}: así nunca se reenvían las direcciones existentes
        if not isinstance(valor, dict):
            raise ValueError("formato no válido")
        actual = list(actual or [])
        quitar = {int(i) for i in valor.get("quitar") or [] if str(i).isdigit()}
        nuevo = [d for i, d in enumerate(actual) if i not in quitar]
        for d in valor.get("anadir") or []:
            d = str(d).strip()
            if not RE_DIRECCION.match(d):
                raise ValueError("dirección no válida (debe ser 0x seguido de 40 caracteres hexadecimales)")
            if d.lower() not in [x.lower() for x in nuevo]:
                nuevo.append(d)
        if len(nuevo) > 50:
            raise ValueError("máximo 50 ballenas manuales")
        return nuevo
    raise ValueError("tipo desconocido")


def _mostrar(c, v):
    if c["tipo"] == "direcciones":
        return [conf.enmascarar_direccion(x) for x in v or []]
    return v


def aplicar_cambios(cfg, cambios, estrategias=(), entorno=None):
    """Devuelve (config_nueva_validada, lista de cambios [{ruta, etiqueta, antes, despues, sube_riesgo}]).
    Lanza ErrorCambio si algún campo no está permitido o la configuración resultante no es válida."""
    if not isinstance(cambios, dict) or not cambios:
        raise ErrorCambio(["no hay cambios"])
    permitidos = {c["ruta"]: c for c in campos(estrategias)}
    nuevo = copy.deepcopy(cfg)
    errores, lista = [], []
    for ruta, valor in cambios.items():
        if not isinstance(ruta, str) or ruta.split(".")[0] in PROHIBIDOS or ruta not in permitidos:
            errores.append(f"{ruta}: no se puede cambiar desde el panel")
            continue
        c = permitidos[ruta]
        antes = _leer(cfg, ruta)
        if c["seccion"] == "estrategias" and antes is None:
            antes = conf.estrategia_activa(cfg, c["etiqueta"])
        try:
            despues = _convertir(c, valor, antes)
        except ValueError as e:
            errores.append(f"{c['etiqueta']}: {e}")
            continue
        if despues == antes:
            continue
        _poner(nuevo, ruta, despues)
        sube = None
        if c.get("riesgo") and isinstance(antes, (int, float)) and isinstance(despues, (int, float)):
            sube = despues > antes if c["riesgo"] == "sube" else despues < antes
        lista.append({"ruta": ruta, "etiqueta": c["etiqueta"], "seccion": c["seccion"], "antes": _mostrar(c, antes),
                      "despues": _mostrar(c, despues), "sube_riesgo": sube})
    if errores:
        raise ErrorCambio(errores)
    if not lista:
        raise ErrorCambio(["los valores son iguales a los actuales: no hay nada que guardar"])
    for k in PROHIBIDOS:                       # defensa extra: lo prohibido sigue exactamente igual
        if cfg.get(k) != nuevo.get(k):
            raise ErrorCambio([f"intento de cambiar «{k}» desde el panel"])
    try:
        validado = conf.validar(copy.deepcopy(nuevo), entorno=entorno)
    except conf.ErrorConfig as e:
        raise ErrorCambio([str(x) for x in e.args[0]] if e.args and isinstance(e.args[0], list) else [str(e)]) from None
    if validado.get("modo") != cfg.get("modo"):
        raise ErrorCambio(["el modo no se puede cambiar desde el panel"])
    return nuevo, lista


# ------------------------------------------------------------------ disco
def _escribir_atomico(ruta, datos):
    from compat import reemplazar
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(datos, fh, indent=2, ensure_ascii=False)
    reemplazar(tmp, ruta)


def _sello():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def guardar(carpeta, nuevo, lista, origen, motivo="cambio desde el panel"):
    """Copia de seguridad del config.json actual, escritura atómica del nuevo y registro del cambio."""
    ruta_cfg = os.path.join(carpeta, "config.json")
    copias = os.path.join(carpeta, DIR_COPIAS)
    os.makedirs(copias, exist_ok=True)
    nombre = f"config-{_sello()}.json"
    with open(ruta_cfg, encoding="utf-8") as fh:
        anterior = fh.read()
    with open(os.path.join(copias, nombre), "w", encoding="utf-8") as fh:
        fh.write(anterior)
    viejas = sorted(f for f in os.listdir(copias) if f.startswith("config-") and f.endswith(".json"))
    for f in viejas[:-MAX_COPIAS]:
        try:
            os.remove(os.path.join(copias, f))
        except OSError:
            pass
    _escribir_atomico(ruta_cfg, nuevo)
    os.makedirs(os.path.join(carpeta, "registro"), exist_ok=True)
    fila = {"ts": time.time(), "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"), "origen": origen,
            "motivo": motivo, "copia_anterior": nombre, "cambios": lista}
    with open(os.path.join(carpeta, "registro", "cambios_config.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fila, ensure_ascii=False, default=str) + "\n")
    return nombre


def historial(carpeta, n=50):
    ruta = os.path.join(carpeta, "registro", "cambios_config.jsonl")
    filas = []
    try:
        with open(ruta, encoding="utf-8") as fh:
            for linea in fh:
                try:
                    filas.append(json.loads(linea))
                except ValueError:
                    continue
    except OSError:
        pass
    copias = set()
    try:
        copias = set(os.listdir(os.path.join(carpeta, DIR_COPIAS)))
    except OSError:
        pass
    for f in filas:
        f["copia_disponible"] = f.get("copia_anterior") in copias
    return list(reversed(filas[-n:]))


def restaurar(carpeta, nombre, origen, estrategias=(), entorno=None):
    """Vuelve a una copia anterior: solo si existe, es válida y NO cambia lo prohibido (modo, real, panel...)."""
    if not re.match(r"^config-\d{8}T\d{6}Z\.json$", str(nombre)):
        raise ErrorCambio(["copia no válida"])
    ruta = os.path.join(carpeta, DIR_COPIAS, nombre)
    try:
        with open(ruta, encoding="utf-8") as fh:
            copia = json.load(fh)
    except (OSError, ValueError):
        raise ErrorCambio(["no encuentro esa copia"]) from None
    with open(os.path.join(carpeta, "config.json"), encoding="utf-8") as fh:
        actual = json.load(fh)
    for k in PROHIBIDOS:
        if actual.get(k) != copia.get(k):
            raise ErrorCambio([f"esa copia cambia «{k}»: eso solo se puede hacer a mano en config.json"])
    try:
        conf.validar(copy.deepcopy(copia), entorno=entorno)
    except conf.ErrorConfig as e:
        raise ErrorCambio(["la copia no es válida con esta versión: " + "; ".join(map(str, e.args[0]))]) from None
    lista = []
    for c in campos(estrategias):
        a, d = _leer(actual, c["ruta"]), _leer(copia, c["ruta"])
        if a != d:
            lista.append({"ruta": c["ruta"], "etiqueta": c["etiqueta"], "seccion": c["seccion"],
                          "antes": _mostrar(c, a), "despues": _mostrar(c, d), "sube_riesgo": None})
    return guardar(carpeta, copia, lista, origen, motivo=f"restaurada la copia {nombre}"), lista
