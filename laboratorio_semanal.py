"""
Laboratorio semanal: pasa el laboratorio, lo compara con la ejecución anterior y avisa por Telegram SOLO si algo
cambia (una estrategia pasa a superar las pruebas o deja de superarlas, o cambia de «no evaluable» a evaluable).

No aprueba nada ni toca la configuración: el laboratorio solo añade evidencia. Lo pone en marcha el Programador
de tareas de Windows (herramientas\\activar-laboratorio-semanal.bat) o se puede lanzar a mano:

    python laboratorio_semanal.py               # con el bot funcionando a la vez (pocas peticiones por minuto)
    python laboratorio_semanal.py --sin-aviso   # sin Telegram

Deja:
  registro/laboratorio_semanal.jsonl        una línea por ejecución (cuándo, cuánto tardó, qué cambió)
  registro/laboratorio_semanal_<fecha>.log  la salida completa del laboratorio
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA_ELEGIDA = os.path.join(AQUI, "estrategia_elegida.json")
DIR_REGISTRO = os.path.join(AQUI, "registro")
RUTA_BLOQUEO = os.path.join(AQUI, "laboratorio_semanal.lock")
BLOQUEO_MAX_SEG = 6 * 3600


def estados(ruta=RUTA_ELEGIDA):
    """{estrategia: 'supera' | 'no_supera' | 'no_evaluable'} del último laboratorio."""
    try:
        with open(ruta, encoding="utf-8") as fh:
            res = json.load(fh).get("resultados", {})
    except (OSError, ValueError):
        return {}
    return {k: r.get("estado_evaluacion") or ("supera" if r.get("supera") else "no_supera") for k, r in res.items()}


def cambios(antes, despues):
    """Lista de cambios relevantes entre dos ejecuciones (texto en español)."""
    out = []
    for k in sorted(set(antes) | set(despues)):
        a, d = antes.get(k), despues.get(k)
        if a == d:
            continue
        if d == "supera":
            out.append(f"✅ {k}: ahora SUPERA las pruebas del laboratorio (antes: {a or 'sin evaluar'})")
        elif a == "supera":
            out.append(f"⚠️ {k}: ya NO supera las pruebas (ahora: {d or 'sin evaluar'})")
        elif a is not None or d != "no_supera":
            out.append(f"ℹ️ {k}: {a or 'sin evaluar'} → {d or 'sin evaluar'}")
    return out


def avisar(texto):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(AQUI, ".env"))
    except ImportError:
        pass
    from notificaciones import Telegram
    tg = Telegram(os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
    if not tg.activo:
        return False
    try:
        tg._llamar("sendMessage", chat_id=tg.chat_id, text=texto[:4000], disable_web_page_preview=True)
        return True
    except Exception:  # noqa: BLE001  (sin aviso no pasa nada: queda en el registro)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Laboratorio semanal con aviso solo si cambia algo")
    ap.add_argument("--sin-aviso", action="store_true")
    ap.add_argument("--presupuesto", type=int, default=300, help="peso de peticiones por minuto para el laboratorio")
    args = ap.parse_args(argv)
    os.makedirs(DIR_REGISTRO, exist_ok=True)
    # Evita dos ejecuciones a la vez (p. ej. si el PC estaba apagado y Windows recupera la tarea).
    try:
        if time.time() - os.path.getmtime(RUTA_BLOQUEO) < BLOQUEO_MAX_SEG:
            print("Ya hay un laboratorio semanal en marcha.")
            return 0
    except OSError:
        pass
    with open(RUTA_BLOQUEO, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    inicio = time.time()
    marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ruta_log = os.path.join(DIR_REGISTRO, f"laboratorio_semanal_{marca}.log")
    antes = estados()
    try:
        with open(ruta_log, "w", encoding="utf-8") as log:
            proc = subprocess.run([sys.executable, os.path.join(AQUI, "laboratorio.py"), "--guardar-memoria",
                                   "--presupuesto", str(args.presupuesto)],
                                  cwd=AQUI, stdout=log, stderr=subprocess.STDOUT, timeout=BLOQUEO_MAX_SEG,
                                  env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        codigo = proc.returncode
    except subprocess.TimeoutExpired:
        codigo = -1
    finally:
        try:
            os.remove(RUTA_BLOQUEO)
        except OSError:
            pass
    despues = estados() if codigo == 0 else antes
    lista = cambios(antes, despues) if codigo == 0 else []
    fila = {"ts": marca, "duracion_min": round((time.time() - inicio) / 60, 1), "codigo": codigo,
            "log": os.path.basename(ruta_log), "cambios": lista,
            "superan": sorted(k for k, v in despues.items() if v == "supera")}
    with open(os.path.join(DIR_REGISTRO, "laboratorio_semanal.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fila, ensure_ascii=False) + "\n")
    if codigo != 0:
        texto = f"🧪 El laboratorio semanal ha fallado (código {codigo}). Mira {os.path.basename(ruta_log)} en la carpeta registro."
    elif lista:
        texto = ("🧪 Laboratorio semanal: hay cambios.\n" + "\n".join(lista) +
                 "\n\nNada se activa solo: superar el laboratorio es evidencia, no permiso. Revísalo en el panel → Estrategias.")
    else:
        texto = None
    print(texto or "Laboratorio semanal terminado: sin cambios (no se envía aviso).")
    if texto and not args.sin_aviso:
        avisar(texto)
    return 0 if codigo == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
