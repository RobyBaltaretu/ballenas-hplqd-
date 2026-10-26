"""
Vigilancia de degradación: ¿alguna estrategia en uso está rindiendo claramente peor de lo que debería?

Para cada estrategia se miran sus operaciones cerradas de los últimos 'ventana_dias' (las del bot y las de shadow,
nunca las del laboratorio), EN ORDEN TEMPORAL, y se estima la incertidumbre de su R medio con un BOOTSTRAP POR
BLOQUES (respeta que operaciones seguidas se parecen; no supone normalidad ni independencia). Como se vigilan
varias estrategias a la vez, el nivel se corrige por su número (Bonferroni): con k estrategias, cota superior
unilateral al 1 − 5 %/k.

Estados:
  sin_datos     menos de 'min_n' operaciones: no se juzga
  ok            nada preocupante
  vigilar       media negativa o muy por debajo de lo esperado, pero la cota superior aún incluye ≥ 0
  degradada     incluso la cota superior (optimista) del R medio es < 0: claramente negativa

Lo que hace el bot va SOLO en la dirección segura (ver fases()):
  - degradada -> tamaño ×0,5 y, si estaba autorizada para real, se le RETIRA la autorización; avisa.
  - Recuperación GRADUAL y con persistencia: deja de estar degradada -> fase 'recuperacion' (sigue ×0,5); tras
    'dias_persistencia' días seguidos sin degradarse y con media ≥ 0 -> ×0,75; tras otro periodo igual -> normal.
    Si vuelve a degradarse, vuelta a ×0,5 y se reinicia la cuenta.
  - Nunca sube por encima del tamaño autorizado (el factor solo multiplica por ≤ 1) y nunca devuelve una
    autorización retirada: eso es decisión de una persona (promocion.py).
"""

import math
import random
import time

ALFA = 0.05
FASES = ("normal", "degradada", "recuperacion", "recuperacion_avanzada")
FACTOR_FASE = {"normal": 1.0, "degradada": 0.5, "recuperacion": 0.5, "recuperacion_avanzada": 0.75}


def _cota_superior(rs, nivel, bloque=5, iteraciones=2000, semilla=7):
    """Cota superior unilateral del R medio (bootstrap de bloques móviles, reproducible)."""
    n = len(rs)
    b = max(1, min(bloque, n))
    rng = random.Random(semilla)
    medias = []
    for _ in range(iteraciones):
        m = []
        while len(m) < n:
            i = rng.randrange(0, n - b + 1)
            m.extend(rs[i:i + b])
        medias.append(sum(m[:n]) / n)
    medias.sort()
    return medias[min(iteraciones - 1, int(nivel * iteraciones))]


def evaluar(registros, esperado=None, ahora=None, ventana_dias=60, min_n=30):
    """registros: los de la memoria del bot. esperado: {clave: r_medio esperado (laboratorio)}."""
    ahora = ahora or time.time()
    esperado = esperado or {}
    por_clave = {}
    for r in registros:
        if r.get("tipo") not in ("real", "shadow") or r.get("resultado_r") is None:
            continue
        if (r.get("cierre_ts") or 0) < ahora - ventana_dias * 86400:
            continue
        por_clave.setdefault(r.get("modulo") or "?", []).append((r.get("cierre_ts") or 0, r["resultado_r"]))
    k = max(1, sum(1 for v in por_clave.values() if len(v) >= min_n))       # estrategias juzgables a la vez
    nivel = 1 - ALFA / k
    out = {}
    for clave, pares in por_clave.items():
        rs = [x for _, x in sorted(pares)]
        n = len(rs)
        media = sum(rs) / n
        se = math.sqrt(sum((x - media) ** 2 for x in rs) / (n - 1)) / math.sqrt(n) if n > 1 else None
        esp = esperado.get(clave)
        d = {"n": n, "r_medio": round(media, 3), "error_tipico": round(se, 3) if se else None, "esperado": esp,
             "estrategias_vigiladas": k, "nivel": round(nivel, 4), "cota_superior": None}
        if n < min_n:
            d.update(estado="sin_datos", texto=f"{n} operaciones en {ventana_dias} días (hacen falta {min_n})")
            out[clave] = d
            continue
        cota = _cota_superior(rs, nivel)
        d["cota_superior"] = round(cota, 3)
        if cota < 0:
            d.update(estado="degradada", texto=f"claramente negativa: {media:+.2f} R de media en {n} operaciones "
                                               f"(cota superior {cota:+.2f} R al {nivel * 100:.1f} %)")
        elif media < 0 or (esp is not None and se and media < esp - 2 * se):
            d.update(estado="vigilar", texto=f"{media:+.2f} R de media en {n} operaciones (aún no concluyente; "
                                             f"cota superior {cota:+.2f} R)")
        else:
            d.update(estado="ok", texto=f"{media:+.2f} R de media en {n} operaciones")
        out[clave] = d
    return out


def fases(previas, evaluacion, ahora=None, dias_persistencia=7):
    """Máquina de estados de la reducción de tamaño. previas: {clave: {"fase", "desde"}} (estado guardado).
    Devuelve (nuevas, cambios) con nuevas[clave] = {"fase", "desde", "factor"}. Solo baja tamaño o lo recupera
    POCO A POCO; nunca pasa de 1."""
    ahora = ahora or time.time()
    nuevas, cambios = {}, []
    claves = set(previas) | set(evaluacion)
    for clave in claves:
        p = previas.get(clave) or {"fase": "normal", "desde": ahora}
        e = evaluacion.get(clave) or {"estado": "sin_datos"}
        fase, desde = p.get("fase", "normal"), p.get("desde", ahora)
        estado = e["estado"]
        if estado == "degradada":
            if fase != "degradada":
                fase, desde = "degradada", ahora
        elif fase == "degradada":
            fase, desde = "recuperacion", ahora                  # deja de estar degradada: aún NO recupera tamaño
        elif fase in ("recuperacion", "recuperacion_avanzada"):
            sana = estado in ("ok", "sin_datos") or (estado == "vigilar" and (e.get("r_medio") or 0) >= 0)
            if not sana:
                desde = ahora                                    # la persistencia vuelve a empezar
            elif ahora - desde >= dias_persistencia * 86400:
                fase, desde = ("recuperacion_avanzada" if fase == "recuperacion" else "normal"), ahora
        if fase != p.get("fase", "normal"):
            cambios.append((clave, p.get("fase", "normal"), fase, e.get("texto")))
        if fase != "normal":
            nuevas[clave] = {"fase": fase, "desde": desde, "factor": FACTOR_FASE[fase]}
    return nuevas, cambios
