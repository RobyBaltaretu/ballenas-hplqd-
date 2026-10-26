"""
Promoción de estrategias por etapas. NADA pasa a dinero real de forma automática.

Etapas (en orden):
  investigacion    hipótesis; solo se registra lo que haría
  laboratorio      evaluada en el laboratorio (walk-forward + holdout); el laboratorio solo AÑADE EVIDENCIA
  shadow           ejecutada en modo shadow con datos nuevos (sin órdenes)
  paper            ejecutada en modo simulado con datos nuevos
  apta_revision    una persona ha revisado la evidencia y la considera apta para un piloto
  real_habilitado  aprobada por una persona para el piloto real limitado (piloto_real en config.json)

Reglas:
  - Cada aprobación queda ligada a una HUELLA determinista de TODO lo que afecta a las decisiones o al riesgo:
    la definición de la estrategia (módulo, intervalo, parámetros, salida, duración), la configuración efectiva
    completa salvo lo que no decide nada (panel, modo, dirección de la subcuenta, saldo ficticio) —riesgo,
    límites agregados y del piloto, órdenes, salidas, costes, universo, filtros, aprendizaje, ballenas...—, el
    código de señales, orquestación, motor, riesgo, ejecución, gestión, laboratorio, simulación, contabilidad y
    promoción, y las versiones del esquema de configuración, del ledger y del modelo de simulación.
    Si cualquier cosa cambia, la aprobación queda INVALIDADA. Si la huella no se puede calcular (falta un
    archivo, configuración rara), se considera NO autorizada (falla cerrado).
  - El laboratorio nunca puede aprobar ni subir límites: solo registra evidencia (run_id, dataset, métricas).
  - "Validada por el laboratorio" NO significa "autorizada para real".
  - Para 'apta_revision' hacen falta, con la MISMA huella, todos los REQUISITOS_APTA (abajo): evaluación del
    laboratorio superada, reciente y con holdout no reutilizado; y evidencia nueva shadow/paper suficiente
    (operaciones, días de observación, frescura, resultado neto, caída máxima y costes). La CLI dice qué falta.
  - La evidencia vieja, incompleta, de otra huella o con errores de conciliación NO cuenta.
  - Solo se aprueba desde la línea de comandos, escribiendo una confirmación literal. Se registra quién y cuándo.
    Ni el panel, ni Telegram, ni el laboratorio, ni el aprendizaje pueden aprobar nada.

Uso:
  python promocion.py estado
  python promocion.py requisitos <estrategia>
  python promocion.py aprobar <estrategia> <etapa> --por "Nombre" [--informe <run_id>]
  python promocion.py revocar <estrategia> --por "Nombre"
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA = os.path.join(AQUI, "promociones.json")
ETAPAS = ("investigacion", "laboratorio", "shadow", "paper", "apta_revision", "real_habilitado")
# Código que afecta a decisiones o riesgo (entra en la huella). Cualquier .py nuevo debe clasificarse aquí o en
# ARCHIVOS_SIN_EFECTO (una prueba lo exige).
ARCHIVOS_CODIGO = ("senales.py", "estrategia.py", "riesgo.py", "motor.py", "gestor.py", "ejecucion.py", "mercado.py",
                   "ballenas.py", "seleccion.py", "aprendizaje.py", "bot.py", "configuracion.py", "contabilidad.py",
                   "ledger.py", "laboratorio.py", "validacion.py", "datos.py", "archivo.py", "papel.py", "promocion.py",
                   "vigilancia.py", "limitador.py", "historia_externa.py", "cerebro.py", "cerebro_vivo.py",
                   "mercado_unificado.py")
ARCHIVOS_SIN_EFECTO = ("panel.py", "notificaciones.py", "observabilidad.py", "informe.py", "compat.py",
                       "laboratorio_semanal.py", "investigacion.py", "instancia.py", "feed.py", "estudios.py",
                       "comprobar.py", "config_panel.py", "contexto_externo.py")
# Partes de config.json que NO deciden nada (no entran en la huella). Todo lo demás, sí.
CONFIG_SIN_EFECTO = ("panel", "modo", "real", "saldo_simulado_usd", "shadow", "feed", "contexto_externo")
VERSION_HUELLA = 2

# Requisitos mínimos para 'apta_revision' (todos con la MISMA huella). Ajustables solo editando este archivo:
# cambiarlos cambia la huella y por tanto invalida aprobaciones anteriores.
REQUISITOS_APTA = {
    "lab_dias_max": 30,             # la evaluación del laboratorio superada no puede tener más de 30 días
    "min_operaciones": 30,          # operaciones shadow + paper válidas (una sola operación nunca basta)
    "min_dias_observacion": 14,     # entre la primera y la última evidencia
    "evidencia_dias_max": 7,        # la última evidencia, de hace menos de 7 días (datos frescos)
    "r_neto_medio_min": 0.0,        # media de R neto > 0
    "pnl_neto_min": 0.0,            # resultado neto acumulado (USD o R modelado) > 0
    "caida_max_r": 10.0,            # caída máxima de la curva de R ≤ 10 R
    "coste_medio_max_r": 0.35,      # costes medios (R bruto − R neto) ≤ 0,35 R por operación
}


class ErrorHuella(RuntimeError):
    """No se puede calcular la huella: nada se considera autorizado."""


def _ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def hash_codigo(carpeta=AQUI):
    """Hash de todo el código que decide. Si falta un archivo, ErrorHuella (nunca un hash 'parecido')."""
    h = hashlib.sha256()
    for nombre in ARCHIVOS_CODIGO:
        ruta = os.path.join(carpeta, nombre)
        try:
            with open(ruta, "rb") as fh:
                # Fin de línea normalizado: el mismo código en Windows y Linux da la misma huella.
                h.update(nombre.encode() + b"\0" + fh.read().replace(b"\r\n", b"\n"))
        except OSError as e:
            raise ErrorHuella(f"falta {nombre}: {e}") from None
    return h.hexdigest()[:16]


def config_decisiva(cfg):
    """Configuración efectiva que decide (sin secretos ni lo que no influye)."""
    if not isinstance(cfg, dict) or "riesgo" not in cfg:
        raise ErrorHuella("configuración incompleta")
    from configuracion import sin_secretos
    return sin_secretos({k: v for k, v in cfg.items() if k not in CONFIG_SIN_EFECTO})


def huella(estrategia, cfg, codigo=None):
    """estrategia: dict con clave, modulo, intervalo, params, salida, barras_max (lo que define su comportamiento).
    Lanza ErrorHuella si no se puede calcular."""
    from configuracion import VERSION_CONFIG
    from ledger import VERSION_ESQUEMA
    if not isinstance(estrategia, dict) or not estrategia.get("clave"):
        raise ErrorHuella("estrategia sin definición")
    base = {
        "v": VERSION_HUELLA,
        "estrategia": {k: estrategia.get(k) for k in ("clave", "modulo", "intervalo", "version", "params", "salida",
                                                      "barras_max", "duracion_min")},
        "config": config_decisiva(cfg),
        "versiones": {"config": VERSION_CONFIG, "ledger": VERSION_ESQUEMA, "simulacion": MODELO_SIMULACION},
        "requisitos": REQUISITOS_APTA,
        "codigo": codigo if codigo is not None else hash_codigo(),
    }
    return hashlib.sha256(json.dumps(base, sort_keys=True, default=str).encode()).hexdigest()[:20]


def huella_segura(estrategia, cfg, codigo=None):
    """Como huella(), pero devuelve None si no se puede calcular (y None nunca está autorizado)."""
    try:
        return huella(estrategia, cfg, codigo)
    except ErrorHuella:
        return None


MODELO_SIMULACION = 2      # sube cuando cambia cómo se simulan fills, costes o funding (papel.py / laboratorio.py)


class Registro:
    def __init__(self, ruta=RUTA):
        self.ruta = ruta
        try:
            with open(ruta, encoding="utf-8") as fh:
                self.d = json.load(fh)
        except (OSError, ValueError):
            self.d = {"version": 1, "estrategias": {}}

    def guardar(self):
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.d, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.ruta)

    def _e(self, clave):
        return self.d["estrategias"].setdefault(clave, {"etapa": "investigacion", "huella": None, "evidencias": [],
                                                        "historial": []})

    def vigente(self, clave, huella_actual):
        """Etapa que vale AHORA. Si la huella cambió (o no se puede calcular), la aprobación está invalidada."""
        e = self.d["estrategias"].get(clave)
        if huella_actual is None:
            return {"etapa": "investigacion", "invalidada": bool(e and e.get("etapa") != "investigacion"),
                    "motivo": "no se pudo calcular la huella (falta código o configuración): nada está autorizado",
                    "aprobado_por": None, "aprobado_ts": None, "informe": None}
        if not e:
            return {"etapa": "investigacion", "invalidada": False, "motivo": None, "aprobado_por": None,
                    "aprobado_ts": None, "informe": None}
        if e.get("huella") and e["huella"] != huella_actual and e["etapa"] not in ("investigacion",):
            return {"etapa": "investigacion", "invalidada": True,
                    "motivo": f"aprobación '{e['etapa']}' invalidada: cambió el código, los parámetros, los costes o el universo",
                    "aprobado_por": e.get("aprobado_por"), "aprobado_ts": e.get("aprobado_ts"), "informe": e.get("informe")}
        ultima_lab = next((x for x in reversed(e.get("evidencias", [])) if x["tipo"] == "laboratorio"), None)
        etapa = e["etapa"]
        # "laboratorio" solo si la ÚLTIMA evaluación del laboratorio con esta misma huella la superó.
        if etapa == "investigacion" and ultima_lab and ultima_lab.get("huella") == huella_actual \
                and (ultima_lab.get("resumen") or {}).get("estado", "supera") == "supera":
            etapa = "laboratorio"
        return {"etapa": etapa, "invalidada": False, "motivo": None, "aprobado_por": e.get("aprobado_por"),
                "aprobado_ts": e.get("aprobado_ts"), "informe": e.get("informe"), "evidencia_lab": ultima_lab}

    def autorizada_real(self, clave, huella_actual):
        return huella_actual is not None and self.vigente(clave, huella_actual)["etapa"] == "real_habilitado"

    @staticmethod
    def evidencia_valida(tipo, resumen):
        """(válida, motivo). Evidencia incompleta, con errores de conciliación o sin resultado NO cuenta."""
        r = resumen or {}
        if tipo == "laboratorio":
            if r.get("estado") not in ("supera", "no_supera", "no_evaluable", "exploratorio"):
                return False, "estado de laboratorio desconocido"
            return True, None
        res = r.get("r_neto", r.get("r_modelado"))
        if not isinstance(res, (int, float)):
            return False, "sin resultado"
        if r.get("conciliacion_ok") is False:
            return False, "errores de conciliación durante la operación"
        if r.get("ordenes_sin_confirmar"):
            return False, "quedaron órdenes sin confirmar"
        if r.get("datos_fills") not in (None, "reales"):
            return False, "fills no disponibles"
        return True, None

    def registrar_evidencia(self, clave, tipo, huella_actual, run_id, resumen):
        """Lo usan el laboratorio (tipo 'laboratorio') y el bot (tipo 'shadow'/'paper'). NUNCA cambia la etapa.
        Sin huella (no calculable) no se registra: evidencia sin versión no vale para nada."""
        if tipo not in ("laboratorio", "shadow", "paper"):
            raise ValueError("tipo de evidencia no válido")
        if huella_actual is None:
            return False
        valida, motivo = self.evidencia_valida(tipo, resumen)
        e = self._e(clave)
        e["evidencias"].append({"tipo": tipo, "huella": huella_actual, "run_id": run_id, "ts": _ahora(),
                                "resumen": resumen, "valida": valida, "motivo_invalida": motivo})
        del e["evidencias"][:-200]
        # Acumulado por tipo y huella (la lista de evidencias se recorta; el acumulado no).
        if tipo in ("shadow", "paper"):
            a = e.setdefault("acumulado", {}).setdefault(f"{tipo}:{huella_actual}", {"n": 0, "suma_r": 0.0, "suma_r2": 0.0,
                                                                                     "ganadoras": 0})
            for k, v in (("invalidas", 0), ("suma_pnl", 0.0), ("suma_coste_r", 0.0), ("n_coste", 0), ("curva_r", 0.0),
                         ("pico_r", 0.0), ("caida_max_r", 0.0), ("primera_ts", None), ("ultima_ts", None)):
                a.setdefault(k, v)
            if not valida:
                a["invalidas"] += 1
                return True
            r = resumen.get("r_neto", resumen.get("r_modelado"))
            ahora = time.time()
            a["n"] += 1
            a["suma_r"] += r
            a["suma_r2"] += r * r
            a["ganadoras"] += int(r > 0)
            a["suma_pnl"] += resumen.get("pnl_neto") if isinstance(resumen.get("pnl_neto"), (int, float)) else r
            if isinstance(resumen.get("r_bruto"), (int, float)) and isinstance(resumen.get("r_neto"), (int, float)):
                a["suma_coste_r"] += resumen["r_bruto"] - resumen["r_neto"]
                a["n_coste"] += 1
            a["curva_r"] += r
            a["pico_r"] = max(a["pico_r"], a["curva_r"])
            a["caida_max_r"] = max(a["caida_max_r"], a["pico_r"] - a["curva_r"])
            a["primera_ts"] = a["primera_ts"] or ahora
            a["ultima_ts"] = ahora
        return True

    def _acumulado(self, clave, huella_actual):
        """Shadow + paper juntos, con la huella actual."""
        e = self.d["estrategias"].get(clave) or {}
        tot = {"n": 0, "suma_r": 0.0, "suma_pnl": 0.0, "suma_coste_r": 0.0, "n_coste": 0, "caida_max_r": 0.0,
               "primera_ts": None, "ultima_ts": None, "invalidas": 0}
        for tipo in ("shadow", "paper"):
            a = (e.get("acumulado") or {}).get(f"{tipo}:{huella_actual}")
            if not a:
                continue
            for k in ("n", "suma_r", "suma_coste_r", "n_coste", "invalidas"):
                tot[k] += a.get(k, 0)
            tot["suma_pnl"] += a.get("suma_pnl", a.get("suma_r", 0.0))
            tot["caida_max_r"] = max(tot["caida_max_r"], a.get("caida_max_r", 0.0))
            for k, f in (("primera_ts", min), ("ultima_ts", max)):
                if a.get(k):
                    tot[k] = a[k] if tot[k] is None else f(tot[k], a[k])
        return tot

    def requisitos_apta(self, clave, huella_actual, ahora=None):
        """Lista de comprobaciones para 'apta_revision': [{"regla", "cumple", "valor"}]. Explica lo que falta."""
        ahora = ahora or time.time()
        q = REQUISITOS_APTA
        e = self.d["estrategias"].get(clave) or {}
        out = []
        if huella_actual is None:
            return [{"regla": "huella calculable", "cumple": False, "valor": None}]
        lab = next((x for x in reversed(e.get("evidencias", [])) if x["tipo"] == "laboratorio"
                    and x.get("huella") == huella_actual and x.get("valida", True)), None)
        estado_lab = ((lab or {}).get("resumen") or {}).get("estado")
        out.append({"regla": "última evaluación del laboratorio con esta huella: SUPERADA (no exploratoria)",
                    "cumple": estado_lab == "supera", "valor": estado_lab})
        edad = (ahora - _ts(lab["ts"])) / 86400 if lab and _ts(lab["ts"]) else None
        out.append({"regla": f"evaluación del laboratorio de hace ≤ {q['lab_dias_max']} días",
                    "cumple": edad is not None and edad <= q["lab_dias_max"], "valor": None if edad is None else round(edad, 1)})
        a = self._acumulado(clave, huella_actual)
        n = a["n"]
        out.append({"regla": f"≥ {q['min_operaciones']} operaciones shadow/paper válidas con esta huella",
                    "cumple": n >= q["min_operaciones"], "valor": n})
        dias = (a["ultima_ts"] - a["primera_ts"]) / 86400 if a["primera_ts"] and a["ultima_ts"] else 0.0
        out.append({"regla": f"≥ {q['min_dias_observacion']} días de observación", "cumple": dias >= q["min_dias_observacion"],
                    "valor": round(dias, 1)})
        frescura = (ahora - a["ultima_ts"]) / 86400 if a["ultima_ts"] else None
        out.append({"regla": f"última evidencia de hace ≤ {q['evidencia_dias_max']} días",
                    "cumple": frescura is not None and frescura <= q["evidencia_dias_max"],
                    "valor": None if frescura is None else round(frescura, 1)})
        media = a["suma_r"] / n if n else None
        out.append({"regla": "R neto medio > 0", "cumple": media is not None and media > q["r_neto_medio_min"],
                    "valor": None if media is None else round(media, 3)})
        out.append({"regla": "resultado neto acumulado > 0", "cumple": n > 0 and a["suma_pnl"] > q["pnl_neto_min"],
                    "valor": round(a["suma_pnl"], 4)})
        out.append({"regla": f"caída máxima ≤ {q['caida_max_r']} R", "cumple": n > 0 and a["caida_max_r"] <= q["caida_max_r"],
                    "valor": round(a["caida_max_r"], 2)})
        coste = a["suma_coste_r"] / a["n_coste"] if a["n_coste"] else None
        out.append({"regla": f"costes medios ≤ {q['coste_medio_max_r']} R por operación (medidos en paper)",
                    "cumple": coste is None or coste <= q["coste_medio_max_r"],
                    "valor": "sin datos de costes (solo shadow)" if coste is None else round(coste, 3)})
        out.append({"regla": "evidencia inválida descartada (informativo)", "cumple": True, "valor": a["invalidas"]})
        return out

    def progreso(self, clave, huella_actual, objetivo=None):
        """Cuánta evidencia NUEVA (shadow/paper con la huella actual) lleva la estrategia."""
        objetivo = objetivo or REQUISITOS_APTA["min_operaciones"]
        e = self.d["estrategias"].get(clave) or {}
        out = {"objetivo": objetivo}
        for tipo in ("shadow", "paper"):
            a = (e.get("acumulado") or {}).get(f"{tipo}:{huella_actual}") or {"n": 0, "suma_r": 0.0, "suma_r2": 0.0,
                                                                            "ganadoras": 0}
            n = a["n"]
            media = a["suma_r"] / n if n else None
            var = (a["suma_r2"] - n * media * media) / (n - 1) if n > 1 else None
            t = media / ((var ** 0.5) / n ** 0.5) if var and var > 0 else None
            out[tipo] = {"n": n, "r_medio": round(media, 3) if media is not None else None,
                         "ganadoras_pct": round(a["ganadoras"] / n * 100, 1) if n else None,
                         "t": round(t, 2) if t is not None else None, "invalidas": a.get("invalidas", 0),
                         "caida_max_r": round(a.get("caida_max_r", 0.0), 2)}
        lab = next((x for x in reversed(e.get("evidencias", [])) if x["tipo"] == "laboratorio"
                    and x.get("huella") == huella_actual), None)
        out["laboratorio"] = (lab or {}).get("resumen", {}).get("estado") if lab else None
        if huella_actual is not None:
            req = self.requisitos_apta(clave, huella_actual)
            out["requisitos_apta"] = {"cumplidos": sum(1 for x in req if x["cumple"]), "total": len(req),
                                      "faltan": [x["regla"] for x in req if not x["cumple"]]}
        return out

    def aprobar(self, clave, etapa, por, huella_actual, informe=None):
        if etapa not in ETAPAS:
            raise ValueError(f"etapa desconocida: {etapa}")
        if not por or not str(por).strip():
            raise ValueError("hace falta indicar quién aprueba (--por)")
        e = self._e(clave)
        actual = self.vigente(clave, huella_actual)["etapa"]
        if etapa == "real_habilitado" and actual != "apta_revision":
            raise ValueError("para 'real_habilitado' la estrategia debe estar antes en 'apta_revision' con la MISMA huella")
        if huella_actual is None:
            raise ValueError("no se puede calcular la huella: no se aprueba nada")
        if etapa == "apta_revision":
            faltan = [x for x in self.requisitos_apta(clave, huella_actual) if not x["cumple"]]
            if faltan:
                raise ValueError("para 'apta_revision' faltan requisitos:\n" + "\n".join(
                    f"   ✗ {x['regla']} (ahora: {x['valor']})" for x in faltan))
        e["historial"].append({"de": e["etapa"], "a": etapa, "por": por, "ts": _ahora(), "huella": huella_actual,
                               "informe": informe})
        e.update(etapa=etapa, huella=huella_actual, aprobado_por=por, aprobado_ts=_ahora(), informe=informe)

    def revocar(self, clave, por):
        e = self._e(clave)
        e["historial"].append({"de": e["etapa"], "a": "investigacion", "por": por, "ts": _ahora(), "revocada": True})
        e.update(etapa="investigacion", huella=None, aprobado_por=None, aprobado_ts=None, informe=None)


def _estrategias_actuales():
    sys.path.insert(0, AQUI)
    from configuracion import cargar, migrar, validar
    from motor import Motor

    class _Log:
        def info(self, *a, **k): pass
        warning = error = debug = info
    cfg, _ = migrar(cargar(os.path.join(AQUI, "config.json")))
    cfg = validar(cfg, entorno={k: v for k, v in os.environ.items() if k in ("HL_MODO",)})
    m = Motor(cfg, _Log(), os.path.join(AQUI, "estrategia_elegida.json"))
    return cfg, {e["clave"]: e for e in m.todas_las_estrategias()}


def main():
    ap = argparse.ArgumentParser(description="Promoción de estrategias (nada pasa a real sin aprobación humana)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("estado")
    a = sub.add_parser("aprobar")
    a.add_argument("estrategia")
    a.add_argument("etapa", choices=ETAPAS)
    a.add_argument("--por", required=True)
    a.add_argument("--informe")
    q = sub.add_parser("requisitos")
    q.add_argument("estrategia")
    r = sub.add_parser("revocar")
    r.add_argument("estrategia")
    r.add_argument("--por", required=True)
    args = ap.parse_args()
    cfg, estrategias = _estrategias_actuales()
    reg = Registro()
    if args.cmd == "estado":
        for clave, e in sorted(estrategias.items()):
            v = reg.vigente(clave, huella_segura(e, cfg))
            extra = f" ({v['motivo']})" if v["invalidada"] else (f" · aprobada por {v['aprobado_por']} el {v['aprobado_ts']}"
                                                                 if v.get("aprobado_por") else "")
            print(f"  {clave:<20} {v['etapa']}{extra}")
        return
    if args.estrategia not in estrategias:
        raise SystemExit(f"Estrategia desconocida: {args.estrategia}")
    try:
        h = huella(estrategias[args.estrategia], cfg)
    except ErrorHuella as e:
        raise SystemExit(f"No se puede calcular la huella ({e}): no se aprueba nada.") from None
    if args.cmd == "requisitos":
        print(f"Requisitos para 'apta_revision' de {args.estrategia} (huella {h}):")
        for x in reg.requisitos_apta(args.estrategia, h):
            print(f"  {'✓' if x['cumple'] else '✗'} {x['regla']}  (ahora: {x['valor']})")
        print("'real_habilitado' exige además estar en 'apta_revision' con la misma huella y una aprobación humana.")
        return
    if args.cmd == "revocar":
        reg.revocar(args.estrategia, args.por)
        reg.guardar()
        print("Revocada.")
        return
    frase = f"CONFIRMO {args.estrategia} {args.etapa}"
    print(f"Vas a poner '{args.estrategia}' en la etapa '{args.etapa}' (huella {h}).")
    if args.etapa == "real_habilitado":
        print("ATENCIÓN: esto permite que la estrategia opere con DINERO REAL dentro de los límites de 'piloto_real'.")
    if input(f"Escribe exactamente «{frase}» para continuar: ").strip() != frase:
        raise SystemExit("Cancelado: la confirmación no coincide.")
    try:
        reg.aprobar(args.estrategia, args.etapa, args.por, h, args.informe)
    except ValueError as e:
        raise SystemExit(f"No se puede: {e}") from None
    reg.guardar()
    print("Hecho.")


if __name__ == "__main__":
    main()
