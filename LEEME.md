# Bot de trading para Hyperliquid

Vigila las monedas más líquidas de Hyperliquid con su **propio motor de señales** (ruptura, retroceso en tendencia, reversión, funding extremo y fuerza relativa, en velas de 1 h y 4 h). Un **laboratorio** prueba esas estrategias con datos históricos y deja **evidencia**; ninguna pasa a dinero real sin la **aprobación explícita de una persona**.

También sigue a las **ballenas** (cuentas grandes) y graba el interés abierto y las operaciones grandes de todo el mercado. Las dos cosas son **funciones de investigación**: se registran y se miden, pero **no abren operaciones** mientras no se demuestre que sirven.

> **No hay ninguna estrategia que se pueda considerar rentable hoy.** En la última pasada del laboratorio (walk-forward con holdout), ninguna superó los criterios. Por eso el bot sigue en **simulado** o **shadow**, acumulando datos.

## Lo esencial en 1 minuto

| Modo | Qué hace | Necesita |
|---|---|---|
| **simulado** (o `paper`) | Opera con dinero **ficticio** (232 $ ≈ 200 €, `saldo_simulado_usd`) usando precios reales. Es el modo por defecto | Nada |
| **shadow** | Solo observa y apunta lo que **habría hecho**. **No puede enviar, cancelar ni modificar órdenes**: ni siquiera crea el cliente de órdenes | Nada |
| **real** | Dinero real, **solo en una subcuenta dedicada**, con estrategias aprobadas y los límites del piloto | Ver «Pasar a real» |

Todo lo que hace el bot queda en un **registro versionado** (`registro/ledger.jsonl`): cada señal, decisión (con sus motivos), orden (con su CLOID), fill, stop, movimiento del stop, funding y cierre. El panel reconstruye cada operación solo a partir de ese registro.

## En Windows: un solo clic

Descomprime el zip donde quieras, por ejemplo en `Documentos\bot-ballenas`, y haz doble clic en **`INICIAR.bat`**. El script hace todo lo que falte, y solo lo que falte:

1. Si no tienes Python, lo instala. No pide permisos de administrador.
2. Prepara el bot e instala lo que necesita. La primera vez tarda un par de minutos; las siguientes, segundos.
3. Crea un acceso directo **"Bot de ballenas"** en el escritorio.
4. Si aún no has pasado el **laboratorio**, te pregunta si quieres hacerlo. Prueba las estrategias con datos reales y deja **evidencia**; no activa nada en real.
5. Comprueba (y si hace falta migra, con copia de seguridad) `config.json`. Si no es válido, **no arranca** y te dice por qué.
6. Abre el **panel** en el navegador y arranca el **bot** en esa ventana. **No la cierres.** Para parar el bot, ciérrala o pulsa Ctrl + C.

A partir de ahí, basta con el acceso directo del escritorio. Si el bot se cierra por un fallo, `INICIAR` lo **reinicia solo** a los 30 segundos. No lo reinicia si lo paras tú, si la configuración no es válida o si se apaga por el suelo de emergencia (código de salida 3).

> Si Windows muestra "Windows protegió su PC", pulsa *Más información → Ejecutar de todas formas*. Aparece porque es un script descargado.

En la carpeta `herramientas` hay otros accesos de doble clic:

| Archivo | Qué hace |
|---|---|
| `laboratorio.bat` | Pasa el laboratorio de estrategias (repítelo cada 2–4 semanas). Puede hacerse con el bot en marcha: el bot recoge el resultado solo |
| `abrir-panel.bat` | Abre solo el panel |
| `ver-informe.bat` | Resumen de lo aprendido |
| `telegram-ver-chat-id.bat` | Muestra tu chat_id al configurar Telegram |
| `configurar-energia.bat` | Ajusta el PC para que, enchufado, no se suspenda nunca, tampoco al cerrar la tapa |
| `conectar-cloudflare.bat` | Conecta el PC a Cloudflare Tunnel (ver más abajo) |
| `activar-arranque-automatico.bat` | El bot arranca solo al encender el PC e iniciar sesión |
| `desactivar-arranque-automatico.bat` | Lo quita |

**Para editar `.env` o `config.json`:** clic derecho → *Abrir con* → **Bloc de notas**.

**Para que el PC no se duerma:** el bot lo impide solo mientras está en marcha. En un portátil, además:
- Déjalo **enchufado**.
- En las opciones de energía, pon **"Al cerrar la tapa: no hacer nada"** cuando está enchufado.
- En *Windows Update → Opciones avanzadas → Horas activas*, cubre las horas en que funciona el bot, para que no se reinicie solo.
- Deja activada **"Establecer la hora automáticamente"**: los límites diarios y semanales van en hora UTC.

## Ver el panel desde el móvil u otros dispositivos

Por defecto, el panel solo se ve en el propio PC. Hay tres formas de abrirlo desde fuera.

**A) Desde tu misma wifi** (la más sencilla)
1. En `.env`, pon una contraseña: `PANEL_PASSWORD=algo-largo-y-tuyo`.
2. En `config.json`, cambia `"host": "127.0.0.1"` por `"host": "0.0.0.0"`.
3. Reinicia el bot. Si Windows pregunta por el cortafuegos, marca **solo "Redes privadas"** y pulsa *Permitir*.
4. Desde el móvil, conectado a la misma wifi, abre `http://IP-DEL-PC:8080`. `herramientas\abrir-panel.bat` te muestra esa dirección exacta. Cualquier usuario sirve; la contraseña es la de `PANEL_PASSWORD`.

**B) Desde cualquier sitio, con Tailscale** (recomendado fuera de casa)
1. Haz los pasos 1–3 de la opción A.
2. Instala **Tailscale** (gratis para uso personal) en el PC y en el móvil, y entra con la misma cuenta en los dos.
3. Desde el móvil, estés donde estés, abre `http://NOMBRE-DEL-PC:8080`. El nombre aparece en la app de Tailscale.

El tráfico va cifrado y no hay que abrir nada en el router.

**C) Con Cloudflare Tunnel** (dirección propia, con inicio de sesión)

El panel tendrá una dirección como `https://bot.tudominio.com`. Para entrar, Cloudflare te pide tu email y te envía un código. Nadie más puede pasar.

En esta opción **no hace falta** cambiar `"host"` en `config.json`: el túnel conecta con el panel desde dentro del propio PC. Lo que sí necesitas es **un dominio gestionado en Cloudflare**.

*Paso 1 · En el panel de Cloudflare* (dash.cloudflare.com)
1. Entra en **Zero Trust**. La primera vez te pide un nombre de equipo y el plan: elige el **gratuito**. Puede pedirte una tarjeta aunque no cobre.
2. En **Get started**, elige **"Set up secure access to private apps from any browser"**. Este asistente crea a la vez el túnel y la protección.
3. Rellena:
   - **Aplicación:** nombre `Bot ballenas`, host `localhost`, protocolo `HTTP`, puerto `8080`.
   - **Dirección pública:** el subdominio (p. ej. `bot`) y tu dominio.
   - **Acceso:** **tu email** (solo los emails que pongas aquí podrán entrar).
   - **Túnel:** crea uno nuevo (p. ej. `pc-casa`), elige **Windows** y **copia el comando de instalación** que te muestra, sin cerrar esa pantalla.

*Paso 2 · En el PC*
1. Haz doble clic en `herramientas\conectar-cloudflare.bat`.
2. Pega el comando que copiaste y acepta el permiso de administrador.
3. El túnel queda instalado como servicio de Windows y arranca solo con el PC, aunque el bot esté parado.

*Paso 3 · Comprobarlo*
Vuelve a la pantalla de Cloudflare: el túnel debe aparecer como conectado. Termina el asistente y abre `https://bot.tudominio.com` desde el móvil con datos (sin wifi). Te pedirá tu email y un código.

Si más adelante quieres añadir o quitar emails, está en **Zero Trust → Access controls**. El túnel está en **Networking → Tunnels**.

> **No abras el puerto 8080 en el router.** Visto desde fuera de tu PC (wifi, Tailscale o Cloudflare), el panel es de **solo lectura** salvo que pongas `"comandos_remotos": true` en el bloque `panel` de `config.json`. Aun así, no lo expongas directamente a internet.

## Configuración (`config.json`)

Se edita con el Bloc de notas (clic derecho → *Abrir con*). Los bloques principales:

| Bloque | Qué controla |
|---|---|
| `modo` | `simulado`, `shadow` o `real`. **Obligatorio**: sin él, el bot no arranca |
| `riesgo` | Riesgo por operación (1 % por defecto), riesgo abierto máximo, máximo de posiciones (4), del mismo lado (3), operaciones al día (10; en tu PC, 12), apalancamiento máximo, exposición por mercado, bruta y neta, stop mínimo/máximo, duración máxima global (336 h = 14 días, para que quepan las estrategias de varias semanas), pérdida diaria/semanal y caída máximas (en **UTC**), suelo de emergencia, antigüedad máxima de los datos |
| `comisiones` | Maker/taker (`fuente: "cuenta"` las lee de tu cuenta en real) |
| `ordenes` | Entrada límite post-only, espera, persecución máxima, consultas de estado, qué hacer si falta un stop (`reponer_y_alertar` o `cerrar`) |
| `salida` | Primer objetivo, parcial, objetivo final, sin pérdidas, stop dinámico y si el stop dinámico sigue el precio medio (`mid`) o el de marca (`mark`) |
| `estrategias` | Activar o desactivar cada una: `{"ruptura_1h": {"activa": false}}`. Las de ballenas y liquidaciones vienen **desactivadas** (investigación) |
| `ballenas` | Recolector opcional. `usar_como_veto: false` por defecto. Si falla, el bot sigue |
| `real`, `piloto_real` | Habilitación del modo real y sus límites, más prudentes (50 $ de capital, 0,5 % de riesgo, 1 posición, 10 $ de pérdida máxima, solo BTC y ETH) |
| `shadow` | Saldo teórico con el que se calculan los tamaños en shadow |
| `panel` | Dirección, puerto y si se permiten comandos desde fuera del PC (`comandos_remotos`, `false` por defecto) |
| `laboratorio` | Tramos walk-forward, holdout, embargo, bootstrap y semilla |

Comprobar la configuración sin arrancar nada (no conecta ni opera):
```
python bot.py --validar-config
```
Muestra los valores que se aplican de verdad y de dónde sale cada uno.

### Migración desde la versión anterior

Las versiones anteriores tenían los ajustes de riesgo sueltos (`riesgo_por_operacion_pct`, `max_posiciones_abiertas`, `comision_taker_pct`...). Al arrancar, el bot **migra** `config.json` solo:

1. Guarda una copia exacta: `config.json.v1-AAAAMMDDTHHMMSSZ.bak`.
2. Mueve cada valor a su bloque nuevo **conservando lo que tenías** (`riesgo`, `comisiones`, `ordenes`).
3. Quita los duplicados que no se aplicaban (`filtros_entrada.sl_min_pct`/`sl_max_pct`, `motor.modulos_sin_validar`, `gestion_capital.riesgo_max_orden_minima_pct`).
4. `duracion_max_min` pasa a `riesgo.duracion_max_global_h` (como mínimo 336 h, para no cortar estrategias de varias semanas).

Para hacerlo a mano: `python bot.py --migrar-config`. Para volver atrás: para el bot y renombra la copia `.bak` a `config.json`.

**Posiciones de la versión anterior.** Si `estado.json` tiene posiciones sin `trade_id`, el bot las marca como de **propiedad incierta**: no las toca (siguen protegidas por las órdenes que ya tenían en la cuenta), las muestra en el panel y cuentan como exposición. En real, además, no abre nada nuevo hasta que desaparezcan.

## Seguridad: qué es del bot y qué no

- Una posición es **del bot** solo si está en su registro con su `trade_id` y los CLOID de sus órdenes, y coincide con la cuenta. Todo lo demás es **externo**: se muestra y cuenta como riesgo, pero el bot **nunca** lo cierra, ni le pone stop, ni cancela sus órdenes.
- Si la cuenta no cuadra con el registro (tamaño mayor, lado distinto, órdenes desconocidas en la misma moneda), ese mercado queda **congelado** y se avisa.
- Cada orden lleva un **CLOID** generado antes de enviarla. Si la respuesta es dudosa (timeout, error de red), el bot **consulta** su estado y **nunca la reenvía**. Si no puede saberlo, congela el mercado. Si el exchange no la conoce, queda como `no_confirmada` (no como rechazada: podría aparecer tarde).
- Una cancelación solo se da por buena tras **re-consultar** al exchange: un «OK» a la petición de cancelar no basta. Si sigue abierta, parcial o desconocida, el mercado se congela.
- **Entrada con orden límite + reserva IOC sin sobrellenados:** la IOC de reserva solo se envía si la orden límite está en un estado final **confirmado** (cancelada, completada o rechazada) y solo por lo que falte según la **posición real** de la cuenta y los fills (deben coincidir). Si no se confirma, no se envía nada más: el mercado se congela, se protege con stop el tamaño REAL y la conciliación lo resuelve (cancelando la orden de entrada si sigue viva y adoptando el tamaño solo si lo explican los fills de las propias órdenes del bot).
- **Stops sin duplicados:** antes de poner o reintentar un stop se mira en la cuenta si ya hay uno del bot que valga (p. ej. de un envío con respuesta ambigua) y se adopta. Si aun así aparecen varios, se conserva el registrado y los demás se cancelan (verificado). Las órdenes de stop, objetivos y cierre son siempre reduce-only: enviar una sin reduce-only es un fallo del bot y se bloquea antes de llegar al exchange.
- **Tras cerrar** se comprueba en la lista de órdenes abiertas de la cuenta que no queda ninguna orden del bot de esa operación; si queda alguna, el mercado se congela y se avisa.
- **Máquina de estados** de cada orden (`ejecucion.TRANSICIONES_ORDEN`): un estado final no vuelve atrás y un estado incierto solo se resuelve consultando. Repetir un ciclo o una respuesta no duplica fills, comisiones, PnL ni funding (cada uno lleva un id y el ledger los cuenta una sola vez).
- **Una sola instancia:** al arrancar, el bot toma un bloqueo del sistema operativo por carpeta y por cuenta (`bot.lock` y `~/.bot-ballenas/bloqueos/`). Se libera solo si el proceso muere (no queda pegado tras una caída). No puede impedir que **otro equipo** use la misma cuenta: nunca arranques a la vez el bot del PC y el del servidor. En marcha, una orden con CLOID desconocido en la cuenta se avisa como posible segunda instancia.
- **Sin «dead man's switch»:** Hyperliquid tiene `scheduleCancel`, pero según su documentación cancela **todas** las órdenes abiertas, incluidos los stops. Activarlo dejaría las posiciones sin protección justo cuando el bot no responde, así que **no se usa**.
- **Qué pasa si el bot cae con una entrada pendiente:** la orden de entrada (ALO) solo vive la espera configurada (`ordenes.espera_limite_seg`, máximo 60 s) y al primer llenado parcial se deja de esperar, se cancela el resto y se protege lo ejecutado. Si el proceso cae justo en ese hueco, la orden puede llenarse mientras el bot está apagado: al arrancar, la conciliación cancela (y confirma) las entradas propias que sigan vivas y protege con stop lo que haya. Las órdenes propias huérfanas (de una operación que ya no está en el registro) se reconocen por su CLOID (registro `cloids_propios` y ledger) y se cancelan.
- **Una operación por moneda, reservada antes de enviar nada:** `gestor.abrir` lee la cuenta en ese momento y no envía ninguna orden si ya hay una operación del bot en esa moneda (aunque esté pendiente o incierta) o si la cuenta ya tiene posición ahí. El exchange netea por mercado: dos operaciones en la misma moneda acabarían mezcladas en una sola posición (lo que pasó con kPEPE el 23/10, ya corregido).
- **Los stops no garantizan el precio:** se ejecutan a mercado con una tolerancia y en un salto brusco pueden llenarse peor o tarde. Por eso los límites de pérdida se miden con el valor de la cuenta (incluye lo no realizado).
- **Tope del piloto real (`piloto_real.perdida_max_usd`):** es un tope, no solo un bloqueo: al perder esa cantidad (valor de la cuenta, incluido lo no realizado) el bot cierra sus posiciones y se apaga, igual que con el suelo de emergencia (`riesgo.perdida_max_total_pct`).
- **Funding en el tamaño:** el riesgo de cada operación incluye el funding que podría pagar durante su duración prevista (el mayor entre el actual en contra y el básico de Hyperliquid, ×2 de margen).
- **Depósitos y retiradas:** un cambio del capital simulado queda registrado como evento (`papel.json → ajustes_capital`, con hora) y las referencias de día, semana, máximo e historial se ajustan con esa hora exacta: un depósito no cuenta como beneficio.
- Antes de abrir nada, el bot **concilia** su registro con la cuenta. También al arrancar.
- En cada ciclo comprueba que cada posición del bot tiene su **stop** con el tamaño, lado y precio correctos. Si no: alerta, bloquea entradas y lo repone (o cierra, según `ordenes.politica_sin_stop`).
- Para mover un stop, primero pone el **nuevo** y lo verifica; después cancela el viejo.
- Varios errores seguidos con el exchange ponen el bot en estado **degradado**: no abre nada hasta que se recupere.
- **Apagado ordenado** (Ctrl+C, cerrar la ventana o `systemctl stop`): deja de abrir, guarda todo y **conserva** los stops y objetivos en el exchange.
- Los secretos (`.env`) nunca salen en el panel, en los registros ni en los informes. No incluyas `.env` al compartir el proyecto (usa `.env.example`).
- El panel no usa manejadores `onclick` con datos: las acciones van en atributos `data-*` (escapados) y una lista cerrada de funciones permitidas.
- Dependencias fijadas: `requirements.txt` (directas) y `requirements-bloqueado.txt` (entorno completo verificado).

## Simulación frente a ejecución real

| Paso | Real (Hyperliquid) | Simulado (paper) | Laboratorio |
|---|---|---|---|
| Entrada | Límite post-only (ALO), espera, cancelación confirmada, IOC de reserva por lo que falte | Igual: misma lógica y mismo código del gestor | A mercado (taker) en la apertura de la vela siguiente: más prudente |
| Fills de la IOC | Recorre el libro; puede ser parcial | Recorre los niveles reales de una foto del libro; parcial si no hay profundidad | Precio de apertura + deslizamiento supuesto (mínimo + parte del ATR) |
| Orden límite | Llena según cola | Solo si el precio la atraviesa (sin cola) | — |
| Stop | Trigger con la **marca**, ejecución a mercado (10 % de tolerancia) | Igual: marca + ejecución contra el libro | Precio del stop (o apertura si hay hueco) peor por el deslizamiento |
| Objetivos | Límite reduce-only (maker) | Igual (atravesados) | A su precio, maker |
| Cierre por tiempo | IOC reduce-only | Igual | Cierre de la vela peor por el deslizamiento |
| Comisiones | Las de la cuenta (maker/taker según el fill) | Tarifa manual (estimación), según el fill | Taker en la entrada y en salidas a mercado |
| Funding | Horario, nocional al precio del oráculo | Igual, con la tasa real vigente | Histórico real del periodo |
| Liquidación | `liquidationPx` real, vigilado cada ciclo | Estimada (fórmula de margen aislado) | Filtro previo con la fórmula aproximada |

## Límites duros y tamaño

- **Tamaño por riesgo:** el riesgo de cada operación incluye la distancia al stop **más** las comisiones de entrada y salida, media horquilla, deslizamiento estimado y un margen de estrés. El tamaño se redondea **hacia abajo**.
- Si la orden mínima de Hyperliquid (10 $) arriesgaría más de lo permitido, la operación se **descarta** con el motivo «tamaño mínimo excede riesgo». Ya no se fuerza la orden mínima.
- Usa el **menor apalancamiento** que cabe y exige que la liquidación quede bastante más lejos que el stop. Esa fórmula aproximada solo **filtra al entrar**; después manda el exchange: en cada ciclo se leen el `liquidationPx`, el margen usado, el de mantenimiento y el valor de la cuenta **reales**. Si la liquidación queda antes que el stop, se cierra la posición del bot (reduce-only); si queda demasiado cerca, se bloquean entradas; si no se conoce (real), se bloquean entradas (`riesgo.politica_riesgo_desconocido`); si el mantenimiento supera `riesgo.mantenimiento_max_fraccion_equity` del valor de la cuenta, también.
- **Impacto en el libro:** el tamaño se recalcula con el impacto de recorrer el libro real con ese tamaño (entrada y salida). Si el libro no lo cubre o el impacto supera `riesgo.impacto_max_pct`, no se entra.
- **Exposición a BTC ponderada por beta** (`riesgo.exposicion_beta_btc_max_pct`): suma lado × tamaño × beta frente a BTC de las posiciones del bot, las **externas** y la nueva (un corto compensa un largo). Sin datos para la beta se usa `riesgo.beta_desconocida` (1,5, prudente). Es una medida aproximada: no capta saltos ni cambios de beta.
- Tras cada fill, el riesgo se **recalcula** con el tamaño y el precio medio realmente ejecutados (aunque difieran del plan).
- La lectura de la cuenta debe ser reciente (`riesgo.cuenta_max_antiguedad_seg`); si falla o es vieja, no se abre nada (los límites diarios se calculan con equity reconciliada).
- Límites que **paran** las entradas (no reducen tamaño): pérdida del día y de la semana (UTC), caída desde el máximo, operaciones del día, riesgo abierto, exposición, datos de precios viejos, conciliación pendiente, stops sin verificar, ejecución degradada, reloj desfasado.
- La gestión de capital (`gestion_capital`) solo **reduce** el tamaño cuando las cosas van mal; nunca lo aumenta.
- **Suelo de emergencia** (−50 % desde el inicio): cierra **solo las posiciones del bot** y se apaga (código 3: no se reinicia solo).

### ¿Por qué tenía tan pocas operaciones y todas a 30 h?

- Con `max_posiciones_abiertas: 2`, dos operaciones de *ruptura 1h* ocupaban todos los huecos. En una reproducción de 3 días con datos reales, **49 de 73 señales descartadas** lo fueron por «máximo de posiciones abiertas (2)». Ahora el valor por defecto es **4** (con un máximo de 3 del mismo lado y el límite de riesgo abierto).
- Las **30 h** son la duración que el laboratorio fijó para *ruptura 1h* (30 velas de 1 h). Cada estrategia tiene la suya; ahora además hay un tope global (`riesgo.duracion_max_global_h`) y el panel muestra la duración de la estrategia y la efectiva.
- Más operaciones no significa más ganancias: sin una estrategia validada, más operaciones solo es más ruido. En simulado sirve para aprender antes.

## Laboratorio

```
python laboratorio.py --guardar-memoria   # todo, y preentrena el aprendizaje
python laboratorio.py --rapido            # menos monedas y parámetros, para probar
python laboratorio.py --sin-ballenas      # sin descargar el historial de las ballenas
python laboratorio.py --sin-cache         # vuelve a descargarlo todo
python laboratorio.py --acciones          # también acciones e índices (HIP-3; solo informe)
python laboratorio.py --presupuesto 300   # menos peticiones por minuto (si el bot está en marcha a la vez)
```

**Cómo valida (sin mirar el futuro):**
1. **Walk-forward:** 5 tramos de prueba seguidos. En cada uno, los parámetros se eligen **solo con lo anterior**.
2. **Purga y embargo en TODAS las particiones:** una operación solo cuenta en un tramo si abre **y cierra** dentro de él, antes de un embargo de 24 velas al final. Una operación que cruza al tramo siguiente (o al holdout) no se cuenta en ninguno: ningún resultado usa precios de otra partición. Lo mismo en la investigación nocturna.
3. **Holdout de UN SOLO USO por experimento, y ACUMULATIVO:** `laboratorio_holdout.json` guarda hasta qué fecha se ha mirado ya el holdout de cada estrategia. Solo las operaciones posteriores cuentan como confirmación independiente. Mientras sean menos de 10, quedan **selladas**: no se calcula ni se muestra su resultado, no entran en la memoria del aprendizaje y **no se marcan como vistas**, así se acumulan semana a semana (antes cada pasada las gastaba y una estrategia de 4 h nunca llegaba a 10). Al llegar a 10 se abren una sola vez, cuentan como confirmación y se marcan. Mientras están selladas, el resultado como mucho es **«exploratorio»** (nunca «supera»). El formato anterior se migra de forma conservadora (todo lo ya mirado deja de ser independiente) y se conserva.
4. **Universo en el momento:** una operación solo cuenta si su moneda estaba entre las más líquidas **en esa fecha** (con historia ampliada, además con el volumen mínimo diario del bot). Ojo: solo con monedas que existen hoy (faltan las retiradas): la reconstrucción del universo histórico **no es completa** (sesgo de supervivencia).
5. **Pruebas múltiples:** valor crítico de una **t de Student de una cola** con n−1 grados de libertad al nivel 5 %/k, con k = variantes probadas **desde siempre** (laboratorio + investigación; `laboratorio_variantes.json`). Repetir pruebas o cambiar la rejilla a mano no regala significación.
6. **Bootstrap por bloques:** intervalo de confianza de la media.
7. **Líneas base comparables:** entrada al azar y «siempre largo» con los mismos datos, horizonte, stop, salida y costes, y dentro de la misma partición. Una línea base vacía o con pocas operaciones da **«no evaluable»**, nunca un aprobado.
8. **Escenarios de estrés explícitos:** costes ×2, deslizamiento ×3, salidas por stop 0,15 R peor (huecos, marca ≠ mid), funding pagado ×3 y el combinado. Cada uno se informa por separado.
9. **Costes en la simulación:** entrada supuesta a mercado (taker, más prudente que la orden límite del bot), deslizamiento variable con la volatilidad (mínimo 0,02 % + 5 % del ATR relativo), salidas por stop y por tiempo con deslizamiento y funding real del periodo.
10. **Informe completo:** resultado neto en R, nº de operaciones, caída máxima, % largos/cortos, estabilidad por trimestre, desglose por moneda, lado y régimen de BTC, y comparación con las líneas base. Nunca solo el % de acierto.
11. **Reproducibilidad:** cada ejecución queda en `registro/laboratorio_ejecuciones.jsonl` (semilla, huella de datos, universo, código, configuración, particiones, holdout visto y resultados).
12. **Historia ampliada** (`laboratorio.historia_externa`, `historia_externa.py`): Hyperliquid solo da 5000 velas (≈ 7 meses en 1 h, ≈ 2,3 años en 4 h). Antes de esas velas se añade la historia **pública** de Binance y, si no coincide o no está, de Bybit (futuros perpetuos en USDT): por defecto **6 años en 4 h y 2 en 1 h**, con su funding. Solo lectura de datos de mercado: sin cuenta, sin claves, sin órdenes; se opera siempre en Hyperliquid. Reglas: Hyperliquid **manda** donde tiene datos; cada fuente se **valida** contra Hyperliquid en las velas comunes (precio con desviación mediana ≤ 1 %; si no, se rechaza para esa moneda: p. ej. otro token con el mismo ticker); el volumen externo se **escala** al de Hyperliquid; el funding de 8 h/4 h se reparte por horas. El grupo descargado incluye monedas con al menos 5 M$ de volumen hoy (`volumen_min_pool_usd`), y una operación solo cuenta si su moneda estaba entre las más líquidas **y** superaba el volumen mínimo diario del bot **en esa fecha**. El informe dice qué fuente y cuántas velas externas tiene cada moneda (`ejecucion.historia_externa`) y da el resultado **solo en el tramo de Hyperliquid** (`fuera_de_muestra_solo_hyperliquid`) como comprobación. La primera vez descarga varios minutos de datos (caché en `datos_backtest/externo/`); el laboratorio tarda bastante más que antes (horas). Para desactivarlo: `"historia_externa": {"activa": false}`.
13. **Aportación y solapamiento** (solo informe): para fuerza relativa y momentum (y todas), cuánto aporta cada una a la combinación, cuánto se solapan sus señales, su correlación diaria entre sí y con BTC, y la reversión a la media por régimen de BTC.

**Para que una estrategia «supere el laboratorio»** necesita: al menos 30 operaciones fuera de muestra, media > 0, t por encima del umbral de Student corregido, límite inferior del intervalo de confianza > 0, holdout positivo **y holdout no consultado antes** positivo, mejor que las líneas base comparables, positiva en cada escenario de estrés (salvo el combinado, que se informa) y positiva en las monedas con historia completa. Muestra insuficiente, holdout corto o línea base insuficiente: **«no evaluable»**. Ballenas y liquidaciones siguen en investigación: como mucho «exploratorio».

**Experimentos solo de investigación:** filtro de régimen (elegido con datos de entrenamiento), carry de funding cubierto (modelado) y desequilibrio del libro (con el libro que archiva el bot, a partir de 14 días). Se informan, el bot no los usa. Pendiente: pares (prioridad baja).

**Qué deja:** `estrategia_elegida.json`, `registro/laboratorio_ejecuciones.jsonl` (id de ejecución, huella de la configuración y del código, semilla, huella de los datos, universo), `aprendizaje_lab.json` y evidencia en `promociones.json`. **El laboratorio nunca aprueba nada ni sube límites.**

## Cerebro en vivo: el único que decide

`cerebro_vivo.py`: las estrategias solo **proponen**; el cerebro decide si cada candidata se opera, con el modelo que entrena y guarda el laboratorio (`cerebro_modelo.json`, con versión y huella). Calcula los rasgos con la misma función que el laboratorio y solo con velas cerradas (también las de Binance/Bybit: la vela en curso se descarta); los datos de Binance/Bybit le llegan de `contexto_externo.py` (últimas velas con flujo comprador y funding). Garantías: sin modelo, modelo corrupto o de otra versión de rasgos → **no entra nadie** (se abstiene y lo dice en el panel); datos externos viejos → esos rasgos «sin dato»; error interno → no entra. La protección (filtros, límites, stops, bloqueos, piloto real) se aplica después, igual que antes. `"cerebro": {"decide": false}` vuelve al filtro de aprendizaje anterior. Panel → Estrategias → «Cerebro».

**Carry de funding realista** (investigación, `carry_realista`): corto en el perpetuo + largo en contado solo con funding positivo, con el riesgo de base (perpetuo de Hyperliquid frente al contado de Binance), 4 comisiones y rentabilidad sobre el capital de las dos patas; aparte, monedas con contado en Hyperliquid y solo el periodo con funding de Hyperliquid.

## Cerebro: un solo modelo con todos los datos (fase 2, solo laboratorio)

`mercado_unificado.py` calcula para cada posible entrada los mismos rasgos con historia y en vivo: tendencia, impulso, volatilidad y funding de Hyperliquid, y de Binance/Bybit la diferencia de funding, el flujo comprador agresivo (última vela y 6 velas) y la diferencia de precio. `cerebro.py` junta las candidatas de TODAS las estrategias y un modelo lineal con penalización (ridge, sencillo a propósito) estima su resultado en R; se operaría solo si supera +0,05 R (umbral fijo, no se optimiza). Se entrena por tiempo, solo con operaciones ya cerradas antes de cada tramo. El laboratorio compara **todas las candidatas · cerebro con todo** (historia ampliada con Binance/Bybit) y, aparte, **todas las candidatas · cerebro solo Hyperliquid** usando únicamente candidatas de cuando ya había velas de Hyperliquid (rasgos y etiquetas 100 % HL). Cada cerebro debe superar: t corregida por variantes, **t por días** (resultado sumado por día, porque las operaciones del mismo día no son independientes), intervalo bootstrap, estrés, holdout sellado, **mejorar a «operar todas las candidatas»** del mismo periodo y dar resultado positivo en una **cartera con los límites reales del bot** (una posición por moneda, máximo de posiciones, riesgo abierto...). El modelo que usa el bot es el de la variante que supera (si ninguna, «con todo», solo para acumular evidencia en simulado) y se entrena con el mismo universo que se validó.

**Importante:** que el cerebro supere el laboratorio no valida ninguna estrategia por separado (los módulos de señal siguen «sin validar»), no autoriza nada en real y la ejecución del laboratorio (entrada a mercado en la vela siguiente) no es idéntica a la real (ALO + IOC). Hace falta evidencia en simulado/shadow y aprobación humana.

## Binance y Bybit: solo datos

El bot **solo opera en Hyperliquid**. De Binance y Bybit usa únicamente datos **públicos** de mercado, sin cuenta, sin claves y sin enviar nunca nada:

- **Laboratorio e investigación:** años de velas y funding para ampliar la historia (punto 12 del laboratorio).
- **En vivo** (`contexto_externo.py`, bloque `contexto_externo` de `config.json`): cada 5 minutos, para las monedas vigiladas, precio frente a Hyperliquid, funding por hora (y su diferencia con el de Hyperliquid), interés abierto y su cambio en 1 h, y qué parte del volumen total tiene Hyperliquid. Se ve en el panel (pestaña **Ballenas**, tarjeta «Binance y Bybit») y se archiva en `datos_mercado/externo_*.jsonl`.
- **Uso hoy: informe e investigación.** No cambia ninguna decisión. Con 14 días archivados, el laboratorio mide si la diferencia de funding o el cambio de interés abierto externos anticipan la vela siguiente (`investigacion.binance_bybit_1h`). Si algún día se usa para decidir, pasará por las etapas normales.
- Si un exchange no responde (o está bloqueado en tu país), el panel lo marca y el bot sigue igual. Se desactiva con `"contexto_externo": {"activo": false}`.

## Datos propios, evidencia y mejoras continuas

**Archivo propio** (`datos_archivo/`, bloque `archivo` de `config.json`). Mientras funciona, el bot guarda:
- las velas cerradas que ya descarga (sin peticiones extra), para que con el tiempo el laboratorio tenga **más historia** que las 5000 velas que da Hyperliquid;
- cada 5 minutos, los 10 mejores niveles del **libro de órdenes** de cada moneda vigilada (en segundo plano, sin frenar el bot). Con 14 días, el laboratorio estudia si el desequilibrio del libro anticipa la vela siguiente.

El interés abierto y el funding por minuto ya los grababa `mercado.py` (`datos_mercado/`). Nada de esto opera: solo se guarda.

**Progreso de la evidencia** (panel → Estrategias). Para cada estrategia, cuántas operaciones lleva en shadow y en simulado **con su versión actual** (objetivo: 30), su R medio y su t, y cuántos días de mercado, libro y velas propias lleva acumulados el bot.

**¿Aporta el filtro de aprendizaje?** (panel → Aprendizaje, y `informe.py`). El bot apunta, para **cada** señal, si el aprendizaje la habría aceptado o rechazado, y sigue su resultado (operada o en papel). Con al menos 30 señales cerradas en cada grupo da un veredicto: *aporta*, *no se distingue* o *resta*. Si no aporta, la recomendación es desactivarlo (`aprendizaje.activo: false`); lo decides tú.

**Laboratorio semanal** (`herramientas\activar-laboratorio-semanal.bat`). Crea una tarea del Programador de tareas de Windows que cada domingo a las 04:00 pasa el laboratorio con pocas peticiones por minuto (para no molestar al bot) y **solo te avisa por Telegram si alguna estrategia cambia de estado**. Cada ejecución queda en `registro/laboratorio_semanal.jsonl`. Se quita con `desactivar-laboratorio-semanal.bat`. A mano: `python laboratorio_semanal.py`.

**Filtro de costes** (`riesgo.coste_max_fraccion_riesgo`, 0,20 por defecto). Si entrar y salir cuesta más del 20 % de lo que se arriesga (stop demasiado corto), la señal se descarta. El coste esperado cuenta la entrada con orden límite (maker), la salida a mercado (taker), media horquilla y un deslizamiento. El laboratorio aplica el mismo filtro. Pon 0 para desactivarlo.

**Momentum de varias semanas** (`momentum_4h`, estrategia nueva). Cada ~4–7 días mira cuánto ha subido o bajado cada moneda en las últimas 2–4 semanas; si el movimiento es claro (varios ATR y con poca ida y vuelta), va a favor y mantiene hasta el siguiente reparto, con un stop amplio de seguridad. El tamaño ya se ajusta a la volatilidad (el stop se mide en ATR y el bot arriesga un % fijo). Solo en velas de 4 h. Como todas, opera en simulado/shadow y necesita superar el laboratorio y una aprobación para ir a real.

**Carry de funding cubierto** (solo investigación, en el informe del laboratorio). Modela cobrar el funding con el precio neutralizado (perpetuo contra spot). No opera: el modelo no incluye la diferencia de precio entre spot y perpetuo, ni comprueba qué monedas tienen spot.

## Departamento de investigación (ideas nuevas, sin operar)

Cada noche (de lunes a sábado; el domingo va el laboratorio) el bot **inventa y examina ideas nuevas** combinando piezas de una gramática: un disparador (ruptura, cruce de medias, momentum, RSI extremo, bandas, compresión de volatilidad, vela grande), filtros (tendencia, eficiencia, volatilidad, a favor de BTC) y una salida (corta, media, larga o sin objetivo), en velas de 1 h o 4 h. A mano: `python investigacion.py` o `herramientas\investigar.bat`; en el PC, programado con `herramientas\activar-investigacion-nocturna.bat`; en el servidor ya va solo (`bot-ballenas-investigacion.timer`).

Cómo se protege de engañarse a sí mismo:
- **Examen en dos fases**: primero un filtro rápido (al menos 40 operaciones, media positiva, t suficiente, intervalo de confianza por encima de 0 y aguantar costes al doble); las que pasan, el veredicto completo del laboratorio (fuera de muestra con purga, holdout final, comparación con entradas al azar y con «comprar y mantener», monedas veteranas).
- **Cuantas más ideas prueba, más exige**: t de Student de una cola corregida por el total de variantes probadas desde siempre (laboratorio + investigación). Con 60 ya está por encima de 3.
- **El holdout de la investigación se consulta cada noche**, así que casi nunca es independiente: una aspirante es exploratoria por definición. Su confirmación independiente es el seguimiento en vivo: para quedar pendiente de tu revisión necesita, en 30 operaciones, media > 0 y t por encima del umbral de Student corregido por las aspirantes vigiladas a la vez.
- **Cementerio**: una idea descartada no se vuelve a probar.
- **Aspirantes**: las pocas que pasan (máximo 10) se siguen **en vivo con precios reales pero SIN órdenes**, sin ocupar huecos, sin contar para los límites y sin influir en el aprendizaje del bot. Tras 30 operaciones en vivo: si fallan, al cementerio; si aguantan, quedan **pendientes de tu revisión**. Convertir una en estrategia es decisión tuya y pasaría por todas las etapas de promoción.
- Solo avisa por Telegram si algo cambia. Informe en `registro/investigacion_informe.md` y en el panel → **Investigación**.

**Vigilancia de estrategias en uso** (cada hora). Mira las operaciones cerradas de los últimos 60 días de cada estrategia, en orden temporal. Con al menos 30, calcula una **cota superior** del R medio con **bootstrap por bloques** (no supone que las operaciones sean independientes) al nivel 95 % corregido por el número de estrategias vigiladas a la vez. Si incluso esa cota es negativa, la estrategia está **degradada**: opera a **mitad de tamaño**, se le **retira la autorización para real** si la tenía y te avisa. La recuperación es **gradual y con persistencia**: al dejar de estar degradada sigue a ×0,5; tras 7 días seguidos sana pasa a ×0,75 y tras otros 7, a su tamaño normal (si recae, vuelve a ×0,5). Nunca por encima del tamaño autorizado, y la autorización para real solo la devuelves tú.

Lo normal es que durante semanas **ninguna idea pase**: eso significa que el filtro funciona, no que el departamento no sirva.

## Promoción de estrategias (nada pasa a real solo)

Etapas: `investigacion` → `laboratorio` → `shadow` → `paper` → `apta_revision` → `real_habilitado`.

- El laboratorio, el modo shadow y el simulado **añaden evidencia** automáticamente; **no cambian la etapa**.
- Las etapas `apta_revision` y `real_habilitado` solo se dan desde la línea de comandos, escribiendo una confirmación literal. Queda registrado quién y cuándo:
  ```
  python promocion.py estado
  python promocion.py aprobar ruptura_1h apta_revision --por "Roberto" --informe lab-2026...
  python promocion.py aprobar ruptura_1h real_habilitado --por "Roberto"
  python promocion.py revocar ruptura_1h --por "Roberto"
  ```
- Cada aprobación va ligada a una **huella** determinista de todo lo que decide o arriesga: la definición de la estrategia, la configuración efectiva completa salvo lo que no decide (panel, modo, subcuenta, saldo ficticio, feed) —riesgo, límites agregados y del piloto, órdenes, salidas, costes, universo, filtros, aprendizaje, ballenas…—, el código de señales, orquestación, motor, riesgo, ejecución, gestión, laboratorio, simulación, contabilidad y promoción, y las versiones del esquema de configuración, del ledger y del modelo de simulación. Si cambia cualquier cosa, la aprobación queda **invalidada**. Si no se puede calcular (falta un archivo), **nada** queda autorizado. La huella no contiene secretos.
- **Requisitos de `apta_revision`** (`promocion.REQUISITOS_APTA`, todos con la misma huella): laboratorio superado (no exploratorio) de hace ≤ 30 días; ≥ 30 operaciones shadow/paper **válidas**; ≥ 14 días de observación; última evidencia de hace ≤ 7 días; R neto medio > 0; resultado neto acumulado > 0; caída máxima ≤ 10 R; costes medios ≤ 0,35 R por operación. La evidencia vieja, incompleta, de otra huella o con errores de conciliación no cuenta. `python promocion.py requisitos <estrategia>` dice qué falta.
- Ningún botón del panel ni comando de Telegram puede aprobar, saltarse comprobaciones o pasar de shadow a real.

## Pasar a real (cuando haya algo validado)

Requisitos, **todos** obligatorios. Si falta uno, el bot **no arranca** y dice por qué:
1. Crea tú, en Hyperliquid, una **subcuenta** dedicada al bot y pasa a ella solo el dinero del piloto. El bot no crea subcuentas ni mueve fondos.
2. Crea una **API wallet** desde tu cuenta principal (puede operar, no retirar). Nunca uses la clave de tu wallet principal.
3. En `.env`: `HL_API_WALLET_KEY`, `HL_DIRECCION_CUENTA` = la subcuenta y `BOT_REAL_CONFIRMADO=SI`.
4. En `config.json`: `"modo": "real"` y `"real": {"habilitado": true, "subcuenta": "0x..."}` (la misma dirección).
5. Al menos una estrategia en `real_habilitado` con la huella actual.

Al arrancar en real, el bot comprueba con Hyperliquid que la cuenta **es una subcuenta**, que la API wallet **pertenece a su cuenta maestra** y que **no hay posiciones ni órdenes que no pueda atribuirse**. Solo después crea el cliente de órdenes. Opera con los límites de `piloto_real` y solo en sus mercados.

## Panel web

`http://localhost:8080` (con `INICIAR.bat` o `herramientas\abrir-panel.bat`). Cada indicador tiene un botón **ⓘ** con su explicación (se abre con teclado o toque; se cierra con Escape o tocando fuera). Hay un glosario completo en Configuración.

Diseño oscuro y compacto, pensado también para el móvil. Casi todo se puede **pulsar** para ver su detalle en un panel lateral.

| Pestaña | Qué muestra |
|---|---|
| **Resumen** | Valor de la cuenta con su evolución (24 h, 7 d, 30 d, todo), cambio desde el inicio, hoy y en la semana; riesgo abierto, operaciones del día, pérdida del día, caída y exposición (también a BTC) con su **barra frente al límite**; posiciones del bot con una **escalera stop · entrada · objetivo** y el precio actual, liquidación y cierre por tiempo; si se pueden abrir entradas (y por qué no); cuenta atrás a la próxima vela; últimos movimientos y alertas |
| **Señales en vivo** | Feed persistente (`registro/feed.jsonl`) de señales, decisiones con cada motivo, órdenes, protección, entradas, salidas, cierres y lo observado del mercado y las ballenas, con procedencia, horas, precio de referencia, frescura y estado. Push (SSE) o sondeo cada 3 s, sin perder ni repetir al reconectar; filtros, búsqueda y pausa. Al pulsar un evento: todos sus datos y enlace a la operación. Lo **inferido** se marca como tal; si el bot deja de escribir, todo se marca **OBSOLETO**. No son predicciones |
| **Operaciones** | Resumen (cerradas, resultado neto, R medio, % ganadoras), registro con filtros y **CSV**, **distribución de resultados en R** y resultado por estrategia. Al pulsar una: por qué entró (incluido lo que opinaba el aprendizaje), ejecución, órdenes, fills, movimientos del stop y cronología completa |
| **Estrategias** | Una tarjeta por estrategia: etapa, estado del laboratorio, R medio fuera de muestra, operaciones, t y evidencia en vivo. Al pulsar: **todo el laboratorio** (entrenamiento, fuera de muestra, holdout, monedas veteranas, con aprendizaje, líneas base y escenarios de estrés), comprobaciones, resultado **por moneda, por régimen de BTC, por lado y por trimestre**, parámetros, lo aprendido y sus últimas operaciones |
| **Ballenas** | Cuántas se siguen, en tiempo real, validadas y vetadas; por cuenta (enmascarada): etiqueta, frescura, resultado de sus señales, puntuación del laboratorio, posiciones observadas y cambios de la última hora; flujo neto por moneda y exposición agregada. Al pulsar: su detalle y su actividad reciente en el feed |
| **Aprendizaje** | Resultado por fuente, si el filtro de aprendizaje aporta, qué habría pasado con lo descartado por cada motivo, por estrategia y los **descartes analizados** (al pulsar: rasgos de la señal y por qué se descartó) |
| **Investigación** | Ideas probadas, t exigida, aspirantes con su progreso en vivo, vigilancia de las estrategias, pendientes de revisión y cementerio |
| **Sistema** | **Solo salud técnica**, sin saldo ni operaciones: estado global (uno obligatorio sin datos da «desconocido»), latido, ciclos, contadores, disco, componentes, incidentes (se pueden reconocer) y registros técnicos con filtros y traza; exportar diagnóstico sin secretos |
| **Configuración** | **Editor de la configuración** (ver abajo), valores de riesgo que se aplican de verdad y glosario |

### Cambiar la configuración desde el panel

En **Configuración** puedes cambiar, por secciones: **monedas** (cuántas, volumen mínimo, incluir siempre o excluir, velas de 1 h/4 h), **ballenas** (cuántas seguir, cuántas en tiempo real —máx. 10—, filtros de selección, añadir o quitar direcciones a mano, modo de sus señales), **estrategias** (encender o apagar cada una), **riesgo y tamaño**, **salidas**, **órdenes**, **aprendizaje**, **gestión del capital** y **ritmo**.

- Los campos cambiados se marcan y abajo aparece «Revisar y guardar». Antes de guardar ves **qué cambia (antes → después)** y un aviso si algo **aumenta el riesgo**.
- Al guardar: se valida la configuración completa con las mismas reglas que al arrancar, se guarda una **copia** de la anterior (`config_copias/`, las 30 últimas), se registra el cambio (`registro/cambios_config.jsonl`) y el bot la **vuelve a validar y se reinicia de forma ordenada** (unos segundos en Windows, ~30 s en el servidor; las posiciones conservan sus stops y se concilian al arrancar).
- **Historial de cambios**: quién/cuándo/qué, con un botón para **volver a lo de antes**.
- Desde el panel **no** se puede cambiar el modo (simulado/shadow/real), la red, el piloto real, las comisiones supuestas, el laboratorio ni la seguridad del panel. Visto desde fuera de tu equipo es de solo lectura (salvo `panel.comandos_remotos`).
- Cambiar algo que decide invalida las aprobaciones y la evidencia acumulada con la configuración anterior (huella).

**Seguridad del panel:** por defecto solo escucha en tu PC. Las acciones exigen un token interno de la página, mismo origen y un tamaño pequeño, y tienen límite de frecuencia. Visto desde fuera es de **solo lectura**. «Cerrar posiciones del bot» pide escribir `CERRAR-BOT`, muestra exactamente qué cerrará y **nunca** toca posiciones externas; en shadow está desactivado.

## Telegram

1. Habla con **@BotFather**, envía `/newbot` y copia el token en `.env` como `TELEGRAM_TOKEN`.
2. Escribe algo a tu bot, ejecuta `python notificaciones.py` y copia el número en `.env` como `TELEGRAM_CHAT_ID`.

| Comando | Qué hace |
|---|---|
| `/estado` | Modo, valor, posiciones y si las entradas están bloqueadas (y por qué) |
| `/pausa` · `/reanudar` | Deja de abrir / vuelve a abrir. Las abiertas siguen gestionándose |
| `/cerrarbot CONFIRMAR` | Cierra **solo** las posiciones del bot y pausa. Sin `CONFIRMAR` solo te dice qué cerraría |
| `/informe` | Resultados por fuente |

## Alertas y qué hacer

| Alerta | Qué significa | Qué hacer |
|---|---|---|
| **Mercado congelado** | Una orden con respuesta dudosa o la cuenta no cuadra con el registro | Revisa en Hyperliquid esa moneda. Se descongela solo cuando la conciliación cuadra |
| **Stop no verificado** | Falta el stop o no coincide | El bot lo repone solo. Si se repite, revisa la conexión |
| **Posición externa** | Hay algo en la cuenta que no es del bot | Es tuyo: el bot no lo toca. En real no debería pasar (subcuenta dedicada) |
| **Ejecución degradada** | Errores seguidos con el exchange | Espera; si sigue, mira Observabilidad |
| **Datos obsoletos** | Los precios llevan demasiado sin actualizarse | Revisa la conexión a internet |
| **Arranque bloqueado** | Configuración o cuenta no seguras | Lee el motivo; el bot no arranca hasta corregirlo |

## Recuperación

- **Se cortó la luz o se cerró la ventana:** vuelve a arrancar. El bot concilia con la cuenta antes de abrir nada; si una entrada se quedó a medias, la reconstruye desde la cuenta y la protege.
- **Registro dañado:** una línea corrupta del ledger se ignora y se avisa; el resto se lee.
- **Volver a empezar en simulado:** con el bot parado, borra `papel.json`, `estado.json` y `panel_datos.json`. Conserva `registro/` si quieres el historial.
- **Cambio de modo:** el bot archiva el estado del modo anterior (`estado-<modo>-<fecha>.json`) y empieza de cero ese modo; la memoria de aprendizaje se conserva.

## En la nube (24/7, sin depender del PC)

Para que el bot funcione día y noche aunque el PC esté apagado, sigue `despliegue/GUIA_ORACLE.md` (Oracle Cloud gratis, región Madrid). Incluye:
- `herramientas\preparar-migracion.bat`: empaqueta el bot con todo lo aprendido (sin `.env`) para subirlo.
- `despliegue/instalar_vps.sh`: instala el bot como servicio (arranca solo, se reinicia si falla, actualizaciones de seguridad, cortafuegos, swap).
- `despliegue/configurar_copias.sh` y `restaurar.sh`: copia diaria a Cloudflare R2 (14 días de versiones) y recuperación.
- `HEALTHCHECK_URL` en `.env`: aviso externo (Healthchecks.io) si el servidor o el bot dejan de dar señales.
- `despliegue/cloudflare_linux.sh` y `herramientas\desconectar-cloudflare.bat`: mover el túnel del panel del PC al servidor.
- Tareas programadas en el servidor: copia diaria, laboratorio semanal y recordatorio mensual de entrar en Oracle.

**Nunca deben funcionar a la vez el bot del PC y el del servidor.**

## Actualizar sin riesgos

1. Para el bot (Ctrl+C) y espera a que diga que se ha detenido.
2. Copia la versión nueva **encima** de la carpeta (no borres `.env`, `config.json`, `registro/`, `datos_mercado/`, `datos_archivo/`, `aprendizaje*.json`, `promociones.json`, `papel.json`, `estado.json`).
3. Arranca con `INICIAR.bat`: valida y, si hace falta, migra la configuración (con copia de seguridad) antes de arrancar.
4. Cualquier cambio de código invalida las aprobaciones de real (huella distinta). Es intencionado.

## Pruebas

```
python -m unittest discover -s tests -t .
```
Usan una cuenta simulada y módulos falsos: **no hacen ninguna llamada a Hyperliquid, no necesitan claves y no pueden operar**.

## Archivos

| Archivo | Para qué |
|---|---|
| `bot.py` | Bucle principal, arranque seguro, shadow, comandos |
| `configuracion.py` | Plantilla, migración y validación de `config.json` |
| `gestor.py` | Propiedad, conciliación, apertura, stops, gestión y cierre |
| `ejecucion.py` | Ciclo de vida de las órdenes (CLOID, consultas, cancelación verificada) y barrera shadow |
| `riesgo.py` | Tamaño, apalancamiento, liquidación, límites agregados y duros |
| `contabilidad.py` | Resultado bruto/neto, comisiones y funding |
| `ledger.py` | Registro versionado de eventos (`registro/ledger.jsonl`) |
| `promocion.py` | Etapas, huella y aprobaciones |
| `observabilidad.py` | Salud del sistema, incidentes y registros técnicos |
| `limitador.py` | Evita superar el límite de peticiones de Hyperliquid |
| `motor.py`, `senales.py`, `estrategia.py` | Señales, indicadores y gestión de salida |
| `ballenas.py`, `seleccion.py`, `mercado.py` | Investigación: ballenas y grabación del mercado |
| `laboratorio.py`, `validacion.py`, `datos.py` | Laboratorio y sus validaciones |
| `laboratorio_semanal.py` | Laboratorio programado con aviso solo si cambia algo |
| `investigacion.py` | Departamento de investigación: genera y examina ideas nuevas; aspirantes y cementerio (`investigacion.json`) |
| `vigilancia.py` | Detecta estrategias en uso que rinden claramente mal (solo reduce) |
| `feed.py` | Feed «Señales en vivo» (persistente, incremental, enmascarado) |
| `instancia.py` | Bloqueo de una sola instancia por carpeta y cuenta |
| `estudios.py` | Aportación marginal y solapamiento de estrategias (solo informe) |
| `comprobar.py` | Comprobaciones de despliegue (`herramientas\comprobar.bat`) |
| `config_panel.py` | Editor de configuración del panel: lista blanca, validación, copias y restauración |
| `archivo.py` | Archivo propio de velas y libro de órdenes (`datos_archivo/`) |
| `aprendizaje.py`, `informe.py` | Memoria de casos parecidos e informe |
| `papel.py` | Cuenta ficticia del modo simulado (`papel.json`) |
| `panel.py`, `panel.html` | Panel web |
| `tests/` | Pruebas automáticas |
| `despliegue/` | Windows y VPS |

## Limitaciones conocidas

- El código **no está probado contra la API real de Hyperliquid** (solo con la cuenta simulada y módulos falsos). Las comprobaciones de subcuenta usan la consulta `userRole`; si Hyperliquid cambia su respuesta, el arranque en real se bloquea (falla cerrado).
- En simulado, un stop se activa con la **marca** (como en Hyperliquid) pero se comprueba cada pocos segundos (no cada tick) y se ejecuta contra una **foto** del libro real (que no reacciona a la orden). Las órdenes límite se llenan solo si el precio las **atraviesa** (aproxima la cola, que no se simula). La liquidación simulada es **estimada**. La marca se lee como mucho cada pocos segundos.
- Las comisiones en simulado son una **estimación** (tarifa manual de `config.json`, nivel 0 sin descuentos: 0,015 % maker / 0,045 % taker según la documentación de Hyperliquid). En real se leen de la cuenta (`comisiones.fuente = "cuenta"`). La real depende del volumen de 14 días y del HYPE en staking.
- El laboratorio usa las monedas que existen hoy: las retiradas no tienen datos. La reconstrucción del universo histórico **no es completa** (sesgo de supervivencia).
- El laboratorio no tiene libro histórico: el deslizamiento es un **supuesto** (mínimo + parte del ATR), por eso los escenarios de estrés lo multiplican.
- La exposición a BTC ponderada por beta y la correlación son medidas **aproximadas** del riesgo de cartera; no hay datos de sector para medir concentración por factor.
- No se puede impedir desde un equipo que **otro equipo** opere la misma cuenta: nunca arranques a la vez el bot del PC y el del servidor.
- **No es asesoramiento financiero.** Ninguna estrategia está validada; opera solo con dinero que puedas permitirte perder.
