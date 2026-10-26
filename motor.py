"""
Motor de señales EN VIVO: une el universo de monedas líquidas, los módulos de senales.py
(con los parámetros que eligió el laboratorio) y el flujo de las ballenas.

  - Universo: las monedas con más volumen en Hyperliquid ahora mismo (config "universo"),
    revisado cada hora. Ya no depende de en qué monedas operen las ballenas.
  - Al cerrar cada vela de 1 h / 4 h, mira cada moneda con cada módulo. Si un módulo ve
    entrada, devuelve una "candidata" que el bot filtra (horquilla, funding, ballenas en
    contra, correlación, aprendizaje, riesgo) antes de abrir.
  - "laboratorio_superado" es solo EVIDENCIA (estrategia_elegida.json). Para operar con dinero real hace
    falta además una promoción humana vigente (promocion.py); eso lo comprueba el bot, no el motor.
  - Cada estrategia tiene clave (p. ej. "ruptura_4h") y versión (senales.MODULOS[...]["version"]).
  - Las estrategias desactivadas en config ("estrategias": {clave: {"activa": false}}) no generan señales;
    las de investigación (ballenas, liquidaciones) generan señales marcadas "solo_investigacion".
"""

import json

import investigacion
import math
import os
import threading
import time
from datetime import datetime, timezone

from ballenas import moneda_valida
from configuracion import estrategia_activa
from senales import (
    INTERVALOS,
    MODULOS,
    Contexto,
    SerieFunding,
    barras_max,
    detectar,
    rangos_fuerza,
    umbrales_funding,
    usa_intervalo,
)

VERSION_BALLENAS = "1"
VERSION_LIQUIDACIONES = "1"

VELAS_HISTORIA = 300          # suficientes para la media de 100 y el ATR medio de 50
MARGEN_CIERRE_SEG = 20        # espera tras el cierre de la vela para que Hyperliquid la tenga completa


class Motor:
    def __init__(self, c, log, ruta_elegida):
        self.c, self.log = c, log
        self.modo = c.get("modo", "real")
        self.cm = c["motor"]
        self.ruta_elegida = ruta_elegida
        self._mtime = self._mtime_elegida()
        self.estado_velas = {}          # intervalo -> última vela cerrada, próxima y hora de evaluación (panel)
        self.estrategias, self.info_lab = self._cargar_estrategias()
        self.monedas, self.volumen, self.funding_actual = [], {}, {}
        self.ultimo_universo = 0.0
        self.procesado = {}                    # (moneda, intervalo) -> t de la última vela estudiada
        self.contextos = {}                    # (moneda, intervalo) -> (t última vela, Contexto)
        self.funding_hist = {}                 # moneda -> (ts actualización, SerieFunding, registros)
        self._hilo_funding = None
        self.ultima_senal_ballenas = {}        # (moneda, lado) -> ts
        self.velas_estudiadas = 0
        self.ruta_investigacion = os.path.join(os.path.dirname(os.path.abspath(ruta_elegida)), "investigacion.json")
        self._mtime_inv, self.aspirantes = None, {}

    # ------------------------------------------------------------ estrategias
    def _mtime_elegida(self):
        try:
            return os.path.getmtime(self.ruta_elegida)
        except OSError:
            return None

    def recargar_si_cambia(self):
        """Si el laboratorio ha generado un estrategia_elegida.json nuevo, lo aplica sin reiniciar el bot."""
        m = self._mtime_elegida()
        if m == self._mtime:
            return False
        self._mtime = m
        self.estrategias, self.info_lab = self._cargar_estrategias()
        self.log.info("Nuevo resultado del laboratorio: %d de %d estrategias validadas (%s).", self.info_lab["validadas"],
                      len(self.estrategias), ", ".join(e["clave"] for e in self.estrategias if e["validada"]) or "ninguna")
        return True

    def _cargar_estrategias(self):
        """Lee estrategia_elegida.json (laboratorio). Sin él, usa los parámetros por defecto de cada módulo."""
        info = {"generado": None, "validadas": 0, "fuente": "parámetros por defecto (aún no has ejecutado el laboratorio)",
                "ballenas_validada": False, "ballenas_predictivas": [], "puntuacion_ballenas": {}, "extra_validadas": []}
        resultados = {}
        try:
            with open(self.ruta_elegida, encoding="utf-8") as fh:
                lab = json.load(fh)
            resultados = lab.get("resultados", {})
            info.update(generado=lab.get("generado"), fuente="laboratorio",
                        ballenas_validada=bool(resultados.get("ballenas_1h", {}).get("supera")),
                        extra_validadas=[k for k in ("liquidaciones_1h",) if resultados.get(k, {}).get("supera")],
                        ballenas_predictivas=[b.lower() for b in lab.get("ballenas_predictivas", [])],
                        puntuacion_ballenas=lab.get("puntuacion_ballenas", {}),
                        resultados_extra={k: resultados[k].get("validacion") for k in ("ballenas_1h", "liquidaciones_1h")
                                          if k in resultados})
        except (OSError, ValueError):
            pass
        estrategias = []
        for iv in self.cm["intervalos"]:
            if iv not in INTERVALOS:
                continue
            for nombre, mod in MODULOS.items():
                if not usa_intervalo(nombre, iv):
                    continue
                clave = f"{nombre}_{iv}"
                r = resultados.get(clave)
                params = dict(mod["defecto"], **(r["params"] if r else {}))
                estrategias.append({
                    "clave": clave, "modulo": nombre, "intervalo": iv, "params": params,
                    "version": mod.get("version", "1"),
                    "salida": dict(self.c["salida"], **(r["salida"] if r else mod["salida"])),
                    "barras_max": r["barras_max"] if r else barras_max(nombre, params),
                    "duracion_min": (r["barras_max"] if r else barras_max(nombre, params)) * INTERVALOS[iv],
                    "activa": estrategia_activa(self.c, clave),
                    "validada": bool(r and r.get("supera")),
                    # Si solo supera con el filtro de aprendizaje, se muestra ese resultado (es el que cuenta).
                    "validacion": (r or {}).get("validacion_con_aprendizaje" if (r or {}).get("supera_solo_con_aprendizaje")
                                                else "validacion"),
                })
        info["validadas"] = (sum(e["validada"] for e in estrategias) + int(info["ballenas_validada"])
                             + len(info["extra_validadas"]))
        return estrategias, info

    @property
    def predictivas(self):
        """Ballenas cuyas cargas anticipan el precio según el laboratorio (None = aún sin puntuar: todas)."""
        return set(self.info_lab["ballenas_predictivas"]) or None

    def todas_las_estrategias(self):
        """Definición de TODAS las estrategias (motor + ballenas + liquidaciones), para promoción y panel."""
        cb, cl = self.c["ballenas"], self.c["mercado"]["liquidaciones"]
        extra = [
            {"clave": "ballenas_1h", "modulo": "ballenas", "intervalo": "1h", "version": VERSION_BALLENAS,
             "params": {k: cb[k] for k in ("ventana_min", "aumento_min_usd", "flujo_min_usd", "contra_max_fraccion", "sl_atr")},
             "salida": dict(self.c["salida"], **cb["salida"]), "barras_max": cb["barras_max"],
             "duracion_min": cb["barras_max"] * 60, "activa": estrategia_activa(self.c, "ballenas_1h"),
             "validada": self.info_lab["ballenas_validada"],
             "validacion": self.info_lab.get("resultados_extra", {}).get("ballenas_1h")},
            {"clave": "liquidaciones_1h", "modulo": "liquidaciones", "intervalo": "1h", "version": VERSION_LIQUIDACIONES,
             "params": {k: cl[k] for k in ("ventana_min", "caida_oi_pct", "movimiento_atr", "sl_atr")},
             "salida": dict(self.c["salida"], **cl["salida"]), "barras_max": cl["barras_max"],
             "duracion_min": cl["barras_max"] * 60, "activa": estrategia_activa(self.c, "liquidaciones_1h"),
             "validada": "liquidaciones_1h" in self.info_lab["extra_validadas"],
             "validacion": self.info_lab.get("resultados_extra", {}).get("liquidaciones_1h")},
        ]
        return self.estrategias + extra

    def definicion(self, clave):
        return next((e for e in self.todas_las_estrategias() if e["clave"] == clave), None)

    # ------------------------------------------------------------ universo
    def actualizar_universo(self, info, meta, forzar=False):
        """Monedas más líquidas ahora (y el funding actual de todas). Cada hora."""
        if not forzar and self.monedas and time.time() - self.ultimo_universo < 3600:
            return self.monedas
        u = self.c["universo"]
        try:
            m, ctxs = info.meta_and_asset_ctxs()
        except Exception as e:  # noqa: BLE001
            self.log.warning("No pude leer el universo de monedas: %s", e)
            return self.monedas
        filas = []
        for a, x in zip(m["universe"], ctxs):
            nombre = a["name"]
            if a.get("isDelisted") or not moneda_valida(nombre) or nombre not in meta:
                continue
            vol = float(x.get("dayNtlVlm") or 0)
            self.volumen[nombre] = vol
            self.funding_actual[nombre] = float(x.get("funding") or 0) * 100   # % por hora
            if vol >= u["volumen_min_24h_usd"] and nombre not in u["excluir"]:
                filas.append((vol, nombre))
        filas.sort(reverse=True)
        monedas = [n for _, n in filas[: u["max_monedas"]]]
        for n in u["incluir_siempre"]:
            if n in meta and n not in monedas and n not in u["excluir"]:
                monedas.append(n)
        if monedas != self.monedas:
            self.log.info("Universo (%d monedas por volumen): %s", len(monedas), ", ".join(monedas))
        self.monedas, self.ultimo_universo = monedas, time.time()
        return monedas

    def refrescar_funding(self, info):
        """Funding actual de todas las monedas (para filtros y rasgos)."""
        try:
            m, ctxs = info.meta_and_asset_ctxs()
            for a, x in zip(m["universe"], ctxs):
                self.funding_actual[a["name"]] = float(x.get("funding") or 0) * 100
        except Exception as e:  # noqa: BLE001
            self.log.warning("No pude leer el funding: %s", e)

    # ------------------------------------------------------------ velas
    def contexto(self, info, moneda, iv, forzar=False):
        """Contexto de velas CERRADAS. Se reutiliza mientras no cierre una vela nueva."""
        min_v = INTERVALOS[iv]
        ahora = time.time()
        ultima_cerrada = (int(ahora // (min_v * 60)) - 1) * min_v * 60
        guardado = self.contextos.get((moneda, iv))
        if guardado and guardado[0] >= ultima_cerrada and not forzar:
            return guardado[1]
        fin = int(ahora * 1000)
        datos = info.candles_snapshot(moneda, iv, fin - (VELAS_HISTORIA + 2) * min_v * 60_000, fin)
        cerradas = sorted((v for v in datos if int(v["t"]) + min_v * 60_000 <= fin), key=lambda v: int(v["t"]))
        if len(cerradas) < 120:
            return None
        ctx = Contexto(cerradas)
        self.contextos[(moneda, iv)] = (ctx.t[-1], ctx)
        return ctx

    # ------------------------------------------------------------ funding histórico
    def _umbrales(self, moneda, params):
        f = self.funding_hist.get(moneda)
        if not f:
            return None
        serie = f[1]
        hist = serie.medias_historicas(time.time(), params["horas"])
        alto, bajo = umbrales_funding(hist, params["percentil"])
        if alto is None or bajo is None:
            return None
        return {"funding_medio": serie.media(time.time(), params["horas"]), "umbral_alto": alto, "umbral_bajo": bajo}

    def preparar_funding(self):
        """Mantiene en segundo plano el funding histórico de cada moneda: la primera vez descarga los últimos
        meses y después, cada hora, solo lo nuevo (una petición por moneda)."""
        if not any(e["modulo"] == "funding" for e in self.estrategias) or not self.monedas:
            return
        if self._hilo_funding and self._hilo_funding.is_alive():
            return
        pendientes = [m for m in self.monedas
                      if m not in self.funding_hist or time.time() - self.funding_hist[m][0] > 3600]
        if not pendientes:
            return

        def tarea():
            from datos import Api, descargar_funding
            api = Api(usar_cache=False)
            dias = self.cm.get("dias_funding_vivo", 90)
            for m in pendientes:
                try:
                    fin = int(time.time() * 1000)
                    previo = self.funding_hist.get(m)
                    regs = list(previo[2]) if previo else []
                    desde = regs[-1]["time"] + 1 if regs else fin - dias * 86400_000
                    nuevos = descargar_funding(api, m, desde, fin)
                    regs = [r for r in regs + nuevos if r["time"] >= fin - dias * 86400_000]
                    if regs:
                        self.funding_hist[m] = (time.time(), SerieFunding(regs), regs)
                except Exception as e:  # noqa: BLE001
                    self.log.debug("Funding histórico de %s no disponible: %s", m, e)
                time.sleep(1 if m in self.funding_hist else 2)   # sin prisa: no competir con el bot

        self._hilo_funding = threading.Thread(target=tarea, daemon=True)
        self._hilo_funding.start()

    # ------------------------------------------------------------ señales
    def rasgos(self, ctx, i, lado, moneda, fuerza, flujo, btc_ctx, ahora):
        s = 1 if lado == "L" else -1
        ra = ctx.rasgos(lado, i)
        ra["fuerza"] = fuerza
        ra["lado"] = s
        if btc_ctx is not None and moneda != "BTC" and len(btc_ctx) > 5:
            ra["btc_mov"] = s * (btc_ctx.c[-1] / btc_ctx.c[-5] - 1) * 100
        else:
            ra["btc_mov"] = 0.0
        ra["funding_contra"] = s * self.funding_actual.get(moneda, 0.0)
        ra["flujo_ballenas"] = flujo.flujo(moneda, lado, ahora, 3600, self.predictivas) / 1e5 if flujo else 0.0
        h = datetime.fromtimestamp(ahora, timezone.utc).hour
        ra["hora_sin"], ra["hora_cos"] = math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)
        return {k: round(v, 5) if isinstance(v, float) else v for k, v in ra.items()}

    def cargar_aspirantes(self):
        """Ideas del departamento de investigación que se siguen en vivo SIN dinero (solo observación)."""
        try:
            m = os.path.getmtime(self.ruta_investigacion)
        except OSError:
            self.aspirantes = {}
            return
        if m == self._mtime_inv:
            return
        self._mtime_inv = m
        try:
            with open(self.ruta_investigacion, encoding="utf-8") as fh:
                ideas = json.load(fh).get("ideas", {})
            self.aspirantes = {k: v for k, v in ideas.items() if v.get("estado") in ("aspirante", "lista_revision")}
            if self.aspirantes:
                self.log.info("Aspirantes del departamento de investigación en observación: %d", len(self.aspirantes))
        except (OSError, ValueError) as e:
            self.log.warning("No pude leer investigacion.json: %s", e)

    def nuevas_senales(self, info, flujo=None):
        """Estudia las velas que acaban de cerrar. Devuelve una lista de candidatas."""
        ahora = time.time()
        self.cargar_aspirantes()
        candidatas = []
        for iv in {e["intervalo"] for e in self.estrategias}:
            min_v = INTERVALOS[iv]
            seg = min_v * 60
            t_ultima = (int(ahora // seg) - 1) * seg          # apertura de la última vela cerrada
            if ahora < t_ultima + seg + MARGEN_CIERRE_SEG:
                t_ultima -= seg                               # aún no ha pasado el margen: la anterior
            # Solo vale una vela que cerró hace poco (si el bot arranca tarde, se espera a la siguiente).
            reciente = ahora - (t_ultima + seg) <= min(900, seg / 4)
            ev = self.estado_velas.setdefault(iv, {})
            ev.update(ultima_cerrada_abre_ts=t_ultima, ultima_cerrada_cierra_ts=t_ultima + seg,
                      proximo_cierre_ts=(int(ahora // seg) + 1) * seg, margen_tras_cierre_s=MARGEN_CIERRE_SEG,
                      senal_solo_al_cierre=True, reciente=reciente)
            estr = [e for e in self.estrategias if e["intervalo"] == iv and e["activa"]]
            btc_ctx = None
            nuevos = {}
            for moneda in self.monedas:
                clave = (moneda, iv)
                if self.procesado.get(clave, 0) >= t_ultima:
                    continue
                self.procesado[clave] = t_ultima
                if not reciente:
                    continue
                try:
                    ctx = self.contexto(info, moneda, iv)
                    if btc_ctx is None and "BTC" in self.monedas:
                        btc_ctx = self.contexto(info, "BTC", iv)
                except Exception as e:  # noqa: BLE001
                    self.log.warning("Velas de %s %s no disponibles: %s", moneda, iv, e)
                    continue
                finally:
                    time.sleep(1.0)   # repartir las peticiones (límite de Hyperliquid): todas cierran vela a la vez
                if ctx is None or ctx.t[-1] < t_ultima:
                    continue
                self.velas_estudiadas += 1
                nuevos[moneda] = ctx
            if not nuevos:
                continue
            ev.update(evaluada_ts=time.time(), vela_evaluada_abre_ts=t_ultima, monedas_evaluadas=len(nuevos))
            # Fuerza relativa: ranking de TODAS las monedas con la vela recién cerrada.
            rangos = {}
            for e in estr:
                if e["modulo"] == "fuerza":
                    lb = e["params"]["lookback"]
                    if lb not in rangos:
                        tabla = rangos_fuerza(list(nuevos.items()), lb)
                        rangos[lb] = {m: tabla.get(m, {}).get(len(ctx) - 1) for m, ctx in nuevos.items()}
            for moneda, ctx in nuevos.items():
                i = len(ctx) - 1
                for e in estr:
                    if e["modulo"] == "funding":
                        extra = self._umbrales(moneda, e["params"])
                        if extra is None:
                            continue
                    elif e["modulo"] == "fuerza":
                        extra = rangos[e["params"]["lookback"]].get(moneda)
                    else:
                        extra = None
                    s = detectar(e["modulo"], ctx, i, e["params"], extra)
                    if not s:
                        continue
                    candidatas.append({
                        "moneda": moneda, "lado": s["lado"], "estrategia": e["clave"], "modulo": e["modulo"],
                        "version": e["version"], "intervalo": iv, "sl_pct": s["sl_pct"], "fuerza": s["fuerza"],
                        "motivo": s["motivo"], "parametros": e["params"],
                        "salida": e["salida"], "duracion_min": e["barras_max"] * min_v, "validada": e["validada"],
                        "precio_senal": ctx.c[i], "vela_ts": ctx.t[i],
                        "rasgos": self.rasgos(ctx, i, s["lado"], moneda, s["fuerza"], flujo, btc_ctx, ahora),
                    })
                # Aspirantes: misma vela, misma lógica que en el examen (investigacion.detectar). Solo observación.
                for k, a in self.aspirantes.items():
                    if a["def"]["intervalo"] != iv:
                        continue
                    s = investigacion.detectar(a["def"], ctx, i, investigacion.btc_alcista_en(btc_ctx, ctx.t[i]))
                    if not s:
                        continue
                    salida, barras = investigacion.salida_de(a["def"], self.c["salida"])
                    candidatas.append({
                        "moneda": moneda, "lado": s["lado"], "estrategia": "inv:" + k, "modulo": "aspirante",
                        "aspirante": k, "solo_investigacion": True, "version": "inv", "intervalo": iv,
                        "sl_pct": s["sl_pct"], "fuerza": s["fuerza"], "motivo": s["motivo"], "parametros": a["def"],
                        "salida": salida, "duracion_min": barras * min_v, "validada": False,
                        "precio_senal": ctx.c[i], "vela_ts": ctx.t[i], "rasgos": {}})
        return candidatas

    def senales_ballenas(self, info, flujo):
        """Módulo 'ballenas': flujo fuerte y mayoritario de grandes cuentas en una moneda del universo."""
        cfg = self.c["ballenas"]
        if not cfg.get("activo", True) or flujo is None:
            return []
        ahora = time.time()
        out = []
        for moneda in self.monedas:
            s = flujo.senal(moneda, ahora, cfg, self.predictivas)
            if not s:
                continue
            clave = (moneda, s["lado"])
            if ahora - self.ultima_senal_ballenas.get(clave, 0) < cfg["ventana_min"] * 60:
                continue
            self.ultima_senal_ballenas[clave] = ahora
            try:
                ctx = self.contexto(info, moneda, "1h")
            except Exception as e:  # noqa: BLE001
                self.log.warning("Velas de %s 1h no disponibles: %s", moneda, e)
                continue
            if ctx is None:
                continue
            i = len(ctx) - 1
            fuerza = round(s["total_usd"] / 1e5, 3)
            salida = dict(self.c["salida"], **cfg["salida"])
            out.append({
                "moneda": moneda, "lado": s["lado"], "estrategia": "ballenas_1h", "modulo": "ballenas",
                "version": VERSION_BALLENAS, "parametros": {k: cfg[k] for k in ("ventana_min", "aumento_min_usd", "flujo_min_usd")},
                # Hipótesis sin validar: por defecto solo se registra (no abre ni en simulado).
                "solo_investigacion": cfg.get("modo_senal") != "simulado" or not estrategia_activa(self.c, "ballenas_1h"),
                "evidencia_ballenas": {"ballenas": s["ballenas"], "total_usd": round(s["total_usd"]),
                                       "contra_usd": round(s["contra_usd"]), "ventana_min": cfg["ventana_min"],
                                       "etiqueta": "observación (flujo neto de posiciones; no es intención declarada)"},
                "intervalo": "1h", "sl_pct": cfg["sl_atr"] * ctx.atr[i] / ctx.c[i] * 100, "fuerza": fuerza,
                "motivo": f"{len(s['ballenas'])} ballenas cargando {s['total_usd'] / 1000:.0f}k USD en la última hora",
                "salida": salida, "duracion_min": cfg["barras_max"] * 60,
                "validada": self.info_lab["ballenas_validada"],
                "precio_senal": ctx.c[i], "ballenas": s["ballenas"],
                "rasgos": self.rasgos(ctx, i, s["lado"], moneda, fuerza, flujo, None, ahora),
            })
        return out

    def resumen(self):
        return {"fuente": self.info_lab["fuente"], "generado": self.info_lab["generado"],
                "validadas": self.info_lab["validadas"], "monedas": self.monedas,
                "ballenas_validada": self.info_lab["ballenas_validada"],
                "extra_validadas": self.info_lab["extra_validadas"],
                "resultados_extra": self.info_lab.get("resultados_extra", {}),
                "ballenas_predictivas": self.info_lab["ballenas_predictivas"],
                "velas": self.estado_velas,
                "estrategias": [{"clave": e["clave"], "version": e["version"], "validada": e["validada"],
                                 "params": e["params"], "validacion": e["validacion"], "activa": e["activa"],
                                 "duracion_min": e["duracion_min"]} for e in self.todas_las_estrategias()]}
