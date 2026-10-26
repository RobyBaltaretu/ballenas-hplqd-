"""
Flujo de las ballenas: cuánto están AUMENTANDO o REDUCIENDO sus posiciones en cada moneda.

Las cuentas grandes casi nunca abren una posición de golpe: la construyen (o la deshacen)
con cientos de órdenes durante horas o días. Por eso aquí no se mira "si abrió", sino el
cambio neto de su posición en una ventana de tiempo, en dólares:
    +150 000 $ en ETH en la última hora  -> están comprando ETH con fuerza
    -80 000 $                            -> están vendiendo (cerrando largos o abriendo cortos)

Las ballenas que el laboratorio ha visto que ANTICIPAN el precio ("predictivas") son las que cuentan
para la señal, el veto y el rasgo; si aún no hay puntuación, cuentan todas.

Se usa de tres formas (config "ballenas"):
  - rasgo:  el flujo a favor / en contra de cada operación entra en el aprendizaje, que mide
            con el tiempo si ayuda o no.
  - veto:   si las ballenas están cargando con fuerza EN CONTRA de una entrada, no se entra.
  - señal:  un flujo muy fuerte genera una entrada por sí mismo (módulo "ballenas").
            Los datos de 60 días no le dieron ventaja, así que por defecto solo opera en simulado
            hasta que el aprendizaje o el laboratorio demuestren lo contrario.

Funciona igual en vivo (fotos de las posiciones cada pocos segundos) y en el laboratorio
(a partir del historial de órdenes ejecutadas).
"""

import bisect
from collections import defaultdict, deque


def moneda_valida(m):
    """Solo perpetuos del mercado principal (no spot '@123' ni mercados de terceros 'xyz:...')."""
    return bool(m) and not m.startswith("@") and ":" not in m


class FlujoBallenas:
    def __init__(self, retener_seg=2 * 86400):
        self.retener = retener_seg
        self.eventos = defaultdict(deque)          # moneda -> deque[(t, ballena, delta_usd, px)]
        self.ultima_foto = {}                      # (ballena, moneda) -> tamaño con signo
        self._indice = None                        # para el laboratorio (búsqueda rápida)

    # ------------------------------------------------------------- alimentar
    def registrar(self, t, ballena, moneda, delta_usd, px):
        if not moneda_valida(moneda) or not delta_usd:
            return
        cola = self.eventos[moneda]
        cola.append((t, ballena, delta_usd, px))
        while cola and cola[0][0] < t - self.retener:
            cola.popleft()

    def foto(self, t, ballena, posiciones, precios):
        """En vivo: posiciones = {moneda: tamaño con signo}; precios = {moneda: precio}.
        Registra el cambio respecto a la foto anterior de esa ballena. Devuelve la lista de cambios
        [(moneda, delta_usd, precio)]: son INFERIDOS (diferencia entre dos fotos), no fills observados."""
        vistas, cambios = set(), []
        for m, tam in posiciones.items():
            vistas.add(m)
            clave = (ballena, m)
            if clave in self.ultima_foto:
                delta = tam - self.ultima_foto[clave]
                if delta and m in precios:
                    self.registrar(t, ballena, m, delta * precios[m], precios[m])
                    cambios.append((m, delta * precios[m], precios[m]))
            self.ultima_foto[clave] = tam
        for (b, m), tam in list(self.ultima_foto.items()):
            if b == ballena and m not in vistas:   # la ha cerrado del todo
                if tam and m in precios:
                    self.registrar(t, ballena, m, -tam * precios[m], precios[m])
                    cambios.append((m, -tam * precios[m], precios[m]))
                del self.ultima_foto[(b, m)]
        return cambios

    def desde_fills(self, ballena, fills):
        """Laboratorio: cada fill cambia la posición en (tamaño con signo) * precio."""
        for f in fills:
            m = f["coin"]
            if not moneda_valida(m):
                continue
            sz = float(f["sz"]) * (1 if f["side"] == "B" else -1)
            px = float(f["px"])
            self.eventos[m].append((f["time"] / 1000, ballena, sz * px, px))
        self._indice = None

    def preparar_indice(self):
        self._indice = {}
        for m, cola in self.eventos.items():
            lista = sorted(cola)
            self.eventos[m] = deque(lista)
            self._indice[m] = [e[0] for e in lista]

    # ------------------------------------------------------------- consultar
    def _ventana(self, moneda, t, ventana_seg):
        cola = self.eventos.get(moneda)
        if not cola:
            return []
        if self._indice is not None and moneda in self._indice:
            ts = self._indice[moneda]
            i, j = bisect.bisect_right(ts, t - ventana_seg), bisect.bisect_right(ts, t)
            return [cola[k] for k in range(i, j)]
        return [e for e in cola if t - ventana_seg < e[0] <= t]

    def por_ballena(self, moneda, t, ventana_seg, solo=None):
        """Cambio neto de cada ballena en la ventana. solo: si se da, únicamente esas ballenas."""
        neto = defaultdict(float)
        for _, b, d, _ in self._ventana(moneda, t, ventana_seg):
            if solo is None or b in solo:
                neto[b] += d
        return neto

    def flujo(self, moneda, lado, t, ventana_seg, solo=None):
        """Flujo neto en dólares A FAVOR de 'lado' (negativo = las ballenas van en contra)."""
        s = 1 if lado == "L" else -1
        return s * sum(self.por_ballena(moneda, t, ventana_seg, solo).values())

    def senal(self, moneda, t, cfg, solo=None):
        """Señal del módulo 'ballenas': aumento fuerte y mayoritario en un sentido."""
        ventana = cfg["ventana_min"] * 60
        por_b = self.por_ballena(moneda, t, ventana, solo)
        if not por_b:
            return None
        mejor = None
        for lado, s in (("L", 1), ("S", -1)):
            a_favor = {b: v * s for b, v in por_b.items() if v * s >= cfg["aumento_min_usd"]}
            total = sum(a_favor.values())
            contra = sum(-v * s for v in por_b.values() if v * s < 0)
            if a_favor and total >= cfg["flujo_min_usd"] and contra < total * cfg["contra_max_fraccion"]:
                if not mejor or total > mejor["total_usd"]:
                    eventos = [e for e in self._ventana(moneda, t, ventana) if e[1] in a_favor and e[2] * s > 0]
                    peso = sum(abs(e[2]) for e in eventos) or 1
                    px = sum(abs(e[2]) * e[3] for e in eventos) / peso
                    mejor = {"lado": lado, "total_usd": total, "ballenas": sorted(a_favor), "contra_usd": contra,
                             "px_medio": px}
        return mejor
