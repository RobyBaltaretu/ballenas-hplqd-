"""G8: un componente obligatorio sin datos nunca deja el sistema como 'correcto'."""

from observabilidad import Sistema
from tests.comunes import ConCarpeta


class EstadoGlobal(ConCarpeta):
    def sistema(self):
        s = Sistema(self.dir, obligatorios=("ciclo", "api_rest", "datos_mercado", "ejecucion"))
        s.ciclo(0.1, True)
        return s

    def test_obligatorio_sin_datos_es_desconocido(self):
        s = self.sistema()
        for k in ("ciclo", "api_rest", "datos_mercado"):
            s.marcar(k, "correcto")
        f = s.foto()
        self.assertEqual(f["estado_global"], "desconocido")
        self.assertIn("ejecucion", f["obligatorios_sin_datos"])

    def test_todos_bien_es_correcto_y_fallo_es_fallido(self):
        s = self.sistema()
        for k in ("ciclo", "api_rest", "datos_mercado", "ejecucion"):
            s.marcar(k, "correcto")
        self.assertEqual(s.foto()["estado_global"], "correcto")
        s.marcar("ejecucion", "fallido")
        self.assertEqual(s.foto()["estado_global"], "fallido")

    def test_opcional_fallido_degrada_pero_no_es_fallido(self):
        s = self.sistema()
        for k in ("ciclo", "api_rest", "datos_mercado", "ejecucion"):
            s.marcar(k, "correcto")
        s.marcar("ballenas", "fallido")
        self.assertEqual(s.foto()["estado_global"], "degradado")

    def test_sin_latido_no_es_correcto(self):
        s = Sistema(self.dir, obligatorios=("ciclo",))
        s.marcar("ciclo", "correcto")
        self.assertEqual(s.foto()["estado_global"], "desconocido")
