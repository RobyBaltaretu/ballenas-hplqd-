"""
Archivo propio de datos de mercado (para que el laboratorio tenga más historia con el tiempo).

Hyperliquid solo devuelve las últimas 5000 velas de cada intervalo (unos 7 meses de velas de 1 h) y no da
histórico del libro de órdenes. El bot, mientras funciona, guarda:

  velas/<intervalo>/<MONEDA>.jsonl   velas CERRADAS que el motor ya ha descargado (no añade peticiones)
  libro_<AAAA-MM-DD>.jsonl           cada N minutos, los mejores niveles del libro de cada moneda vigilada

El interés abierto, el precio de marca y el funding por minuto ya los graba mercado.py (datos_mercado/).
Todo se escribe por anexado (una línea por dato): un corte de luz como mucho estropea la última línea,
que al leer se ignora. Nada de esto opera ni influye en las decisiones: solo se guarda.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

MIN_DIAS_LIBRO = 14


def _dia(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _leer_jsonl(ruta):
    out = []
    try:
        with open(ruta, encoding="utf-8", errors="replace") as fh:
            for linea in fh:
                try:
                    out.append(json.loads(linea))
                except ValueError:
                    continue      # línea cortada (corte de luz): se ignora
    except OSError:
        pass
    return out


def leer_velas(carpeta, moneda, intervalo):
    """Velas archivadas de una moneda, ordenadas y sin repetir."""
    filas = _leer_jsonl(os.path.join(carpeta, "velas", intervalo, f"{moneda}.jsonl"))
    unicas = {}
    for v in filas:
        if isinstance(v, dict) and "t" in v:
            unicas[int(v["t"])] = v
    return [unicas[t] for t in sorted(unicas)]


def combinar(velas_api, velas_archivo):
    """Une las velas descargadas con las archivadas. Si una vela está en las dos, manda la descargada."""
    por_t = {int(v["t"]): v for v in velas_archivo}
    por_t.update({int(v["t"]): v for v in velas_api})
    return [por_t[t] for t in sorted(por_t)]


def leer_libro(carpeta, desde=None):
    """Fotos del libro archivadas: [{"t", "m", "b": [[px, sz], ...], "a": [[px, sz], ...]}] en orden."""
    try:
        nombres = sorted(f for f in os.listdir(carpeta) if f.startswith("libro_") and f.endswith(".jsonl"))
    except OSError:
        return []
    out = []
    for f in nombres:
        if desde and f[6:16] < _dia(desde):
            continue
        out += [x for x in _leer_jsonl(os.path.join(carpeta, f)) if isinstance(x, dict) and "t" in x]
    return out


def desequilibrio(foto, niveles=5):
    """(volumen comprador − vendedor) / total en los primeros 'niveles' niveles, en USD. Entre −1 y 1."""
    b = sum(px * sz for px, sz in foto.get("b", [])[:niveles])
    a = sum(px * sz for px, sz in foto.get("a", [])[:niveles])
    return (b - a) / (b + a) if b + a > 0 else None


class Archivo:
    def __init__(self, carpeta, cfg, log=None):
        self.carpeta, self.cfg, self.log = carpeta, cfg, log
        os.makedirs(carpeta, exist_ok=True)
        self.ultima_vela = {}          # (moneda, intervalo) -> t (ms) de la última vela archivada
        self.ultimo_libro = 0.0
        self.hilo = None
        self.bloqueo = threading.Lock()
        self.fotos_libro = 0
        self.velas_guardadas = 0
        self.errores = 0

    # ------------------------------------------------------------ velas (sin peticiones extra)
    def _ultima(self, moneda, iv):
        clave = (moneda, iv)
        if clave not in self.ultima_vela:
            velas = leer_velas(self.carpeta, moneda, iv)
            self.ultima_vela[clave] = int(velas[-1]["t"]) if velas else -1
        return self.ultima_vela[clave]

    def guardar_velas(self, moneda, iv, velas_cerradas):
        """velas_cerradas: velas en el formato de Hyperliquid ({"t", "o", "h", "l", "c", "v", ...}), ya cerradas."""
        if not self.cfg.get("velas", True) or not velas_cerradas:
            return 0
        ultima = self._ultima(moneda, iv)
        nuevas = [v for v in velas_cerradas if int(v["t"]) > ultima]
        if not nuevas:
            return 0
        ruta = os.path.join(self.carpeta, "velas", iv)
        os.makedirs(ruta, exist_ok=True)
        try:
            with open(os.path.join(ruta, f"{moneda}.jsonl"), "a", encoding="utf-8") as fh:
                for v in nuevas:
                    fh.write(json.dumps({k: v.get(k) for k in ("t", "T", "o", "h", "l", "c", "v", "n")},
                                        separators=(",", ":")) + "\n")
        except OSError as e:
            self.errores += 1
            if self.log:
                self.log.warning("Archivo: no pude guardar velas de %s %s: %s", moneda, iv, e)
            return 0
        self.ultima_vela[(moneda, iv)] = int(nuevas[-1]["t"])
        self.velas_guardadas += len(nuevas)
        return len(nuevas)

    # ------------------------------------------------------------ libro (en segundo plano)
    def foto_libro(self, info, monedas, ahora=None, en_segundo_plano=True):
        """Cada 'libro_cada_min' minutos, guarda los mejores niveles del libro de cada moneda.
        Corre en un hilo aparte para no retrasar el ciclo; si el anterior sigue, no lanza otro."""
        if not self.cfg.get("libro", True) or not monedas:
            return False
        ahora = ahora or time.time()
        if ahora - self.ultimo_libro < self.cfg.get("libro_cada_min", 5) * 60:
            return False
        if self.hilo is not None and self.hilo.is_alive():
            return False
        self.ultimo_libro = ahora
        if en_segundo_plano:
            self.hilo = threading.Thread(target=self._fotos, args=(info, list(monedas), ahora), daemon=True)
            self.hilo.start()
        else:
            self._fotos(info, list(monedas), ahora)
        return True

    def _fotos(self, info, monedas, ahora):
        niveles = int(self.cfg.get("libro_niveles", 10))
        lineas = []
        for m in monedas:
            try:
                l2 = info.l2_snapshot(m)
                lv = l2["levels"]
                foto = {"t": int(l2.get("time") or ahora * 1000) / 1000, "m": m,
                        "b": [[float(x["px"]), float(x["sz"])] for x in lv[0][:niveles]],
                        "a": [[float(x["px"]), float(x["sz"])] for x in lv[1][:niveles]]}
                lineas.append(json.dumps(foto, separators=(",", ":")))
            except Exception:  # noqa: BLE001  (una moneda sin libro no detiene las demás)
                self.errores += 1
            time.sleep(0.2)
        if not lineas:
            return
        try:
            with self.bloqueo, open(os.path.join(self.carpeta, f"libro_{_dia(ahora)}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write("\n".join(lineas) + "\n")
            self.fotos_libro += len(lineas)
        except OSError as e:
            self.errores += 1
            if self.log:
                self.log.warning("Archivo: no pude guardar el libro: %s", e)
        self._limpiar(ahora)

    def _limpiar(self, ahora):
        limite = _dia(ahora - self.cfg.get("dias_guardar_libro", 365) * 86400)
        try:
            for f in os.listdir(self.carpeta):
                if f.startswith("libro_") and f.endswith(".jsonl") and f[6:16] < limite:
                    os.remove(os.path.join(self.carpeta, f))
        except OSError:
            pass

    # ------------------------------------------------------------ resumen
    def resumen(self):
        """Días acumulados (se recalcula como mucho cada 10 minutos: leer el disco en cada ciclo sería caro)."""
        cache = getattr(self, "_resumen", None)
        if cache and time.time() - cache[0] < 600:
            return dict(cache[1], fotos_libro_sesion=self.fotos_libro, velas_guardadas_sesion=self.velas_guardadas,
                        errores_archivo=self.errores)
        try:
            dias_libro = len({f[6:16] for f in os.listdir(self.carpeta) if f.startswith("libro_")})
        except OSError:
            dias_libro = 0
        dias_velas = 0
        ruta = os.path.join(self.carpeta, "velas", "1h")
        try:
            for f in os.listdir(ruta):
                try:
                    with open(os.path.join(ruta, f), encoding="utf-8") as fh:
                        primera = json.loads(fh.readline())
                except (OSError, ValueError):
                    continue
                ultima = self._ultima(f[:-6], "1h")
                if ultima >= 0:
                    dias_velas = max(dias_velas, int((ultima - int(primera["t"])) / 86_400_000))
        except OSError:
            pass
        datos = {"dias_libro": dias_libro, "min_dias_libro": MIN_DIAS_LIBRO, "dias_velas": dias_velas}
        self._resumen = (time.time(), datos)
        return dict(datos, fotos_libro_sesion=self.fotos_libro, velas_guardadas_sesion=self.velas_guardadas,
                    errores_archivo=self.errores)
