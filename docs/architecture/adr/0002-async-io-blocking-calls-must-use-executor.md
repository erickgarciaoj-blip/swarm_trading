# ADR-0002: I/O bloqueante debe correr en executor, nunca directo en una corrutina

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Fase 1

## Contexto

`CLAUDE.md` establece "Use async/await everywhere; this is a fully async codebase" como regla del proyecto. En la práctica, el análisis de arquitectura (`ARCHITECTURE_REVIEW.md`) encontró varias llamadas síncronas/bloqueantes invocadas directamente dentro de métodos `async def`, sin `asyncio.to_thread`/`run_in_executor`:

- `MarketFeed._fetch_yfinance` (`data/feeds/market_feed.py`) — `yf.download()`, I/O de red, invocada **cada 15s por cada uno de los 5 símbolos** desde el loop principal del `SwarmOrchestrator`. Esta era la más grave: congelaba el event loop completo (los 100 agentes, el WebSocket del dashboard, las rutas HTTP) mientras duraba la descarga.
- `NewsFeed._fetch_forexfactory` (`data/news/news_feed.py`) — `feedparser.parse()`, I/O de red. Menor impacto hoy (backend por defecto es `"demo"`, no `"forexfactory"`), pero mismo patrón.
- `IBKRBroker.connect` (`brokers/ibkr/ibkr_broker.py`) — `self._client.connect(...)` (conexión TCP síncrona de `ibapi`), solo una vez al arrancar, y solo alcanzable en modo `live` real (hoy el swarm corre con `offline=True`).
- Todos los métodos de `MT5Broker` (`brokers/mt5/mt5_broker.py`) — cada llamada a `mt5.*` es IPC síncrono al terminal de MT5. Código inalcanzable hoy (MT5 solo soporta Windows, se corre en macOS/VPS Linux), pero debe quedar correcto para cuando se soporte.

Se verificó con `grep` que no existen otros patrones bloqueantes comunes en el repo (`requests`, `urllib`, `time.sleep`, `subprocess`, `sqlite3` síncrono).

## Alternativas consideradas

1. **No hacer nada / aceptar el bloqueo periódico** — descartada de inmediato: contradice la regla explícita de "fully async" y es la causa raíz de que el sistema completo se congele cada 15 segundos, algo inaceptable para un motor que debe correr 24/7.
2. **Migrar a un cliente HTTP async nativo** (ej. `httpx` async en vez de `yfinance`/`feedparser`) — objetivamente superior a largo plazo (sin overhead de threads, más control), pero requeriría reimplementar el parsing de yfinance/RSS a mano. Se descarta para esta fase por desproporcionada respecto al problema inmediato; queda como mejora futura si `yfinance`/`feedparser` se vuelven un cuello de botella real (hoy no lo son una vez sacados del event loop).
3. **`asyncio.to_thread` / `loop.run_in_executor`** (la elegida) — cambio mínimo, mecánico, de bajo riesgo: mueve la llamada bloqueante a un thread del executor por defecto, liberando el event loop mientras se ejecuta.

## Decisión

Toda llamada de I/O síncrona invocada desde una corrutina se envuelve en `asyncio.to_thread(...)` (o `loop.run_in_executor(None, ...)` donde ya existía ese patrón, como en `IBKRBroker`, para mantener consistencia con el código ya presente). Aplicado a los 4 puntos listados en el contexto.

**Criterio para decidir qué envolver:** operaciones de red o IPC entre procesos, sí. Un `Path.stat()`/`Path.exists()` sobre el filesystem local (usado por el hot-swap de modelos RL de ADR-0001) no se envuelve — es una syscall de microsegundos; envolverla en un executor agrega más overhead de scheduling que el tiempo que ahorra.

## Ventajas

- Elimina la congelación periódica total del sistema — el hallazgo de mayor impacto de todo el análisis de arquitectura.
- Cambio mecánico y de bajo riesgo: no cambia ninguna lógica de negocio, solo el mecanismo de ejecución de la llamada.
- Reutiliza un patrón que ya existía en el propio repo (`IBKRBroker` ya usaba `run_in_executor` para su `_connected_event.wait(...)`), no introduce un concepto nuevo.
- Se agregó un test de regresión (`tests/unit/test_market_feed.py::test_fetch_yfinance_does_not_block_the_event_loop`) que mide directamente si el event loop queda libre durante la llamada — verificado que falla contra el código viejo (delay medido de 0.21s) y pasa contra el código corregido, para confirmar que el test tiene poder de detección real y no es un falso positivo.

## Desventajas

- `asyncio.to_thread` usa el thread pool executor por defecto (tamaño limitado, compartido) — con muchas llamadas bloqueantes concurrentes podría agotarse. No es un riesgo hoy (5 símbolos, baja frecuencia), se revisita si el número de fuentes de datos externas crece mucho.
- No resuelve la causa raíz (yfinance/feedparser siguen siendo bloqueantes por naturaleza) — solo la aísla. Ver Alternativa 2 para el camino a una solución más profunda si hiciera falta.

## Consecuencias

- `data/feeds/market_feed.py`, `data/news/news_feed.py`, `brokers/ibkr/ibkr_broker.py`, `brokers/mt5/mt5_broker.py` modificados.
- Nuevo archivo `tests/unit/test_market_feed.py` — primera cobertura de test que existe para `MarketFeed` (antes no tenía ninguna).
- Regla operativa hacia adelante: cualquier I/O síncrono nuevo que se agregue al codebase (nuevos feeds de datos, nuevos brokers) debe envolverse en `asyncio.to_thread`/`run_in_executor` desde el primer commit, no como corrección posterior.
