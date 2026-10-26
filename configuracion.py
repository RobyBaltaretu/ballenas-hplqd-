"""
Configuración del bot: una sola fuente de verdad por parámetro, migración desde versiones anteriores,
validación estricta al arrancar y comprobación del modo de operación frente al entorno.

MODOS
  simulado  Cuenta ficticia local (papel.json) con precios reales. Es el "paper trading". Por defecto.
  shadow    Solo observa: calcula señales y registra qué habría hecho. NO crea ningún cliente de órdenes,
            no necesita claves y no puede enviar, cancelar ni modificar nada.
  real      Dinero real. Solo arranca si TODO esto se cumple (si falta algo, se detiene antes de crear
            el cliente que firma órdenes):
              - config: "modo": "real" y "real": {"habilitado": true, "subcuenta": "0x..."}
              - entorno: BOT_REAL_CONFIRMADO=SI  y  HL_DIRECCION_CUENTA = esa misma subcuenta
              - la cuenta es una SUBCUENTA dedicada (se comprueba con Hyperliquid al arrancar)
              - sin posiciones ni órdenes que el registro del bot no pueda atribuirse
              - al menos una estrategia con promoción "real_habilitado" vigente (promocion.py)
"paper" se acepta como sinónimo de "simulado".

Si el entorno contradice la configuración (p. ej. HL_MODO=simulado con "modo": "real"), no arranca.
"""

import copy
import json
import math
import os
import shutil
from datetime import datetime, timezone

VERSION_CONFIG = 2
MODOS = ("simulado", "shadow", "real")
SINONIMOS_MODO = {"paper": "simulado"}
VARIABLE_CONFIRMACION_REAL = "BOT_REAL_CONFIRMADO"
VALOR_CONFIRMACION_REAL = "SI"


class ErrorConfig(Exception):
    """Configuración inválida: el bot no debe arrancar."""

    def __init__(self, errores):
        self.errores = list(errores)
        super().__init__("Configuración no válida:\n  - " + "\n  - ".join(self.errores))


# ============================================================ valores por defecto (instalación nueva)
DEFECTOS = {
    "version_config": VERSION_CONFIG,
    "modo": "simulado",
    "red": "mainnet",
    "saldo_simulado_usd": 58,
    "real": {"habilitado": False, "subcuenta": ""},
    # Límites del piloto real: SIEMPRE se aplican además de "riesgo" (gana el más prudente).
    "piloto_real": {"capital_max_usd": 50, "riesgo_por_operacion_pct": 0.5, "riesgo_abierto_max_pct": 1.5,
                    "max_posiciones_abiertas": 1, "perdida_max_usd": 10, "mercados": ["BTC", "ETH"]},
    "shadow": {"saldo_teorico_usd": 58},
    "riesgo": {
        "riesgo_por_operacion_pct": 1.0,        # pérdida máxima prevista por operación (stop + costes + estrés)
        "riesgo_abierto_max_pct": 4.0,          # suma del riesgo de todas las posiciones del bot
        "max_posiciones_abiertas": 4,
        "max_mismo_lado": 3,                    # posiciones del bot en la misma dirección
        "max_operaciones_dia": 10,
        "apalancamiento_max": 3,
        "margen_max_por_operacion_pct": 35,     # margen de una posición frente al valor de la cuenta
        "exposicion_max_mercado_pct": 100,      # notional en un mercado frente al valor de la cuenta
        "exposicion_bruta_max_pct": 250,
        "exposicion_neta_max_pct": 150,
        "sl_min_pct": 0.25,                     # stop más cercano admitido (más cerca, las comisiones pesan demasiado)
        "sl_max_pct": 12.0,                     # stop más lejano admitido
        "duracion_max_global_h": 336,           # 14 días: ninguna operación dura más (aunque su estrategia diga otra cosa)
        "perdida_diaria_max_pct": 6,            # BLOQUEO de entradas hasta el día siguiente (UTC)
        "perdida_semanal_max_pct": 12,          # BLOQUEO hasta la semana siguiente (UTC, ISO)
        "drawdown_max_pct": 25,                 # BLOQUEO mientras la cuenta esté a más de esto de su máximo
        "perdida_max_total_pct": 50,            # SUELO de emergencia: cierra las posiciones DEL BOT y se apaga
        "pausa_tras_perdidas_seguidas": 0,
        "minutos_pausa_tras_racha": 120,
        "deslizamiento_estimado_pct": 0.05,     # coste estimado por ejecución a mercado (además de la comisión)
        "margen_estres_pct": 0.05,              # colchón extra de coste en el cálculo del tamaño
        "spread_max_pct": 0.05,
        "correlacion_max": 0.8,
        "datos_max_antiguedad_seg": 120,        # precios más viejos que esto = datos obsoletos -> no se entra
        "errores_seguidos_para_degradar": 5,
        "distancia_liquidacion_min_multiplo_stop": 1.5,   # la liquidación debe quedar al menos 1,5× más lejos que el stop
        "coste_max_fraccion_riesgo": 0.20,      # descarta si entrar y salir cuesta más del 20 % de lo arriesgado (0 = sin filtro)
        "impacto_max_pct": 0.15,                # impacto estimado en el libro (recorriendo niveles) al entrar con ese tamaño
        "exposicion_beta_btc_max_pct": 200,     # exposición NETA ponderada por la beta frente a BTC (bot + externas)
        "beta_desconocida": 1.5,                # beta supuesta (prudente) si no hay datos para calcularla
        "mantenimiento_max_fraccion_equity": 0.5,   # margen de mantenimiento usado / valor de cuenta: por encima, no se entra
        "cuenta_max_antiguedad_seg": 90,        # lectura de la cuenta más vieja que esto = no se entra (equity no reconciliada)
        "politica_riesgo_desconocido": "bloquear",  # posiciones (del bot o externas) sin liquidación conocida en real
    },
    "comisiones": {"maker_pct": 0.015, "taker_pct": 0.045, "fuente": "config"},
    "ordenes": {"entrada_limite": True, "espera_limite_seg": 20, "reserva_a_mercado": True,
                "persecucion_max_pct": 0.5, "deslizamiento_max": 0.01, "max_consultas_estado": 3,
                "politica_sin_stop": "reponer_y_alertar"},
    "salida": {"parcial_activo": True, "tp1_r": 1.0, "parcial_pct": 50, "tp_final_r": 3.0, "trailing_r": 1.0,
               "breakeven_tras_r": 1.0, "mover_stop_min_r": 0.25, "precio_trailing": "mid"},
    "gestion_capital": {
        "reducir_en_caida": [[5, 0.75], [10, 0.5], [20, 0.3]],
        "modo_defensa": {"ultimas_operaciones": 8, "r_medio_minimo": -0.25, "factor": 0.5},
        "perdida_diaria_pct": 4, "factor_tras_perdida_diaria": 0.4,
        "perdida_semanal_pct": 8, "factor_tras_perdida_semanal": 0.5,
        "objetivo_diario_pct": 3, "factor_tras_objetivo": 0.5,
        "horas_sin_operar_para_relajar": 24, "factor_relajado": 0.5,
    },
    "filtros_entrada": {"funding_max_en_contra_pct": 0.005},
    # Estrategias: "activa" = genera señales en simulado/shadow. Para dinero real hace falta ADEMÁS una
    # promoción vigente (promocion.py); el laboratorio nunca la concede por sí solo.
    "estrategias": {},
    "aprendizaje": {"activo": True, "min_casos_parecidos": 8, "vecinos": 20, "distancia_max": 3.0,
                    "r_minimo_para_entrar": 0.05, "peso_sombras": 0.7, "peso_backtest": 0.5, "vida_media_dias": 21,
                    "factor_tamano_sin_datos": 1.0, "min_casos_ballena": 10, "r_minimo_ballena": -0.2,
                    "vida_media_backtest_dias": 365, "r_minimo_relajado": -0.1, "explorar_en_simulado": True,
                    "factor_exploracion": 0.5},
    "panel": {"host": "127.0.0.1", "puerto": 8080, "comandos_habilitados": True, "comandos_remotos": False,
              "permitir_http_red": False},
    # Cerebro: único que decide qué candidata se opera (cerebro_vivo.py). decide=false -> vuelve el filtro antiguo.
    "cerebro": {"decide": True, "umbral_r": 0.05, "max_antiguedad_externos_min": 20},
    # Binance y Bybit: lectura de datos públicos de mercado para informe e investigación (contexto_externo.py).
    "contexto_externo": {"activo": True, "cada_min": 5, "fuentes": ["binance", "bybit"], "desviacion_max_pct": 2.0},
    "archivo": {"activo": True, "velas": True, "libro": True, "libro_cada_min": 5, "libro_niveles": 10,
                "dias_guardar_libro": 365},
    "feed": {"activo": True, "dias_retencion": 14, "max_mb": 20, "min_fill_ballena_usd": 10000,
             "min_cambio_ballena_usd": 25000},
    "tiempo_real": {"websocket": True, "max_ballenas_ws": 10, "silencio_max_seg": 300},
    "universo": {"volumen_min_24h_usd": 20_000_000, "max_monedas": 20, "excluir": [], "incluir_siempre": ["BTC", "ETH"]},
    "motor": {"intervalos": ["1h", "4h"], "persecucion_max_r": 0.3, "dias_funding_vivo": 90},
    "ballenas": {"activo": True, "requerido": False,
                 "modo_senal": "investigacion",   # "investigacion" (solo registra) | "simulado" (opera en papel)
                 "usar_como_veto": False,
                 "ventana_min": 60, "aumento_min_usd": 50_000, "flujo_min_usd": 100_000, "contra_max_fraccion": 0.5,
                 "veto_usd": 150_000, "sl_atr": 1.5, "barras_max": 8, "datos_max_antiguedad_seg": 180,
                 "salida": {"tp1_r": 1.0, "tp_final_r": 2.5, "trailing_r": 1.0}},
    "mercado": {"grabar_operaciones": True, "operacion_grande_usd": 50_000, "operacion_enorme_usd": 250_000,
                "dias_guardar": 120,
                "liquidaciones": {"activo": True, "ventana_min": 60, "caida_oi_pct": 3.0, "movimiento_atr": 2.0,
                                  "sl_atr": 1.5, "barras_max": 6, "espera_horas": 4, "corroboracion_usd": 250_000,
                                  "salida": {"tp1_r": 1.0, "tp_final_r": 2.0, "trailing_r": 0.8}}},
    "laboratorio": {"dias_funding": 850, "dias_ballenas": 180, "max_casos_memoria": 4000, "ballenas_candidatas": 60,
                    "dex_acciones": [], "volumen_min_acciones": 1_000_000, "max_acciones": 20,
                    "pliegues": 5, "fraccion_holdout": 0.15, "embargo_barras": 24, "monedas_pool": 30,
                    "bootstrap_iteraciones": 1000, "bloque_bootstrap": 10, "semilla": 12345,
                    # Historia ampliada con datos PÚBLICOS de Binance/Bybit (solo lectura, sin claves): ver
                    # historia_externa.py. Hyperliquid manda donde tiene datos.
                    "procesos": 0,       # núcleos para simular (0 = automático)
                    "historia_externa": {"activa": True, "fuentes": ["binance", "bybit"], "anos_1h": 4,
                                         "anos_4h": 6, "desviacion_max_pct": 1.0, "min_velas_solape": 100,
                                         "volumen_min_pool_usd": 2_000_000}},
    "seleccion_automatica": True,
    "ballenas_manuales": [],
    "filtros_ballenas": {"numero_ballenas": 15, "valor_cuenta_min_usd": 500_000, "pnl_historico_min_usd": 500_000,
                         "roi_mensual_max": 1.0, "rotacion_mensual_max": 60, "candidatas_a_analizar": 60,
                         "ordenes_dia_max": 300, "fraccion_min_en_monedas": 0.3, "dias_max_sin_operar": 7},
    "refrescar_ballenas_horas": 12,
    "segundos_entre_ciclos": 15,
    "enfriamiento_min": 30,
}

# Estado por defecto de cada estrategia (se completa con las de senales.MODULOS al validar).
ESTRATEGIAS_INVESTIGACION = {"ballenas_1h", "liquidaciones_1h"}   # hipótesis: solo registran, no operan por defecto


# ============================================================ utilidades
def _fusionar(base, encima):
    """Copia de 'base' con los valores de 'encima' (recursivo en diccionarios)."""
    out = copy.deepcopy(base)
    for k, v in encima.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _fusionar(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def cargar(ruta):
    try:
        with open(ruta, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ErrorConfig([f"falta {os.path.basename(ruta)}"]) from None
    except ValueError as e:
        raise ErrorConfig([f"{os.path.basename(ruta)} no es JSON válido: {e}"]) from None


# ============================================================ migración v1 -> v2
MIGRACION_V1 = [
    # (clave antigua, destino nuevo, transformación)
    ("riesgo_por_operacion_pct", ("riesgo", "riesgo_por_operacion_pct"), None),
    ("margen_max_por_operacion_pct", ("riesgo", "margen_max_por_operacion_pct"), None),
    ("apalancamiento_max", ("riesgo", "apalancamiento_max"), None),
    ("max_posiciones_abiertas", ("riesgo", "max_posiciones_abiertas"), None),
    ("max_operaciones_dia", ("riesgo", "max_operaciones_dia"), None),
    ("perdida_max_total_pct", ("riesgo", "perdida_max_total_pct"), None),
    ("pausa_tras_perdidas_seguidas", ("riesgo", "pausa_tras_perdidas_seguidas"), None),
    ("minutos_pausa_tras_racha", ("riesgo", "minutos_pausa_tras_racha"), None),
    ("comision_maker_pct", ("comisiones", "maker_pct"), None),
    ("comision_taker_pct", ("comisiones", "taker_pct"), None),
    ("deslizamiento_max", ("ordenes", "deslizamiento_max"), None),
]


def migrar(c):
    """Convierte una configuración antigua (sin version_config) al formato actual, conservando los valores
    que el usuario tenía. Devuelve (config_nueva, lista de cambios legibles)."""
    cambios = []
    c = copy.deepcopy(c)
    if c.get("version_config", 1) >= VERSION_CONFIG:
        return c, cambios
    nueva = {}
    for vieja, (bloque, clave), _ in MIGRACION_V1:
        if vieja in c:
            nueva.setdefault(bloque, {})[clave] = c.pop(vieja)
            cambios.append(f"{vieja} -> {bloque}.{clave}")
    if "duracion_max_min" in c:
        # Antes era la duración de TODAS las operaciones (copiar ballenas, minutos). Ahora cada estrategia
        # tiene la suya y esto es solo un tope global en horas; se respeta como mínimo el valor por defecto (336 h).
        h = max(c.pop("duracion_max_min") / 60, DEFECTOS["riesgo"]["duracion_max_global_h"])
        nueva.setdefault("riesgo", {})["duracion_max_global_h"] = h
        cambios.append(f"duracion_max_min -> riesgo.duracion_max_global_h = {h:g}")
    fe = c.get("filtros_entrada", {})
    for k in ("spread_max_pct", "correlacion_max"):
        if k in fe:
            nueva.setdefault("riesgo", {})[k] = fe.pop(k)
            cambios.append(f"filtros_entrada.{k} -> riesgo.{k}")
    if "persecucion_max_pct" in fe:
        nueva.setdefault("ordenes", {})["persecucion_max_pct"] = fe.pop("persecucion_max_pct")
        cambios.append("filtros_entrada.persecucion_max_pct -> ordenes.persecucion_max_pct")
    # Límites de stop duplicados: filtros_entrada.sl_* (0,5-3 %) y motor.sl_* (0,25-12 %). El motor era el
    # que de verdad se aplicaba a las señales; se conserva ese y se elimina el otro.
    m = c.get("motor", {})
    for k in ("sl_min_pct", "sl_max_pct"):
        if k in m:
            nueva.setdefault("riesgo", {})[k] = m.pop(k)
            cambios.append(f"motor.{k} -> riesgo.{k} (único límite de stop)")
        if k in fe:
            fe.pop(k)
            cambios.append(f"filtros_entrada.{k} eliminado (duplicado y no aplicado a las señales)")
    if m.pop("modulos_sin_validar", None) is not None:
        cambios.append("motor.modulos_sin_validar eliminado: ahora decide la promoción por estrategia (promocion.py)")
    gc = c.get("gestion_capital", {})
    if gc.pop("riesgo_max_orden_minima_pct", None) is not None:
        cambios.append("gestion_capital.riesgo_max_orden_minima_pct eliminado: ya no se fuerza la orden mínima "
                       "(si supera el riesgo permitido, la operación se descarta)")
    b = c.get("ballenas", {})
    if b.get("modo_senal") in ("solo_simulado", "siempre"):
        b["modo_senal"] = "investigacion"
        cambios.append("ballenas.modo_senal -> 'investigacion' (hipótesis sin validar: solo registra)")
    if "veto_usd" in b and "usar_como_veto" not in b:
        b["usar_como_veto"] = False
        cambios.append("ballenas.usar_como_veto = false (el veto no tiene evidencia suficiente; actívalo si quieres)")
    c.pop("backtest", None)
    c = _fusionar(c, nueva)
    c["version_config"] = VERSION_CONFIG
    return c, cambios


def migrar_archivo(ruta):
    """Migra config.json en disco haciendo antes una copia de seguridad. Devuelve la lista de cambios."""
    original = cargar(ruta)
    nueva, cambios = migrar(original)
    if not cambios and original.get("version_config") == VERSION_CONFIG:
        return []
    marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(ruta, f"{ruta}.v{original.get('version_config', 1)}-{marca}.bak")
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(nueva, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, ruta)
    return cambios


# ============================================================ validación
def _num(c, ruta, minimo=None, maximo=None, entero=False, errores=None, minimo_excl=False):
    v = c
    for p in ruta:
        if not isinstance(v, dict) or p not in v:
            errores.append(f"falta {'.'.join(ruta)}")
            return None
        v = v[p]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        errores.append(f"{'.'.join(ruta)} debe ser un número finito (vale {v!r})")
        return None
    if entero and int(v) != v:
        errores.append(f"{'.'.join(ruta)} debe ser entero")
    if minimo is not None and (v <= minimo if minimo_excl else v < minimo):
        errores.append(f"{'.'.join(ruta)} = {v} fuera de rango (mínimo {'>' if minimo_excl else '≥'} {minimo})")
    if maximo is not None and v > maximo:
        errores.append(f"{'.'.join(ruta)} = {v} fuera de rango (máximo {maximo})")
    return v


def _bool(c, ruta, errores):
    v = c
    for p in ruta:
        if not isinstance(v, dict) or p not in v:
            errores.append(f"falta {'.'.join(ruta)}")
            return None
        v = v[p]
    if not isinstance(v, bool):
        errores.append(f"{'.'.join(ruta)} debe ser true o false")
    return v


def normalizar_modo(valor):
    if not isinstance(valor, str):
        return None
    v = valor.strip().lower()
    return SINONIMOS_MODO.get(v, v)


def validar(c, entorno=None, modulos=None):
    """Valida la configuración ya migrada. Lanza ErrorConfig con TODOS los problemas.
    entorno: dict de variables (por defecto os.environ). modulos: claves de estrategia conocidas.
    Devuelve la configuración con los valores por defecto aplicados a lo que faltaba."""
    entorno = os.environ if entorno is None else entorno
    errores = []
    if not isinstance(c, dict):
        raise ErrorConfig(["la configuración debe ser un objeto JSON"])
    if c.get("version_config") != VERSION_CONFIG:
        errores.append(f"version_config debe ser {VERSION_CONFIG}: ejecuta 'python bot.py --migrar-config' "
                       f"(hace copia de seguridad)")

    # ---- modo: obligatorio, sin valor por defecto implícito
    if "modo" not in c:
        errores.append('falta "modo" (simulado, shadow o real). Una instalación nueva usa "simulado".')
        modo = None
    else:
        modo = normalizar_modo(c["modo"])
        if modo not in MODOS:
            errores.append(f'"modo" = {c["modo"]!r} no es válido: usa "simulado" (o "paper"), "shadow" o "real"')
            modo = None
    if errores and modo is None:
        raise ErrorConfig(errores)

    c = _fusionar(DEFECTOS, c)    # lo que falte toma el valor prudente por defecto
    c["modo"] = modo

    # ---- coherencia con el entorno
    env_modo = normalizar_modo(entorno.get("HL_MODO", "")) if entorno.get("HL_MODO") else None
    if env_modo and env_modo != modo:
        errores.append(f"contradicción: HL_MODO={entorno.get('HL_MODO')!r} pero config.json dice modo={modo!r}")
    real = c.get("real") or {}
    if not isinstance(real, dict):
        errores.append('"real" debe ser un objeto')
        real = {}
    habilitado = real.get("habilitado") is True
    if modo == "real":
        if not habilitado:
            errores.append('modo "real" requiere "real": {"habilitado": true, ...} (habilitación explícita)')
        if entorno.get(VARIABLE_CONFIRMACION_REAL) != VALOR_CONFIRMACION_REAL:
            errores.append(f"modo real requiere la variable de entorno {VARIABLE_CONFIRMACION_REAL}="
                           f"{VALOR_CONFIRMACION_REAL} (confirmación explícita, fuera de config.json)")
        sub = str(real.get("subcuenta") or "").strip().lower()
        if not (sub.startswith("0x") and len(sub) == 42):
            errores.append('modo real requiere "real.subcuenta" con la dirección 0x... de la SUBCUENTA dedicada al bot')
        cuenta_env = str(entorno.get("HL_DIRECCION_CUENTA", "")).strip().lower()
        if not cuenta_env:
            errores.append("modo real requiere HL_DIRECCION_CUENTA en .env (la subcuenta dedicada)")
        elif sub and cuenta_env != sub:
            errores.append("contradicción: HL_DIRECCION_CUENTA (.env) no coincide con real.subcuenta (config.json)")
        if not str(entorno.get("HL_API_WALLET_KEY", "")).strip():
            errores.append("modo real requiere HL_API_WALLET_KEY en .env (clave de una API wallet, nunca la principal)")
    else:
        if habilitado:
            errores.append(f'contradicción: "real.habilitado" es true pero modo={modo!r}. Ponlo a false si no '
                           f'vas a operar en real.')
        if entorno.get(VARIABLE_CONFIRMACION_REAL) == VALOR_CONFIRMACION_REAL:
            errores.append(f"contradicción: {VARIABLE_CONFIRMACION_REAL}=SI en el entorno pero modo={modo!r}")
    if c.get("red") not in ("mainnet", "testnet"):
        errores.append('"red" debe ser "mainnet" o "testnet"')

    # ---- riesgo (única fuente de verdad)
    r = ("riesgo",)
    _num(c, r + ("riesgo_por_operacion_pct",), 0, 5, errores=errores, minimo_excl=True)
    _num(c, r + ("riesgo_abierto_max_pct",), 0, 20, errores=errores, minimo_excl=True)
    _num(c, r + ("max_posiciones_abiertas",), 1, 20, entero=True, errores=errores)
    _num(c, r + ("max_mismo_lado",), 1, 20, entero=True, errores=errores)
    _num(c, r + ("max_operaciones_dia",), 1, 200, entero=True, errores=errores)
    _num(c, r + ("apalancamiento_max",), 1, 10, errores=errores)
    _num(c, r + ("margen_max_por_operacion_pct",), 0, 100, errores=errores, minimo_excl=True)
    _num(c, r + ("exposicion_max_mercado_pct",), 0, 1000, errores=errores, minimo_excl=True)
    _num(c, r + ("exposicion_bruta_max_pct",), 0, 2000, errores=errores, minimo_excl=True)
    _num(c, r + ("exposicion_neta_max_pct",), 0, 2000, errores=errores, minimo_excl=True)
    slmin = _num(c, r + ("sl_min_pct",), 0, 20, errores=errores, minimo_excl=True)
    slmax = _num(c, r + ("sl_max_pct",), 0, 50, errores=errores, minimo_excl=True)
    if slmin is not None and slmax is not None and slmin >= slmax:
        errores.append("riesgo.sl_min_pct debe ser menor que riesgo.sl_max_pct")
    _num(c, r + ("duracion_max_global_h",), 0.1, 24 * 30, errores=errores)
    _num(c, r + ("perdida_diaria_max_pct",), 0, 50, errores=errores, minimo_excl=True)
    _num(c, r + ("perdida_semanal_max_pct",), 0, 80, errores=errores, minimo_excl=True)
    _num(c, r + ("drawdown_max_pct",), 0, 90, errores=errores, minimo_excl=True)
    _num(c, r + ("perdida_max_total_pct",), 0, 90, errores=errores, minimo_excl=True)
    _num(c, r + ("pausa_tras_perdidas_seguidas",), 0, 100, entero=True, errores=errores)
    _num(c, r + ("deslizamiento_estimado_pct",), 0, 5, errores=errores)
    _num(c, r + ("margen_estres_pct",), 0, 5, errores=errores)
    _num(c, r + ("spread_max_pct",), 0, 5, errores=errores, minimo_excl=True)
    _num(c, r + ("correlacion_max",), 0, 1, errores=errores)
    _num(c, r + ("datos_max_antiguedad_seg",), 5, 3600, errores=errores)
    _num(c, r + ("errores_seguidos_para_degradar",), 1, 100, entero=True, errores=errores)
    _num(c, r + ("distancia_liquidacion_min_multiplo_stop",), 1, 10, errores=errores)
    _num(c, r + ("impacto_max_pct",), 0, 5, errores=errores, minimo_excl=True)
    _num(c, r + ("exposicion_beta_btc_max_pct",), 0, 2000, errores=errores, minimo_excl=True)
    _num(c, r + ("beta_desconocida",), 0, 10, errores=errores, minimo_excl=True)
    _num(c, r + ("mantenimiento_max_fraccion_equity",), 0, 1, errores=errores, minimo_excl=True)
    _num(c, r + ("cuenta_max_antiguedad_seg",), 15, 3600, errores=errores)
    if c["riesgo"].get("politica_riesgo_desconocido") not in ("bloquear", "avisar"):
        errores.append('riesgo.politica_riesgo_desconocido debe ser "bloquear" o "avisar"')

    # Hyperliquid admite como mucho 10 usuarios distintos por IP en suscripciones de usuario: el bot usa
    # min(valor, 10) y lo avisa en el panel (no se rechaza la configuración por eso).
    _num(c, ("tiempo_real", "max_ballenas_ws"), 0, 1000, entero=True, errores=errores)
    # ---- feed de señales en vivo (panel)
    _bool(c, ("feed", "activo"), errores)
    _num(c, ("feed", "dias_retencion"), 1, 365, errores=errores)
    _num(c, ("feed", "max_mb"), 1, 1000, errores=errores)
    _num(c, ("feed", "min_fill_ballena_usd"), 0, 1e9, errores=errores)
    _num(c, ("feed", "min_cambio_ballena_usd"), 0, 1e9, errores=errores)

    # ---- comisiones: explícitas (o leídas de la cuenta en real)
    _num(c, ("comisiones", "maker_pct"), -0.1, 1, errores=errores)
    _num(c, ("comisiones", "taker_pct"), 0, 1, errores=errores)
    if c["comisiones"].get("fuente") not in ("config", "cuenta"):
        errores.append('comisiones.fuente debe ser "config" o "cuenta"')

    # ---- órdenes y salida
    _num(c, ("ordenes", "espera_limite_seg"), 1, 60, errores=errores)
    _num(c, ("ordenes", "persecucion_max_pct"), 0, 10, errores=errores)
    _num(c, ("ordenes", "deslizamiento_max"), 0.0001, 0.1, errores=errores)
    _num(c, ("ordenes", "max_consultas_estado"), 1, 20, entero=True, errores=errores)
    if c["ordenes"].get("politica_sin_stop") not in ("reponer_y_alertar", "cerrar"):
        errores.append('ordenes.politica_sin_stop debe ser "reponer_y_alertar" o "cerrar"')
    s = c["salida"]
    _bool(c, ("salida", "parcial_activo"), errores)
    _num(c, ("salida", "parcial_pct"), 0, 100, errores=errores, minimo_excl=True)
    tp1 = _num(c, ("salida", "tp1_r"), 0, 100, errores=errores, minimo_excl=True)
    tpf = _num(c, ("salida", "tp_final_r"), 0, 100, errores=errores, minimo_excl=True)
    if s.get("parcial_activo") and tp1 is not None and tpf is not None and tp1 >= tpf:
        errores.append("salida: tp1_r debe ser menor que tp_final_r")
    _num(c, ("salida", "trailing_r"), 0, 100, errores=errores, minimo_excl=True)
    _num(c, ("salida", "breakeven_tras_r"), 0, 100, errores=errores, minimo_excl=True)
    if s.get("precio_trailing") not in ("mid", "mark"):
        errores.append('salida.precio_trailing debe ser "mid" o "mark"')

    # ---- universo y mercados permitidos
    u = c["universo"]
    for k in ("excluir", "incluir_siempre"):
        if not isinstance(u.get(k), list) or not all(isinstance(x, str) and x for x in u.get(k, [])):
            errores.append(f"universo.{k} debe ser una lista de nombres de mercado")
    _num(c, ("universo", "max_monedas"), 1, 100, entero=True, errores=errores)
    _num(c, ("universo", "volumen_min_24h_usd"), 0, None, errores=errores)
    pr = c["piloto_real"]
    if not isinstance(pr.get("mercados"), list) or not all(isinstance(x, str) for x in pr.get("mercados", [])):
        errores.append("piloto_real.mercados debe ser una lista de mercados")
    for k, mx in (("capital_max_usd", 1e7), ("riesgo_por_operacion_pct", 5), ("riesgo_abierto_max_pct", 20),
                  ("perdida_max_usd", 1e7)):
        _num(c, ("piloto_real", k), 0, mx, errores=errores, minimo_excl=True)
    _num(c, ("piloto_real", "max_posiciones_abiertas"), 1, 20, entero=True, errores=errores)

    # ---- estrategias
    if not isinstance(c.get("estrategias"), dict):
        errores.append('"estrategias" debe ser un objeto {clave: {"activa": true/false}}')
    else:
        for k, v in c["estrategias"].items():
            if not isinstance(v, dict) or not isinstance(v.get("activa"), bool):
                errores.append(f'estrategias.{k} debe ser {{"activa": true|false}}')
            if modulos is not None and k not in modulos:
                errores.append(f"estrategias.{k}: estrategia desconocida")
    if c["ballenas"].get("modo_senal") not in ("investigacion", "simulado"):
        errores.append('ballenas.modo_senal debe ser "investigacion" o "simulado" (nunca opera en real sin promoción)')

    _num(c, ("riesgo", "coste_max_fraccion_riesgo"), 0, 1, errores=errores)
    for k in ("activo", "velas", "libro"):
        _bool(c, ("archivo", k), errores)
    _num(c, ("archivo", "libro_cada_min"), 1, 1440, entero=True, errores=errores)
    _num(c, ("archivo", "libro_niveles"), 1, 20, entero=True, errores=errores)
    _num(c, ("archivo", "dias_guardar_libro"), 1, 5000, entero=True, errores=errores)

    _num(c, ("segundos_entre_ciclos",), 5, 600, errores=errores)
    _num(c, ("saldo_simulado_usd",), 1, 1e9, errores=errores)
    if c["panel"].get("host") not in ("127.0.0.1", "localhost", "::1") and not entorno.get("PANEL_PASSWORD"):
        errores.append("panel.host expone el panel fuera de este equipo: hace falta PANEL_PASSWORD en .env "
                       "(y aun así úsalo solo detrás de VPN o HTTPS)")
    if errores:
        raise ErrorConfig(errores)
    return c


# ============================================================ valores efectivos (lo que de verdad se aplica)
def efectiva(c, estrategias=None):
    """Resumen de los valores que se aplican de verdad, para el panel y el arranque. 'estrategias' es la lista
    de estrategias del motor (con su duración propia) para mostrar la duración efectiva de cada una."""
    r = dict(c["riesgo"])
    fuentes = {k: "riesgo" for k in r}
    if c["modo"] == "real":
        p = c["piloto_real"]
        for k in ("riesgo_por_operacion_pct", "riesgo_abierto_max_pct", "max_posiciones_abiertas"):
            if p[k] < r[k]:
                r[k] = p[k]
                fuentes[k] = "piloto_real"
    out = {"modo": c["modo"], "riesgo": r, "fuente": fuentes, "comisiones": dict(c["comisiones"]),
           "duraciones": {}}
    tope_min = c["riesgo"]["duracion_max_global_h"] * 60
    for e in estrategias or []:
        propia = e.get("duracion_min") or 0
        out["duraciones"][e["clave"]] = {"estrategia_min": propia, "efectiva_min": min(propia, tope_min) if propia else tope_min,
                                         "recortada": bool(propia and propia > tope_min)}
    return out


def estrategia_activa(c, clave):
    """¿Esta estrategia genera señales (en simulado/shadow)? Las de investigación solo registran."""
    v = (c.get("estrategias") or {}).get(clave)
    if v is not None:
        return bool(v.get("activa"))
    return clave not in ESTRATEGIAS_INVESTIGACION


SENSIBLES = ("key", "clave", "secret", "secreto", "token", "password", "contrasena", "contraseña", "private", "privada",
             "mnemonic", "seed", "semilla_wallet", "chat_id")


NO_SENSIBLES = {"clave", "claves"}       # "clave" a secas es el nombre de una estrategia (ruptura_1h...), no un secreto


def es_sensible(nombre):
    n = str(nombre).lower()
    return n not in NO_SENSIBLES and any(s in n for s in SENSIBLES)


def sin_secretos(obj):
    """Copia de obj con cualquier campo de nombre sensible sustituido por '***'."""
    if isinstance(obj, dict):
        return {k: ("***" if es_sensible(k) else sin_secretos(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sin_secretos(x) for x in obj]
    return obj


def enmascarar_direccion(d):
    d = str(d or "")
    return d[:6] + "…" + d[-4:] if d.startswith("0x") and len(d) > 12 else d


def hash_config(c):
    """Huella corta de la configuración (sin secretos) para registrar ejecuciones reproducibles."""
    import hashlib
    return hashlib.sha256(json.dumps(sin_secretos(c), sort_keys=True, default=str).encode()).hexdigest()[:16]
