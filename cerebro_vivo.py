"""
El cerebro en vivo: ÚNICO que decide si una candidata se opera (en simulado y shadow; en real, además, sigue
haciendo falta la aprobación humana de siempre y los límites del piloto).

Garantías (cada una tiene su prueba):
  - Decide con el modelo que entrenó el laboratorio (cerebro_modelo.json), validado al cargarlo. Modelo ausente,
    corrupto o de otra versión de rasgos -> NO entra nadie (se abstiene) y lo dice.
  - Calcula los rasgos con la MISMA función que el laboratorio (mercado_unificado.rasgos) y solo con velas cerradas.
  - Datos de Binance/Bybit viejos o ausentes -> esos rasgos valen «sin dato» (nunca se inventan) y queda anotado.
  - Jamás salta la protección: límites, stops, bloqueos y piloto real se aplican después, igual que antes.
  - Cualquier error interno -> se abstiene (no entra) y se registra; el bot sigue funcionando.
"""

import json
import os
import time

import cerebro as cb
import mercado_unificado as mu

DEFECTOS = {"decide": True, "umbral_r": cb.UMBRAL_R, "max_antiguedad_externos_min": 20}


class CerebroVivo:
    def __init__(self, cfg, ruta, log=None):
        self.cfg = dict(DEFECTOS, **(cfg or {}))
        self.ruta, self.log = ruta, log
        self.modelos, self.version, self.huella, self.error = {}, None, None, None
        self._mtime = None
        self.contadores = {"entra": 0, "no_entra": 0, "abstiene": 0}
        self.ultimas = []
        self.recargar()

    # ------------------------------------------------------------ modelo
    def recargar(self):
        try:
            mt = os.path.getmtime(self.ruta)
        except OSError:
            self.modelos, self.error = {}, "no hay modelo del cerebro: pasa el laboratorio"
            return
        if mt == self._mtime:
            return
        self._mtime = mt
        try:
            with open(self.ruta, encoding="utf-8") as fh:
                doc = json.load(fh)
            modelos = {}
            for iv, d in (doc.get("modelos") or {}).items():
                if list(d.get("rasgos") or []) not in (list(mu.RASGOS), list(mu.RASGOS_HL)):
                    raise ValueError(f"el modelo de {iv} usa otros rasgos (versión antigua)")
                m = cb.Modelo.desde_dict(d)
                if m is None:
                    raise ValueError(f"el modelo de {iv} no supera la validación")
                modelos[iv] = (m, d)
            self.modelos, self.version, self.huella, self.error = modelos, doc.get("version"), doc.get("huella"), None
            if self.log:
                self.log.info("Cerebro cargado: %s (huella %s) · %s", self.version, self.huella, ", ".join(modelos))
        except Exception as e:  # noqa: BLE001
            self.modelos, self.error = {}, f"modelo del cerebro inválido: {e}"
            if self.log:
                self.log.error("Cerebro: %s. No entra ninguna operación hasta tener un modelo válido.", self.error)

    @property
    def decide(self):
        return bool(self.cfg["decide"])

    # ------------------------------------------------------------ decisión
    def decidir(self, cand, motor, ctx_externo=None, ahora=None):
        """{'abrir', 'prediccion', 'motivo', 'rasgos', 'version', 'faltan'} para una candidata del motor."""
        out = {"abrir": False, "prediccion": None, "version": self.version, "rasgos": None, "faltan": []}
        try:
            iv, moneda = cand.get("intervalo"), cand["moneda"]
            par = self.modelos.get(iv)
            if par is None:
                return self._fin(out, "abstiene", self.error or f"sin modelo del cerebro para {iv}: no entra")
            modelo, _ = par
            ctx = (motor.contextos.get((moneda, iv)) or (None, None))[1]
            if ctx is None or len(ctx) < 60:
                return self._fin(out, "abstiene", "sin velas suficientes para calcular los rasgos")
            fh = (motor.funding_hist.get(moneda) or (None, None, None))[1]
            velas_ext, regs_ext = (ctx_externo.series_cerebro(moneda, iv, self.cfg["max_antiguedad_externos_min"] * 60,
                                                             ahora) if ctx_externo else (None, None))
            ra = mu.rasgos(ctx, len(ctx) - 1, cand["lado"], fh,
                           mu.SerieExterna(velas_ext) if velas_ext else None,
                           mu.FundingHorario(regs_ext) if regs_ext else None)
            out["rasgos"] = ra
            out["faltan"] = [k for k, v in ra.items() if v is None]
            p = modelo.predecir(ra, cand.get("estrategia"))
            out["prediccion"] = round(p, 4)
            if p > self.cfg["umbral_r"]:
                out["abrir"] = True
                return self._fin(out, "entra", f"cerebro: resultado esperado {p:+.3f} R (> {self.cfg['umbral_r']} R)")
            return self._fin(out, "no_entra", f"cerebro: resultado esperado {p:+.3f} R (≤ {self.cfg['umbral_r']} R)")
        except Exception as e:  # noqa: BLE001
            return self._fin(out, "abstiene", f"error del cerebro ({type(e).__name__}): no entra")

    def _fin(self, out, tipo, motivo):
        out["motivo"] = motivo + (f" · sin dato: {', '.join(out['faltan'])}" if out.get("faltan") else "")
        self.contadores[tipo] += 1
        self.ultimas = (self.ultimas + [{"ts": time.time(), "tipo": tipo, "motivo": out["motivo"],
                                          "prediccion": out["prediccion"]}])[-20:]
        return out

    def resumen(self):
        return {"decide": self.decide, "version": self.version, "huella": self.huella, "error": self.error,
                "modelos": {iv: {"n": d.get("n"), "entrenado_hasta": d.get("entrenado_hasta"), "variante": d.get("variante"),
                                 "estado_laboratorio": d.get("estado_laboratorio"),
                                 "pesos": m.pesos()} for iv, (m, d) in self.modelos.items()},
                "contadores": dict(self.contadores), "ultimas": list(self.ultimas)}
