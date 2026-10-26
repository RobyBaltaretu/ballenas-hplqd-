# Informe de cambios: corrección y endurecimiento (v5 → v6)

Fecha: 22/10/2026. El bot queda en su **modo seguro existente (simulado)**. No se ha enviado ninguna orden real, no se ha usado ninguna clave privada y no se ha cambiado la configuración para operar en real. Todas las pruebas usan módulos falsos o la cuenta simulada.

Las líneas citadas son las de la versión entregada (v6). Las funciones nuevas o reescritas se citan por su primera línea.

---

## A. Órdenes: sobrellenados y estados ambiguos

| Cambio | Archivo:línea |
|---|---|
| Máquina de estados explícita de orden (`TRANSICIONES_ORDEN`). Un estado final confirmado no retrocede, y una respuesta repetida o atrasada no «reabre» una orden | `ejecucion.py:45`, `cambiar_estado` `:68` |
| Nuevo estado `no_confirmada`: si el exchange no conoce una orden tras varias consultas, ya **no** se da por rechazada; se congela el mercado y se concilia | `ejecucion.py:247` |
| `cancelar_confirmada`: el ACK no cuenta como cancelación; se re-consulta al exchange. Solo `confirmado=True` si el estado final es cancelada/completada/rechazada según el exchange | `ejecucion.py:296` |
| Las órdenes de protección/cierre sin `reduce_only` se bloquean antes de enviarse | `ejecucion.py:41` (`ROLES_REDUCCION`), comprobación en `enviar` |
| `id_fill`: identificador estable de fill (`tid`) para no contar dos veces comisiones ni PnL | `ejecucion.py:84`; uso en `gestor._registrar_fills` `gestor.py:410` |
| **Entrada:** la IOC de reserva solo se envía si la orden límite tiene un estado final **confirmado**. Si sigue abierta, parcial o desconocida, no se envía nada más: se congela el mercado, se protege el tamaño real y se concilia | `gestor.abrir` `gestor.py:592`, `_entrada_incierta` `:692` |
| El tamaño ejecutado sale de la **posición real** y de los **fills** (que deben coincidir), sin contar dos veces un parcial; la IOC completa solo lo que falta | `_ejecutado_confirmado` `gestor.py:670` |
| Se protege con stop el tamaño **real**, aunque difiera del plan; el riesgo se recalcula con el fill real | `_despues_de_entrada` `gestor.py:702`; `riesgo.riesgo_tras_fill` `riesgo.py:129` |
| **Stops:** antes de poner o reintentar un stop se mira la cuenta por cloid/oid; si ya existe uno válido, se adopta sin reenviar. Los duplicados se cancelan (verificado) y se conserva el registrado | `asegurar_stop` `gestor.py:523`, `_limpiar_sobrantes` `:576`, `stops_extra` en `conciliar` `:187` |
| Una entrada incierta que acaba llenándose se adopta solo si la explican fills de la propia entrada; si no, se trata como divergencia y se congela | `_adoptar_tamano_de_entrada` `gestor.py:385`, `_resolver_entradas` `:343` |
| Tras un cierre se verifica en la cuenta que no quedan órdenes del bot; si alguna no se puede cancelar, se congela y queda como incidente. Un cierre repetido no duplica PnL ni comisiones | `procesar_cierre` `gestor.py:863` |
| **Una sola instancia:** bloqueo del sistema operativo (fcntl/msvcrt) en la carpeta y en un bloqueo común por cuenta. Lo libera el sistema operativo si el proceso muere, así que nunca queda «pegado» | `instancia.py` (`_Bloqueo` `:32`, `BloqueoInstancia` `:87`); uso en `bot.py:1613` |
| **Dead man's switch (`scheduleCancel`): NO se activa.** Según la documentación cancela «todas las órdenes abiertas», sin decir si excluye los stops; dejaría posiciones sin protección justo cuando el bot no responde. Queda documentado | `LEEME.md` (Seguridad) |

## B. Promoción: huella completa y evidencia suficiente

| Cambio | Archivo:línea |
|---|---|
| La huella cubre **todo** el código que decide (`ARCHIVOS_CODIGO`) y la configuración efectiva completa salvo lo que no decide (`CONFIG_SIN_EFECTO`). También incluye las versiones de esquema de configuración, ledger y modelo de simulación, y los propios requisitos | `promocion.py:50`, `:58`, `huella` `:112` |
| Toda `.py` nueva debe clasificarse (una prueba lo exige). Si falta un archivo o la configuración es rara, la huella falla **cerrada** (`ErrorHuella`, `huella_segura` → None = no autorizado) | `hash_codigo` `promocion.py:90`, `huella_segura` `:131` |
| Sin secretos en la huella (`sin_secretos`) | `config_decisiva` |
| Mínimos explícitos para `apta_revision`: laboratorio superado de ≤ 30 días con holdout no reutilizado; ≥ 30 operaciones shadow/paper; ≥ 14 días de observación; última evidencia de < 7 días; media y PnL netos > 0; caída ≤ 10 R; coste medio ≤ 0,35 R | `REQUISITOS_APTA` `promocion.py:63`, `requisitos_apta` `:263` |
| Evidencia sin resultado, con errores de conciliación, con órdenes sin confirmar, de otra huella o vieja: **no cuenta** | `evidencia_valida` `promocion.py:188` |
| La CLI explica qué falta (`python promocion.py requisitos <estrategia>`). La aprobación sigue siendo solo humana, desde la línea de comandos | `aprobar` `promocion.py:329` |

## C. Laboratorio: falsos positivos

| Cambio | Archivo:línea |
|---|---|
| t de **Student, una cola**, con n−1 grados de libertad y corrección de **Bonferroni** por el total de variantes (implementación propia de la beta incompleta, comprobada con valores de tabla) | `validacion.py:120` (`t_critico`), `:137` (`t_requerido`) |
| Línea base vacía o insuficiente → `no_evaluable`, nunca aprueba (`base_comparable`) | `validacion.py:307`, `veredicto` `:351` |
| **Purga:** se descarta toda operación que cruza un límite de partición; embargo aplicado igual en laboratorio e investigación | `en_tramo_purgado` `validacion.py:62`; `laboratorio.py:329`, `:334`; `investigacion.py:275`, `:296` |
| **Holdout de un solo uso** por experimento (`RegistroHoldout`). Si ya se consultó, el resultado se degrada a `exploratorio` y no vale para promoción | `validacion.py:452`; `laboratorio.py:921`; `investigacion.examinar` `:258` |
| **Variantes efectivas acumuladas desde siempre** (laboratorio e investigación comparten registro, incluidas repeticiones manuales), con semilla, datos, universo, código y configuración registrados | `RegistroVariantes` `validacion.py:419`; `laboratorio.py:914`; `investigacion.py:384` |
| Muestra insuficiente, falta de datos o moneda no disponible → `no_evaluable` | `veredicto` |
| Métricas más allá del % de acierto: neto, caída, estabilidad por trimestre (`:212`), exposición largo/corto (`:220`), desglose por moneda/régimen, escenarios de costes reproducibles (`escenarios_estres` `:170`: taker, deslizamiento, hueco de stop y funding adverso) | `validacion.py` |
| Las líneas base (aleatoria y «comprar y mantener») se generan con los mismos datos, horizonte, costes y salidas, y dentro de la misma partición | `linea_base_aleatoria`, `linea_base_largo` |
| Supervivencia: documentado que la reconstrucción del universo histórico **no es completa** | `LEEME.md` (Limitaciones) |

## D. Simulación y riesgo

| Cambio | Archivo:línea |
|---|---|
| Papel (`MODELO = 2`): la IOC recorre los niveles de una foto real del libro (parcial si no hay profundidad); la límite solo se llena si el precio la **atraviesa** (maker); el stop se **activa con la marca** y se ejecuta contra el libro, peor que el trigger; funding con nocional al precio del **oráculo**; liquidación **estimada** | `papel.py:14`, `:40`, `_liquidacion` `:138`, `_funding` `:195` |
| Laboratorio: deslizamiento variable (mínimo + parte del ATR), impacto adverso en salidas a mercado; comisiones maker/taker según el tipo de fill | `laboratorio.py` (`simular_entrada`) |
| Comisiones: tarifa manual mostrada como **estimación** (nivel 0: 0,015 % maker / 0,045 % taker); `comisiones.fuente = "cuenta"` lee `userFees` | `bot.py:1577` |
| Impacto en el libro antes de entrar (`impacto_pct`) y exposición ponderada por **beta a BTC** que incluye posiciones externas y lados opuestos | `riesgo.py:138`, `:173`, `comprobar_agregados` `:187` |
| **Liquidación real vigilada** cada ciclo (`liquidationPx`, mantenimiento, margen usado, equity). Si la liquidación llega antes que el stop → cierre reduce-only. Si está demasiado cerca, si es desconocida en real o si el mantenimiento es alto → se bloquean entradas | `vigilar_liquidacion` `gestor.py:290` |
| Precio de referencia explícito en cada stop/trailing (`precio_referencia`: mark/mid); el panel muestra la marca, el mid, el stop, la liquidación, sus distancias y su antigüedad | `gestor.py:505`; `panel.html` (posiciones) |
| Tabla «Simulación frente a ejecución real» con todas las diferencias que quedan | `LEEME.md` |

## E. «Señales en vivo»

| Cambio | Archivo:línea |
|---|---|
| `feed.py`: flujo **persistente** append-only (`registro/feed.jsonl`) con `seq` creciente, `event_id` estable, `ts_exchange`/`ts_recepcion`/`ts_proceso`, precio de referencia con su tipo, frescura y estado. Lo desconocido se guarda como `None` y se muestra «desconocido». Rotación por tamaño y retención por días | `feed.py:102` (`Feed`), `publicar` `:145`, `desde_ledger` `:197` |
| Eventos separados: señal, decisión aprobada/rechazada (con cada motivo), cada estado de orden, protección, entrada/salida/cierre, fills **observados** de ballenas, cambios **inferidos** de ballenas, operación grande pública, liquidación **inferida**, datos obsoletos, reconexión, fuente perdida/recuperada | `feed.py` (`TIPOS`); `bot.py:286`, `:291`, `:321`, `:430`, `:472`, `:528`, `:725` |
| API incremental con cursor (`/api/feed?desde=`) y **SSE** (`/api/feed/stream`, con límite de conexiones). Si SSE falla, polling incremental | `panel.py:584`–`:623` |
| Pestaña con filtros (tipo, moneda, estrategia, lado, estado, texto), pausa, marca de histórico/nuevo, enlace a la operación, desconexión/reconexión y antigüedad visibles | `panel.html` |
| Ballenas: cobertura (elegidas, suscritas en tiempo real hasta el **límite de 10 usuarios por IP**, frescura por cuenta, fallos), fills observados por separado de los cambios inferidos y del flujo de la última hora. Las direcciones se enmascaran | `bot.py:912`; `panel.html` |
| Velas: cuenta atrás al próximo cierre, última vela cerrada y hora de evaluación; las estrategias solo señalan al **cierre** de la vela | `motor.py` (`estado_velas`); `panel.html` |
| (Revisión de hoy) Una orden en el libro ya no muestra «tamaño 0»: el tamaño solo aparece si hubo ejecución; lo ejecutado va en los datos del evento | `feed.py:226` |

## F. Observabilidad, seguridad y despliegue

| Cambio | Archivo:línea |
|---|---|
| Estado global con componentes **obligatorios**: `sin_datos` en uno obligatorio da `desconocido`, nunca «correcto»; se distinguen `degradado` y `fallido` | `observabilidad.py:16`, `:37`, `:249` |
| Latido propio del panel (supervisado aparte del bot), registro de accesos y errores sin credenciales | `panel.py` |
| El panel solo escucha en 127.0.0.1 salvo que se ponga `panel.permitir_http_red = true` (se recomienda el túnel cifrado). Se mantienen Origin, token interno, solo lectura remota y comandos remotos desactivados por defecto | `panel.py:657`, `:773` |
| Ningún paquete lleva `.env`: hay un paquete de actualización solo con código (`preparar-actualizacion.bat`) y las guías explican cómo poner los secretos en el destino | `despliegue/preparar_actualizacion.ps1`, `GUIA_VPS.md`, `GUIA_ORACLE.md` |
| `comprobar.py`: configuración, una sola instancia, permisos, copias, salud y servicios. **(Revisión de hoy)** la salud desconocida se marca «?» y el resumen ya no dice «TODO CORRECTO» si hay algo sin confirmar | `comprobar.py:28`, `:115`, resumen al final |
| Enmascarado de direcciones y secretos en logs, CSV, diagnóstico, API y feed | `observabilidad.py`, `feed.py`, `panel.py` |

## H. Vigilancia y estudios comparativos

| Cambio | Archivo:línea |
|---|---|
| La normal unilateral se sustituye por un **bootstrap por bloques** en orden temporal (respeta la dependencia), con mínimo de 30 operaciones, incertidumbre visible (cota) y corrección por el número de estrategias vigiladas | `vigilancia.py:34`, `evaluar` `:50` |
| **Recuperación gradual con persistencia:** degradada ×0,5 → recuperación ×0,5 → ×0,75 → normal, con 7 días seguidos en cada paso; si recae, se reinicia la cuenta. Nunca supera ×1 ni devuelve una autorización retirada | `fases` `vigilancia.py:90`; `bot.vigilar_estrategias` `bot.py:992` |
| `estudios.py`: aportación marginal, solapamiento de señales, correlación con BTC y entre estrategias, caída y resultado por trimestre y régimen (fuerza relativa y momentum, y reversión por régimen). Es solo informe | `estudios.py:52`, `:77`, `:92`; `laboratorio.py:1023`–`:1039` |
| Ballenas y liquidaciones siguen en **investigación** (`ballenas.modo_senal = "investigacion"`). Nada se activa por estar en el código o en el laboratorio | `config.json` |

---

## Pruebas ejecutadas (22/10/2026, en este entorno)

- **Suite completa:** `python -m unittest discover -s tests -t .` → **218 pruebas, OK**. Incluye las regresiones G1–G10:

| G | Prueba(s) |
|---|---|
| 1 | `test_ordenes_seguras`: `test_entrada_que_sigue_abierta_tras_cancelar_no_recibe_ioc`, `test_parcial_o_desconocida_tras_cancelar_no_recibe_ioc` |
| 2 | `test_stop_con_respuesta_ambigua_no_se_duplica`, `test_ioc_ambigua_no_duplica_la_entrada`, `test_stops_duplicados_se_reducen_a_uno` |
| 3 | `test_entrada_incierta_que_luego_se_llena_queda_protegida_con_el_tamano_real`, `test_cancelacion_confirmada_completa_solo_lo_que_falta` |
| 4 | `test_validacion_promocion`: `test_cada_archivo_de_codigo_cambia_la_huella`, `test_cada_limite_de_riesgo_y_orden_cambia_la_huella`, `test_falta_un_archivo_falla_cerrado`, `test_todo_archivo_py_esta_clasificado` |
| 5 | `test_laboratorio_validez`: `test_linea_base_vacia_o_insuficiente_no_aprueba`, `test_muestra_insuficiente_no_evaluable`, `test_holdout_reutilizado_no_supera`, `test_holdout_de_un_solo_uso` |
| 6 | `test_operacion_que_cruza_la_particion_se_purga`, `test_walk_forward_no_cuenta_operaciones_que_entran_en_el_holdout` |
| 7 | `test_simulacion_riesgo`: IOC por el libro, límite solo si se atraviesa (maker), stop por marca y peor que el trigger, funding con oráculo, escenarios peores |
| 8 | `test_salud_global` (4 pruebas) y `test_comprobar` |
| 9 | `test_feed`: `test_reconexion_sin_perder_ni_duplicar`, `test_lector_incremental_sin_perdidas_ni_duplicados_con_rotacion`, `test_api_incremental_y_obsoleto` |
| 10 | `test_secretos` (API, CSV, diagnóstico y logs) y `test_feed.test_direcciones_y_secretos_enmascarados` |

- `ruff check . --select F,E9` → sin errores. `shellcheck -S warning despliegue/*.sh` → sin avisos. Sintaxis JS del panel (`node --check`) correcta. Los 6 scripts `.ps1` se analizan sin errores con PowerShell 7.
- **Laboratorio completo offline** con una API falsa (velas sintéticas): termina sin errores y ningún módulo supera. Los resultados no significan nada; solo prueban que el flujo funciona.
- **Investigación offline:** 40 ideas sintéticas, todas al cementerio.
- **Panel** con datos de demostración y Chromium (escritorio y móvil de 390 px): sin errores de JavaScript ni desbordamiento horizontal.
- La configuración real del PC (`config.json` v2) valida con el código nuevo y sigue en `simulado`.

**No ejecutado:** nada contra Hyperliquid (ni REST ni websocket reales); ni el bot ni el panel en Windows; ni systemd en un servidor real; ni el laboratorio con datos reales (aquí no hay acceso a Hyperliquid).

## Riesgos cerrados

- IOC de reserva con la límite aún viva (sobrellenado).
- Reenvío ciego tras una respuesta ambigua.
- Stops duplicados o de tamaño distinto al real.
- Contabilidad doble de fills.
- Dos instancias en el mismo equipo.
- Aprobaciones que sobreviven a cambios de código o límites.
- Una sola operación paper como evidencia.
- Aprobación con línea base vacía o con holdout reutilizado.
- Fugas entre particiones.
- `sin_datos` presentado como sano.
- Cambios inferidos de ballenas presentados como fills.
- Recuperación de tamaño de golpe.

## Riesgos pendientes

1. Nada está probado contra la **API real**: formatos de respuesta, `userRole`, `orderStatus` con cloid, `userFills` por cloid y la latencia de aparición de órdenes. El primer uso real debe ser con el piloto mínimo y vigilado.
2. **Dos equipos** con la misma cuenta: el bloqueo es por equipo. Solo queda la detección de CLOID desconocido y la regla operativa de no arrancar los dos a la vez.
3. **Ventana de stop tras una entrada incierta:** si la orden de entrada sigue viva, la posición puede crecer entre ciclos; el stop se rehace con el tamaño real en la siguiente conciliación (unos 15 s).
4. Simulación: cola de órdenes, reacción del libro, liquidación y ADL no se reproducen; el libro histórico no existe en el laboratorio (el deslizamiento es un supuesto).
5. Universo histórico incompleto (monedas retiradas): queda sesgo de supervivencia.
6. Beta a BTC y correlación son aproximaciones; no hay datos de sector/factor.
7. `scheduleCancel` sin usar: si el bot se cae, las órdenes quedan vivas (los stops protegen, pero también siguen vivas las órdenes de entrada pendientes).

## Decisiones que requieren calibración o revisión humana

- `REQUISITOS_APTA` (30 operaciones, 14 días, caída ≤ 10 R, coste ≤ 0,35 R): son conservadores pero arbitrarios. Cambiarlos invalida aprobaciones (entra en la huella).
- Comisiones: confirmar con `userFees` de la cuenta antes de cualquier piloto (`comisiones.fuente = "cuenta"`).
- `distancia_liquidacion_min_multiplo_stop` (1,5), `mantenimiento_max_fraccion_equity` (0,5), `impacto_max_pct` (0,15) y `exposicion_beta_btc_max_pct` (200): calibrar con datos reales de la cuenta.
- Deslizamiento del laboratorio y del papel: compararlo con los fills reales del piloto y ajustarlo.
- Vigilancia: `dias_persistencia` (7) y mínimo de 30 operaciones.
- Habilitar o no `scheduleCancel`: solo si se confirma que no cancela los stops, o con stops re-colocados por un proceso aparte.
- Paso de cualquier estrategia a `apta_revision`/`real_habilitado`: siempre persona, siempre CLI.

**Ninguna estrategia está validada y nada de esto indica rentabilidad.**

## Fuentes consultadas (documentación oficial de Hyperliquid)

- [Take profit and stop loss orders (TP/SL)](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl): el disparo es por precio de marca; las TP/SL de mercado tienen un 10 % de tolerancia de deslizamiento.
- [Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint): `scheduleCancel` («cancela todas las órdenes abiertas», como máximo 10 al día), `cancelByCloid`, ALO/IOC/GTC y campos de las órdenes trigger.
- [Fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees): nivel 0 con 0,045 % taker y 0,015 % maker; volumen de 14 días; descuentos por staking. No está confirmado si `userFees` ya incluye el staking.
- [Liquidations](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations): por marca y margen de mantenimiento (la mitad del inicial al apalancamiento máximo); fórmula del precio de liquidación.
- [Funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding): pago horario, límite del 4 %/h, nocional al precio del oráculo.
- [WebSocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions): `userFills` con `isSnapshot`, `trades`, `l2Book`.
- [Rate limits and user limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits): 10 usuarios únicos por IP en el websocket; 1200 de peso por minuto en REST.

**No confirmado en la documentación:** si `scheduleCancel` excluye las órdenes trigger, el significado exacto de `normalTpsl`/`positionTpsl` y si se pueden suscribir los `userFills` de cuentas ajenas (el código lo trata como posible y lo etiqueta como «observado»).
