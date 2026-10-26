"""
Memoria y aprendizaje del bot.

Cada señal que el bot estudia se guarda con su estrategia (módulo + velas) y sus "rasgos"
(tendencia, impulso, RSI, eficiencia, volatilidad, funding, flujo de las ballenas, BTC, hora...).
Cuando se conoce cómo acabó, se guarda el resultado en "R":
    +2 R = ganó el doble de lo que arriesgaba,  -1 R = saltó el stop-loss.

Tipos de registro:
  - real:     operaciones que el bot abrió de verdad.
  - sombra:   señales que el bot DESCARTÓ, seguidas "en papel" con la misma gestión de
              salida (estrategia.SimSalida) para saber qué habría pasado.
  - backtest: casos sacados de la historia por laboratorio.py (preentrenamiento).

Antes de abrir, el bot busca las operaciones pasadas más parecidas (vecinos más
cercanos, con más peso para las reales, las recientes y las de la misma moneda) y
calcula el resultado medio que dieron. Si es malo, no entra; si es flojo, entra con
menos tamaño. También lleva la reputación de cada ballena y veta a las que, copiadas,
hacen perder dinero.
"""

import json
import math
import os
import time
import uuid

from compat import reemplazar
from estrategia import SimSalida

# Escala de cada rasgo para comparar operaciones (cuánto tiene que diferir para "contar" 1).
ESCALAS = {
    "lado": 0.5,
    "fuerza": 1.0,
    "tendencia_atr": 1.5,
    "pendiente_atr": 0.75,
    "impulso_atr": 1.5,
    "rsi": 12.0,
    "eficiencia": 0.15,
    "atr_pct": 0.5,
    "vol_relativa": 0.35,
    "funding_contra": 0.005,
    "btc_mov": 1.0,
    "flujo_ballenas": 3.0,      # 300 000 $ de flujo de las ballenas a favor/en contra
    "oi_cambio_1h": 2.0,        # % de cambio del interés abierto en la última hora
    "flujo_grandes": 5.0,       # 500 000 $ netos de operaciones grandes de todo el mercado
    "hora_sin": 1.0,
    "hora_cos": 1.0,
}
PENALIZACION_OTRA_MONEDA = 0.75
PENALIZACION_OTRO_MODULO = 1.5   # una ruptura se parece poco a una reversión aunque los números coincidan
PESO_TIPO = {"real": 1.0, "sombra": None, "backtest": None}  # None -> se toma de la config
MAX_REGISTROS = 5000


def _cargar(ruta):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"registros": []}


def ruta_lab(ruta):
    """Los casos del laboratorio van en un archivo aparte (aprendizaje_lab.json): así el laboratorio puede
    ejecutarse con el bot en marcha sin que uno pise lo que escribe el otro."""
    base, ext = os.path.splitext(ruta)
    return base + "_lab" + ext


class Memoria:
    def __init__(self, ruta, cfg_aprendizaje, cfg_salida, costes):
        """costes = (fee_entrada, fee_taker, fee_maker) en fracción."""
        self.ruta = ruta
        self.cfg = cfg_aprendizaje
        self.cfg_salida = cfg_salida
        self.costes = costes
        self.datos = _cargar(ruta) if ruta else {"registros": []}
        # Versiones antiguas guardaban los casos de backtest aquí mismo: ya no sirven (otra estrategia).
        self.datos["registros"] = [r for r in self.datos["registros"] if r["tipo"] != "backtest"]
        self.lab, self._lab_mtime = [], None
        self.recargar_lab()

    def recargar_lab(self):
        """Lee (o relee, si el laboratorio lo ha actualizado) los casos históricos del laboratorio."""
        if not self.ruta:
            return False
        try:
            mtime = os.path.getmtime(ruta_lab(self.ruta))
        except OSError:
            return False
        if mtime == self._lab_mtime:
            return False
        try:
            self.lab = [r for r in _cargar(ruta_lab(self.ruta))["registros"] if r.get("resultado_r") is not None]
            self._lab_mtime = mtime
            return True
        except (OSError, ValueError, KeyError):
            return False

    # ------------------------------------------------------------ persistencia
    def guardar(self):
        if not self.ruta:
            return
        regs = self.datos["registros"]
        if len(regs) > MAX_REGISTROS:
            abiertos = [r for r in regs if r["resultado_r"] is None]
            cerrados = [r for r in regs if r["resultado_r"] is not None]
            cerrados.sort(key=lambda r: r["cierre_ts"])
            self.datos["registros"] = cerrados[-(MAX_REGISTROS - len(abiertos)):] + abiertos
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.datos, f)
        reemplazar(tmp, self.ruta)

    def cerrados(self, ahora=None, con_lab=True):
        regs = self.datos["registros"] + (self.lab if con_lab else [])
        return [r for r in regs if r["resultado_r"] is not None and (ahora is None or r["cierre_ts"] <= ahora)]

    # ------------------------------------------------------------- registros
    def _nuevo(self, tipo, moneda, lado, rasgos, ballenas, plan, motivos, ts):
        r = {
            "id": uuid.uuid4().hex[:10], "tipo": tipo, "moneda": moneda, "lado": lado,
            "modulo": plan.get("estrategia"), "version": plan.get("version"),
            # origen del dato (real / simulado / shadow / backtest) y fuente: así nunca se mezclan sin etiqueta
            "origen": plan.get("origen"), "fuente": plan.get("fuente", tipo), "trade_id": plan.get("trade_id"),
            "ts": ts, "rasgos": rasgos, "ballenas": ballenas, "motivos_descarte": motivos,
            "entrada": plan["precio"], "sl_pct": plan["sl_pct"],
            "resultado_r": None, "cierre": None, "cierre_ts": None, "sim": None,
        }
        if plan.get("aprendizaje"):
            r["juicio_aprendizaje"] = plan["aprendizaje"]
        if plan.get("explorando"):
            r["explorando"] = True   # abierta en simulado aunque el aprendizaje no la veía clara
        self.datos["registros"].append(r)
        return r

    def registrar_real(self, moneda, lado, rasgos, ballenas, plan, ts=None):
        return self._nuevo("real", moneda, lado, rasgos, ballenas, plan, [], ts or time.time())["id"]

    def cerrar_real(self, id_reg, resultado_r, motivo, ts=None, pnl_usd=None):
        for r in self.datos["registros"]:
            if r["id"] == id_reg:
                r["resultado_r"], r["cierre"], r["cierre_ts"] = round(resultado_r, 3), motivo, ts or time.time()
                if pnl_usd is not None:
                    r["pnl_usd"] = round(pnl_usd, 4)
                return

    def registrar_cerrado(self, tipo, moneda, lado, rasgos, ballenas, plan, motivos, ts, resultado_r, motivo, cierre_ts):
        """Para el backtest: registro ya terminado."""
        r = self._nuevo(tipo, moneda, lado, rasgos, ballenas, plan, motivos, ts)
        r.update(resultado_r=round(resultado_r, 3), cierre=motivo, cierre_ts=cierre_ts)
        return r

    def hay_sombra_abierta(self, moneda, lado):
        return any(r["tipo"] in ("sombra", "shadow") and r["resultado_r"] is None and r["moneda"] == moneda
                   and r["lado"] == lado for r in self.datos["registros"])

    def abrir_sombra(self, moneda, lado, rasgos, ballenas, plan, motivos, ts=None, tipo="sombra"):
        """Sigue en papel una señal descartada ('sombra') o una intención del modo shadow ('shadow').
        El resultado es MODELADO (SimSalida con precios reales), nunca una ejecución."""
        if tipo == "aspirante":
            if any(r["tipo"] == "aspirante" and r["resultado_r"] is None and r["moneda"] == moneda
                   and r.get("modulo") == plan.get("estrategia") for r in self.datos["registros"]):
                return None
        elif self.hay_sombra_abierta(moneda, lado):
            return None
        ts = ts or time.time()
        r = self._nuevo(tipo, moneda, lado, rasgos, ballenas, plan, motivos, ts)
        sim = SimSalida(lado, plan["precio"], plan["sl_pct"], plan.get("salida") or self.cfg_salida,
                        ts + plan["duracion_min"] * 60, *self.costes)
        r["sim"] = sim.a_dict()
        return r

    def actualizar_sombras(self, mids, ahora=None):
        """Avanza las operaciones en papel con el precio actual. Devuelve las que se cerraron en este paso."""
        ahora = ahora or time.time()
        cerradas = []
        for r in self.datos["registros"]:
            if r["tipo"] not in ("sombra", "shadow", "aspirante") or r["resultado_r"] is not None or r["moneda"] not in mids:
                continue
            px = float(mids[r["moneda"]])
            sim = SimSalida.desde_dict(r["sim"])
            if sim.paso(px, px, px, ahora):
                r["resultado_r"], r["cierre"], r["cierre_ts"] = sim.resultado_r(), sim.motivo, ahora
                r["sim"] = None
                cerradas.append(r)
            else:
                r["sim"] = sim.a_dict()
        return cerradas

    # -------------------------------------------------------------- similitud
    @staticmethod
    def _distancia(a, b, moneda_a, moneda_b, modulo_a=None, modulo_b=None):
        d2 = sum(((a.get(k, 0) - b.get(k, 0)) / esc) ** 2 for k, esc in ESCALAS.items())
        return (math.sqrt(d2) + (PENALIZACION_OTRA_MONEDA if moneda_a != moneda_b else 0)
                + (PENALIZACION_OTRO_MODULO if modulo_a != modulo_b else 0))

    def _peso_tipo(self, tipo):
        if tipo == "aspirante":
            return 0.0       # ideas en observación: no influyen en las decisiones del bot
        if tipo == "real":
            return 1.0
        if tipo == "shadow":
            return self.cfg["peso_sombras"]
        if tipo == "backtest":
            return self.cfg["peso_backtest"]
        return self.cfg["peso_sombras"]

    def valorar(self, moneda, rasgos, ahora=None, modulo=None, relajado=False, medir=False):
        """
        Devuelve {"abrir": bool, "factor": 0.5..1, "n": casos parecidos, "r_esperado": float|None, "texto": str}
        Solo usa resultados conocidos ANTES de 'ahora' (importante en el backtest).
        relajado: tras muchas horas sin operar se acepta un resultado esperado algo menor (nunca quedarse parado),
        pero nunca lo claramente malo.
        """
        c = self.cfg
        if not c.get("activo", True) and not medir:      # medir=True: juzga aunque el filtro esté apagado
            return {"abrir": True, "factor": 1.0, "n": 0, "r_esperado": None, "texto": "aprendizaje desactivado"}
        ahora = ahora or time.time()
        self.recargar_lab()
        vecinos = []
        for r in self.cerrados(ahora):
            if r["tipo"] == "aspirante":
                continue
            d = self._distancia(rasgos, r["rasgos"], moneda, r["moneda"], modulo, r.get("modulo"))
            if d > c["distancia_max"]:
                continue
            peso = (1 / (1 + d)) * self._peso_tipo(r["tipo"])
            # La historia del laboratorio envejece despacio (son años de mercado); lo vivido, más rápido.
            vida = c.get("vida_media_backtest_dias", 365) if r["tipo"] == "backtest" else c["vida_media_dias"]
            peso *= 0.5 ** (max(0.0, ahora - r["cierre_ts"]) / 86400 / vida)
            vecinos.append((d, peso, r["resultado_r"]))
        vecinos.sort(key=lambda x: x[0])
        vecinos = vecinos[: c["vecinos"]]
        n = len(vecinos)
        if n < c["min_casos_parecidos"]:
            return {"abrir": True, "factor": c["factor_tamano_sin_datos"], "n": n, "r_esperado": None,
                    "texto": f"solo {n} casos parecidos, aún aprendiendo"}
        peso_total = sum(p for _, p, _ in vecinos)
        r_esp = sum(p * res for _, p, res in vecinos) / peso_total
        aciertos = sum(1 for _, _, res in vecinos if res > 0) / n * 100
        texto = f"{n} casos parecidos: {r_esp:+.2f} R de media, {aciertos:.0f}% ganadoras"
        minimo = c.get("r_minimo_relajado", -0.1) if relajado else c["r_minimo_para_entrar"]
        if r_esp < minimo:
            return {"abrir": False, "factor": 0, "n": n, "r_esperado": r_esp, "texto": texto}
        factor = max(0.5, min(1.0, 0.5 + r_esp))
        return {"abrir": True, "factor": factor, "n": n, "r_esperado": r_esp, "texto": texto}

    # ------------------------------------------------------- reputación ballenas
    def reputacion(self, ballena, ahora=None):
        res = [r["resultado_r"] for r in self.cerrados(ahora) if ballena in (r.get("ballenas") or [])]
        return len(res), (sum(res) / len(res) if res else 0.0)

    def vetada(self, ballena, ahora=None):
        n, media = self.reputacion(ballena, ahora)
        return n >= self.cfg["min_casos_ballena"] and media < self.cfg["r_minimo_ballena"]

    def resumen(self, tipo="real"):
        res = [r["resultado_r"] for r in self.cerrados() if r["tipo"] == tipo]
        if not res:
            return 0, 0.0, 0.0
        return len(res), sum(1 for x in res if x > 0) / len(res) * 100, sum(res) / len(res)


def evaluar_filtro(registros, min_por_grupo=30, iteraciones=2000, semilla=12345):
    """¿El filtro de aprendizaje separa bien? Compara el resultado (R) de las señales que habría ACEPTADO con
    las que habría RECHAZADO, usando solo señales que el filtro pudo juzgar (con casos parecidos suficientes)
    y ya cerradas. Las del laboratorio no cuentan (el filtro ya las conocía).
    Devuelve el veredicto con un intervalo de confianza (bootstrap) de la diferencia de medias."""
    import random
    aceptadas, rechazadas = [], []
    for r in registros:
        j = r.get("juicio_aprendizaje")
        if r.get("tipo") == "backtest" or r.get("resultado_r") is None or not j or not j.get("juzgada"):
            continue
        (aceptadas if j["abrir"] else rechazadas).append(r["resultado_r"])
    out = {"aceptadas": {"n": len(aceptadas), "r_medio": round(sum(aceptadas) / len(aceptadas), 3) if aceptadas else None},
           "rechazadas": {"n": len(rechazadas), "r_medio": round(sum(rechazadas) / len(rechazadas), 3) if rechazadas else None},
           "minimo_por_grupo": min_por_grupo}
    if len(aceptadas) < min_por_grupo or len(rechazadas) < min_por_grupo:
        out.update(veredicto="sin_datos", texto=f"Faltan datos: hacen falta {min_por_grupo} señales juzgadas y cerradas en "
                                                f"cada grupo (aceptadas {len(aceptadas)}, rechazadas {len(rechazadas)}).")
        return out
    rng = random.Random(semilla)
    difs = []
    for _ in range(iteraciones):
        a = [rng.choice(aceptadas) for _ in aceptadas]
        b = [rng.choice(rechazadas) for _ in rechazadas]
        difs.append(sum(a) / len(a) - sum(b) / len(b))
    difs.sort()
    lo, hi = difs[int(0.05 * iteraciones)], difs[int(0.95 * iteraciones) - 1]
    dif = sum(aceptadas) / len(aceptadas) - sum(rechazadas) / len(rechazadas)
    out.update(diferencia_r=round(dif, 3), ic90=[round(lo, 3), round(hi, 3)])
    if lo > 0:
        out.update(veredicto="aporta", texto="Las señales que acepta salen claramente mejor que las que rechaza: el filtro aporta.")
    elif hi < 0:
        out.update(veredicto="resta", texto="Las que rechaza salen MEJOR que las que acepta: el filtro estorba. "
                                            "Recomendación: desactivarlo (aprendizaje.activo = false).")
    else:
        out.update(veredicto="no_se_distingue", texto="No hay diferencia clara entre aceptadas y rechazadas: hoy el filtro "
                                                      "no demuestra aportar. Recomendación: desactivarlo o seguir midiendo.")
    return out
