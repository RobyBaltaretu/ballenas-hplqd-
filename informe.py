"""
Informe de lo que ha aprendido el bot.   Uso:  python informe.py

R = resultado medido en "riesgos": +2 R ganó el doble de lo arriesgado, -1 R saltó el stop.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from compat import consola_utf8

RUTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aprendizaje.json")


def tipo_cierre(r):
    c = r.get("cierre") or ""
    if "tiempo" in c:
        return "tiempo máximo"
    if "ballenas" in c:
        return "salida de las ballenas"
    if c in ("stop-loss", "stop sin pérdidas", "stop dinámico", "objetivo final"):
        return c
    if "stop u objetivo" in c:
        return "objetivo / stop en ganancia" if r["resultado_r"] > 0 else "stop-loss"
    return "otros (límites, fallos)"


def resumen(lista):
    if not lista:
        return "   —"
    n = len(lista)
    gan = sum(1 for r in lista if r > 0) / n * 100
    return f"{n:4d} casos | {gan:3.0f}% ganadoras | media {sum(lista) / n:+.2f} R | total {sum(lista):+.1f} R"


def tabla(titulo, grupos, minimo=1):
    print(f"\n== {titulo}")
    filas = [(k, v) for k, v in grupos.items() if len(v) >= minimo]
    filas.sort(key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
    for k, v in filas:
        print(f"  {k!s:<38} {resumen(v)}")
    if not filas:
        print("  (aún sin datos suficientes)")


def main():
    consola_utf8()
    try:
        with open(RUTA, encoding="utf-8") as f:
            regs = json.load(f)["registros"]
    except FileNotFoundError:
        print("Todavía no hay aprendizaje.json: deja el bot funcionando un tiempo.")
        return
    regs = [r for r in regs if r["tipo"] != "backtest"]
    try:
        from aprendizaje import ruta_lab
        with open(ruta_lab(RUTA), encoding="utf-8") as f:
            lab = [r for r in json.load(f)["registros"] if r.get("resultado_r") is not None]
    except (OSError, ValueError, KeyError):
        lab = []
    cerrados = [r for r in regs if r["resultado_r"] is not None]
    abiertos = len(regs) - len(cerrados)
    reales = [r["resultado_r"] for r in cerrados if r["tipo"] == "real"]
    sombras = [r for r in cerrados if r["tipo"] == "sombra"]

    print(f"Registros: {len(regs)} ({abiertos} siguiéndose ahora)")
    shadow = [r["resultado_r"] for r in cerrados if r["tipo"] == "shadow"]
    print(f"\nOperaciones del bot ({'ficticias' if any(r.get('origen') == 'simulado' for r in regs) else 'según modo'}): {resumen(reales)}")
    print(f"Intenciones SHADOW (modeladas): {resumen(shadow)}")
    print(f"Señales descartadas (en papel): {resumen([r['resultado_r'] for r in sombras])}")
    print(f"Casos del laboratorio:          {resumen([r['resultado_r'] for r in lab])}")

    por_estrategia = defaultdict(list)
    for r in cerrados:
        por_estrategia[f"{r.get('modulo') or 'ballenas (antigua)'} · {r['tipo']}"].append(r["resultado_r"])
    tabla("Por estrategia (reales y en papel)", por_estrategia, minimo=1)

    etiquetas = [
        ("aprendizaje", "aprendizaje"), ("sin validar", "estrategia sin validar"), ("ya avanzó", "precio ya avanzado"),
        ("ajustado", "stop muy ajustado"), ("lejano", "stop muy lejano"), ("ballenas cargan", "ballenas en contra"),
        ("horquilla", "horquilla ancha"), ("funding", "funding en contra"), ("correlada", "correlación"),
        ("enfriamiento", "enfriamiento"), ("límites", "límites de riesgo"), ("sin hueco", "sin hueco"),
    ]
    por_filtro = defaultdict(list)
    for r in [x for x in cerrados if x["tipo"] == "sombra" and x.get("motivos_descarte")]:
        for m in r["motivos_descarte"]:
            clave = next((et for clave_txt, et in etiquetas if clave_txt in m), m)
            por_filtro[clave].append(r["resultado_r"])
    tabla("Qué habría pasado con lo DESCARTADO, por motivo  (media positiva = el filtro quita buenas operaciones)",
          por_filtro, minimo=3)

    por_moneda, por_lado, por_hora, por_ballena, por_cierre = (defaultdict(list) for _ in range(5))
    for r in cerrados:
        res = r["resultado_r"]
        por_moneda[r["moneda"]].append(res)
        por_lado["LARGO" if r["lado"] == "L" else "CORTO"].append(res)
        h = datetime.fromtimestamp(r["ts"], timezone.utc).hour
        por_hora[f"{h // 4 * 4:02d}-{h // 4 * 4 + 4:02d} h UTC"].append(res)
        por_cierre[tipo_cierre(r)].append(res)
        for b in r.get("ballenas") or []:
            por_ballena[b[:12]].append(res)

    tabla("Por moneda (reales + papel)", por_moneda)
    tabla("Por dirección", por_lado)
    tabla("Por franja horaria", por_hora)
    tabla("Por forma de cierre", por_cierre)
    tabla("Ballenas: cuánto rinden sus señales de flujo (mín. 3 casos)", por_ballena, minimo=3)

    from aprendizaje import evaluar_filtro
    ev = evaluar_filtro(regs)
    print("\n== ¿Aporta el filtro de aprendizaje?")
    print(f"  Habría aceptado:  {ev['aceptadas']['n']:4d} señales · media {ev['aceptadas']['r_medio']} R")
    print(f"  Habría rechazado: {ev['rechazadas']['n']:4d} señales · media {ev['rechazadas']['r_medio']} R")
    if ev.get("ic90"):
        print(f"  Diferencia {ev['diferencia_r']:+.3f} R (IC 90 %: {ev['ic90'][0]:+.3f} a {ev['ic90'][1]:+.3f})")
    print("  " + ev["texto"])


if __name__ == "__main__":
    main()
