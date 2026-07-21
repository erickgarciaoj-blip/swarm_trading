# Architecture Review — Swarm Trading

**Estado:** propuesto, pendiente de aprobación final antes de tocar código.
**Alcance:** diagnóstico completo del repo + roadmap de refactorización a Clean Architecture, monolito modular, listo para producción 24/7 en VPS propio.
**Fuera de alcance explícito:** Kubernetes, microservicios, deploy automático a producción.

---

## 1. Estado actual del proyecto

100 agentes async (`agents/{scalper,swing,news_reactive,hedger,rl}`) coordinados por un `SwarmOrchestrator` único (`core/orchestrator/orchestrator.py`), que en cada tick de 15s recorre 5 símbolos, pide estado de mercado (`MarketFeed`, yfinance) y noticias (`NewsFeed`), resuelve TP/SL contra el broker (`BrokerInterface` → `IBKRBroker` en modo offline simulado hoy), despacha los agentes de ese símbolo concurrentemente, valida cada propuesta de orden contra `RiskEngine`, y persiste trades/snapshots vía `AsyncRepository` (SQLAlchemy async, SQLite hoy). El dashboard es FastAPI + WebSocket, sirviendo un frontend estático propio (`dashboard/frontend/index.html`, sin build step, JS vanilla + Chart.js por CDN).

**Fortalezas que se conservan sin cambios:**
- `core/models.py`: dominio puro (dataclasses/enums), cero dependencias de framework. Ya es, de facto, una capa de dominio correcta.
- `BrokerInterface` (ABC) ya desacopla agentes de la implementación concreta del broker.
- `RiskEngine` no importa FastAPI/broker/dashboard — ya es testeable de forma aislada.
- `WebSocketManager` está deliberadamente desacoplado del orchestrator.
- Alembic ya está correctamente cableado a `settings.database_url` con engine async.
- Ya existe scaffolding vacío (`core/events/`, `core/context/`, `core/memory/`, `data/cache/`, `data/normalizer/`, `risk/monitor/`, `risk/rules/`, `tests/integration/`, `tests/backtest/`) — construimos sobre esto, no inventamos carpetas nuevas sin razón.

---

## 2. Problemas encontrados

### 2.1 Bloqueantes de rendimiento (evidencia directa en el código)

| # | Problema | Evidencia | Impacto |
|---|---|---|---|
| P1 | `MarketFeed._fetch_yfinance` llama `yf.download()` (síncrono, I/O de red) **directamente dentro de una corrutina**, sin `to_thread`/executor. | `data/feeds/market_feed.py:44` | Congela el event loop **completo** cada 15s por símbolo — los 100 agentes, el WebSocket y las rutas HTTP dejan de responder mientras dura la descarga. Contradice la regla propia del proyecto de "fully async". |
| P2 | `NewsFeed._fetch_forexfactory` llama `feedparser.parse()` (síncrono) sin executor. | `data/news/news_feed.py:81` | Mismo patrón que P1, hoy de bajo impacto (backend en `"demo"`, guard de 5 min), pero debe corregirse antes de activar esa fuente. |
| P3 | Contención de escritura en SQLite bajo concurrencia — ya observado empíricamente en esta sesión (`swarm_trading.db-journal` de 8.7KB con 100 agentes escribiendo). | runtime observado | Se agrava con más agentes; SQLite no tiene MVCC real. |

### 2.2 Deuda arquitectónica

| # | Problema | Evidencia | Por qué importa |
|---|---|---|---|
| D1 | Estado global mutable como mecanismo de inyección de dependencias. `_orchestrator`/`_repository` son variables de módulo seteadas desde `main.py`. | `dashboard/api/routes.py` | No testeable de forma aislada sin depender de reset manual de globals; impide tener múltiples instancias (ej. multi-cuenta futura). |
| D2 | Encapsulación rota: acceso directo a atributos "privados" (`_agents`, `_risk`) desde 3 módulos distintos. | `dashboard/api/routes.py:142`, `core/mcp_server.py:41-63` | Cualquier cambio interno al Orchestrator puede romper silenciosamente el dashboard o el MCP server. |
| D3 | Dos autoridades de esquema compitiendo: `AsyncRepository.init()` llama `Base.metadata.create_all()` en cada arranque, y también existe Alembic con migraciones versionadas. | `data/historic/repository.py` | En Postgres productivo esto puede enmascarar una migración pendiente o generar divergencia silenciosa de esquema. |
| D4 | `SwarmFactory` imperativo: 5 loops hardcodeados, conteos como constantes de módulo, instanciación directa de clases concretas. | `agents/templates/swarm_factory.py` | Agregar un símbolo, una estrategia o escalar de 100 a 1000 agentes requiere editar código y redeployar. |
| D5 | `Agent` y `Strategy` fusionados: la lógica de señal (ej. thresholds de RSI) vive hardcodeada dentro de cada subclase de `BaseAgent`. | `agents/scalper/scalper_agent.py` y análogos | Cada estrategia nueva = una clase nueva. Viola el objetivo explícito de "agregar estrategia sin tocar el núcleo". |
| D6 | `RLAgent.on_trade_closed()` dispara `_retrain()` (entrenamiento PPO) **dentro del proceso de trading en vivo**. | `agents/rl/rl_agent.py:116-134` | Contradice el requisito de "motor de ejecución exclusivamente". Compite por CPU/memoria con el motor de riesgo en el mismo contenedor. |
| D7 | Selección de broker acoplada a `app_env`: `if app_env == "live": MT5Broker() else: IBKRBroker(offline=True)`. No hay forma de pedir "IBKR live" explícitamente. | `main.py` | Bloquea soportar las 4 combinaciones objetivo (Simulator / IBKR Paper / IBKR Live / MT5) de forma explícita. |
| D8 | `mypy strict=true` y `ruff` están configurados en `pyproject.toml` pero **no instalados** en el entorno — nunca se han ejecutado realmente. | `pyproject.toml` vs. venv actual | El "tipado estricto" es aspiracional; deuda de tipos acumulada sin visibilidad. |
| D9 | Sin CI (`.github/workflows` no existe), sin `.pre-commit-config.yaml`, sin config de coverage. | raíz del repo | Cero red de seguridad automática hoy. |
| D10 | Dashboard sin autenticación — `/swarm/halt` es POST público sin login. | `dashboard/api/routes.py` | Crítico antes de exponer el puerto públicamente o conectar a broker live (dinero real). |
| D11 | Cruft sin trackear en el working tree (`agents/scalper/.permtest`, `swarm_trading.db-journal`). | raíz | Housekeeping. |
| D12 | ~~`.env.example` documentaba `RISK_MAX_DAILY_LOSS_PCT` como si existiera un límite de pérdida diaria, pero `SwarmSettings` no tenía el campo y no había ningún control de pérdida *diaria* real.~~ **Resuelto en Fase 4.5** — ver [ADR-0010](architecture/adr/0010-daily-and-total-loss-halt.md): `risk_max_daily_loss_pct` (15%) y `risk_max_total_loss_pct` (30%, antes 50%) ahora ambos activos por defecto, con halt persistido y reactivación explícita. | `.env.example`, `core/config.py`, `risk/engine/risk_engine.py` | Riesgo cerrado: el guardrail de pérdida diaria que antes se asumía existía ahora existe y está probado. |

---

## 3. Decisiones arquitectónicas

### 3.1 Estructura de carpetas objetivo

```
app/
  domain/                 # entidades + reglas puras — cero imports de framework
    models.py               # = core/models.py actual (ya cumple)
    events.py                # NUEVO — TradeExecuted, PositionOpened, etc.
    strategies/               # NUEVO — Strategy como abstracción, separada de Agent
      base.py                   # Protocol: analyze(market_state) -> Signal | None
      registry.py                # auto-registro de estrategias (plugin system)
      rsi_reversal.py             # lógica hoy embebida en ScalperAgent
      ema_trend.py                  # lógica hoy embebida en SwingAgent
      ...
    risk/                    # risk_engine.py — ya aislado, se mueve tal cual
  application/             # casos de uso / orquestación — el "cómo se conecta todo"
    orchestrator.py
    swarm_composition.py     # reemplaza swarm_factory.py, data-driven
    event_bus.py              # pub/sub in-process, asyncio-nativo
  infrastructure/          # todo lo que toca el mundo exterior
    brokers/                  # BrokerInterface + Simulator/IBKR/MT5
    persistence/               # repository.py + db_models.py + alembic
    feeds/                      # market_feed.py, news_feed.py (con fix de I/O)
    cache/                       # Redis adapter detrás de un Protocol
  interfaces/               # puntos de entrada al sistema
    api/                        # FastAPI routes
    websocket/
    mcp/
  core/                     # config, logging, DI container, arranque
tests/
  unit/
  integration/
  backtest/
docker/
scripts/
```

**Por qué esta forma y no la lista plana original (`agents/`, `strategies/`, `risk/`, `brokers/` como hermanos de nivel superior):** agentes, estrategias y risk engine son lógica de negocio pura — pertenecen a `domain/`, no deben estar al mismo nivel que `api/` o `config/`, que sí son infraestructura/interfaz. Esta separación es lo que hace que `domain/` sea importable y testeable sin arrancar FastAPI, sin Docker, sin nada — la definición operativa de "aislado" que ya pediste para el Risk Engine, extendida a todo el núcleo de negocio.

### 3.2 Agent vs. Strategy

`Agent` (en `application/`) pasa a ser **solo** una unidad de ejecución: posee `capital`, `equity`, `lifecycle`, `status`, `posiciones`, `métricas`. No sabe nada de RSI, EMA ni ADX. Cada `Agent` recibe una `Strategy` inyectada en su constructor; en cada tick, delega: `signal = self.strategy.analyze(market_state, agent_context)`.

`Strategy` (en `domain/strategies/`) es donde vive toda la lógica de trading — pura, sin estado de capital, testeable con un `MarketState` de entrada y una señal esperada de salida, sin necesidad de instanciar un `Agent` completo.

**Consecuencia directa:** "agregar una estrategia nueva" pasa de "crear una subclase de `BaseAgent`" a "crear un archivo en `domain/strategies/` con una clase que implemente el `Protocol`". Cero cambios en `Agent`, `Orchestrator` o `SwarmComposition`.

### 3.3 Plugin system para estrategias

Registro simple por decorador, sin maquinaria de `entry_points`/setuptools (eso sería sobreingeniería para un monolito):

```python
# domain/strategies/registry.py (boceto conceptual, se implementa en Fase 7)
_REGISTRY: dict[str, type[Strategy]] = {}

def register_strategy(key: str):
    def wrapper(cls):
        _REGISTRY[key] = cls
        return cls
    return wrapper
```

Cada módulo de estrategia se autoregistra con `@register_strategy("rsi_reversal")` al importarse. `domain/strategies/__init__.py` descubre los módulos del paquete (`pkgutil.iter_modules`) para que el registro ocurra automáticamente con solo agregar el archivo — sin tocar el orquestador ni un índice manual.

### 3.4 Event Bus

**Decisión explícita: pub/sub in-process, asyncio-nativo — NO una cola de mensajes externa (no Redis Streams, no Kafka) en esta fase.** Un Event Bus no implica infraestructura distribuida; implica desacoplar quién *produce* un evento de quién *reacciona* a él, dentro del mismo proceso. Esto respeta directamente tu restricción de "no sobreingeniería / no microservicios".

Eventos de dominio (`domain/events.py`), dataclasses inmutables: `TradeExecuted`, `PositionOpened`, `PositionClosed`, `RiskTriggered`, `AgentStarted`, `AgentStopped`.

**Por qué importa concretamente:** hoy, `on_trade_closed_callback` (`core/orchestrator/orchestrator.py:172`) hardcodea 4 efectos secundarios en un solo método: notificar al `RiskEngine`, notificar al `Agent`, hacer broadcast por WebSocket, y persistir en la DB. Cualquier efecto nuevo (ej. un contador de Prometheus, un log estructurado, una notificación a Telegram) obliga a editar ese método. Con Event Bus, cada uno de esos 4 side-effects se vuelve un `subscriber` independiente de `TradeExecuted` — agregar el quinto no toca el orchestrator. Esto es Open/Closed aplicado donde más se necesita en el codebase actual.

**Riesgo reconocido y cómo se mitiga:** un event bus mal usado agrega indirección — "¿quién reacciona a este evento?" se vuelve más difícil de rastrear que una llamada directa. Mitigación: el bus es síncrono-en-orden (no fire-and-forget salvo que el propio handler lo pida explícitamente) y todos los subscribers se registran en un único lugar visible (`application/bootstrap.py`), no dispersos.

### 3.5 RL: separación completa entrenamiento/inferencia

- `RLAgent._retrain()` y `_train_and_save()` se **eliminan del proceso de trading**. El agente en producción solo carga (`PPO.load(...)`) y predice.
- `agents/rl/train.py` (ya existe) se formaliza como el único punto de entrada de entrenamiento — se corre offline, en tu Mac o en otra máquina, y produce el mismo artefacto (`{symbol}_ppo.zip`) que hoy se guarda en `settings.rl_model_dir`.
- **Hot-swap sin downtime:** el modelo se identifica por archivo + mtime/hash. Antes de cada `analyze()` (o en un chequeo periódico barato, un `stat()` no una lectura completa), el agente compara el mtime del archivo contra el que tiene cargado en memoria; si cambió, recarga. Esto significa que actualizar un modelo es literalmente `scp modelo_nuevo.zip` al VPS — sin reiniciar el proceso. Se formaliza en Fase 9, pero el requisito de "solo inferencia" se resuelve ya en Fase 0 (ver roadmap).

### 3.6 SwarmFactory → composición data-driven

Reemplaza los 5 loops hardcodeados por una lista de composición (config YAML o tabla en Postgres — se decide en Fase 8 con evidencia de cuál pesa menos operar) con forma `{agent_type, strategy, symbol, count, capital, params}`. `SwarmComposition.build(orchestrator)` itera esa lista y usa el registro de estrategias (3.3) para resolver cada entrada. Pasar de 100 a 1000 agentes, o agregar un símbolo nuevo, se vuelve edición de configuración — cero deploy de código.

### 3.7 Broker: selector explícito

Nueva variable de entorno `BROKER_PROVIDER` (`simulator | ibkr | mt5`) + `BROKER_MODE` (`paper | live`), independientes de `APP_ENV` (que vuelve a significar solo entorno/logging, no elección de broker). `main.py` resuelve el broker con un factory simple basado en esas dos variables — sin conflicto entre "estoy en producción" y "quiero IBKR live".

### 3.8 Redis

Se introduce detrás de `Protocol`s (`CachePort`, `PubSubPort`) en `infrastructure/cache/`. El resto del sistema depende del `Protocol`, nunca de `redis.asyncio` directamente — reemplazar Redis por otra cosa (o por una implementación in-memory en tests) no toca ni una línea fuera de `infrastructure/cache/`.

### 3.9 Observabilidad

- Logging estructurado con `loguru.bind(component=..., agent_id=..., symbol=..., broker=..., request_id=...)` — loguru ya soporta esto nativamente, es cuestión de aplicar el patrón consistentemente.
- `/health` se profundiza: hoy solo devuelve `{"status": "ok"}` sin chequear nada; pasa a verificar conexión a DB y estado del broker.
- `/metrics` nuevo, formato Prometheus (`prometheus-client`), sin instalar Prometheus/Grafana todavía — solo el endpoint.
- Métricas por agente (PnL, equity, drawdown, Sharpe, win rate, trades, latencia de análisis, latencia de ejecución, uso de memoria) se capturan naturalmente **a través del Event Bus** (3.4): cada evento ya lleva timestamps; un subscriber de métricas los agrega sin que `Agent` u `Orchestrator` necesiten saber que existen.

---

## 4. Roadmap completo (fases, con tu aprobación de reordenamiento aplicada)

> Reordené dos cosas respecto a tu propuesta original, ambas aprobadas explícitamente: (a) tooling/CI se mueve antes de Postgres/Docker, para que esas dos migraciones grandes queden protegidas por lint/tests/typecheck desde el principio; (b) la reestructuración mecánica de carpetas se mueve antes del split Agent/Strategy, para construir la lógica nueva directamente en su ubicación final en vez de moverla dos veces.

| Fase | Contenido | Depende de | Estado |
|---|---|---|---|
| **0** | Quick wins de bajo riesgo: desactivar `_retrain()`/`_train_and_save()` del proceso de producción (requisito "solo inferencia"), hot-swap de modelo por mtime, limpiar cruft (`.permtest`, `.gitignore` para `.db-journal`). | — | ✅ Completada — [ADR-0001](docs/architecture/adr/0001-rl-inference-only-in-production.md) |
| **1** | Corregir I/O bloqueante: `MarketFeed._fetch_yfinance`, `NewsFeed._fetch_forexfactory`, `IBKRBroker.connect`, y todo `MT5Broker` a `asyncio.to_thread`/executor. Test de regresión agregado. | — | ✅ Completada — [ADR-0002](docs/architecture/adr/0002-async-io-blocking-calls-must-use-executor.md) |
| **2** | Tooling & CI: `ruff`+`mypy` (strict, cero errores — ver [ADR-0004](docs/architecture/adr/0004-mypy-strict-baseline.md)), `pytest-cov`, `pre-commit`, GitHub Actions (lint + typecheck + test + alembic-check + docker-build). | 0, 1 | ✅ Completada — [ADR-0004](docs/architecture/adr/0004-mypy-strict-baseline.md), [ADR-0005](docs/architecture/adr/0005-dependency-cleanup-and-dev-runtime-split.md), [ADR-0006](docs/architecture/adr/0006-per-symbol-error-isolation-in-orchestrator-loop.md) |
| **3** | Migración completa SQLite → PostgreSQL. Eliminar `create_all()`; Alembic como única autoridad de esquema. | 2 | ✅ Completada y mergeada — [PR #2](https://github.com/erickgarciaoj-blip/swarm_trading/pull/2) (commit de merge `3bf8884`), [ADR-0008](docs/architecture/adr/0008-postgresql-alembic-schema-authority.md) |
| **4** | Dockerización completa: `app` + `postgres` + `redis` + `nginx` (preparado para HTTPS/auth futura, sin activar todavía), healthchecks, volúmenes persistentes, `.env` para todo. `docker compose up -d` como único comando de arranque. | 3 | ✅ Completada y mergeada — [PR #3](https://github.com/erickgarciaoj-blip/swarm_trading/pull/3) (commit de merge `a718d47`), [ADR-0009](docs/architecture/adr/0009-docker-stack-gate-liveness-readiness.md). Imagen final optimizada a PyTorch CPU-only durante la propia verificación (5.68GB → 1.68GB). |
| **5** | Redis desacoplado detrás de `Protocol`s (cache + pub/sub, sin lógica de negocio acoplada). | 4 | Pendiente |
| **6** | Reestructuración mecánica de carpetas → `domain/application/infrastructure/interfaces/core`. Movimiento 1:1, imports actualizados, **cero cambio de comportamiento**, suite verde antes y después. | 5 | Pendiente |
| **7** | Split Agent/Strategy + Event Bus + plugin system de estrategias (las tres piezas se construyen juntas, están acopladas por diseño). | 6 | Pendiente |
| **8** | `SwarmFactory` → composición data-driven usando el registro de estrategias de la Fase 7. | 7 | Pendiente |
| **9** | Observabilidad: logging estructurado, `/metrics` (Prometheus), métricas por agente vía Event Bus. | 7 | Pendiente |
| **10** | Integración IBKR Paper + selector de broker explícito (`BROKER_PROVIDER`/`BROKER_MODE`), camino documentado a IBKR Live y MT5. | 4, 6 | Pendiente |

Cada fase termina con: (1) explicación de qué se hizo y por qué, (2) verificación manual de que el sistema sigue corriendo, (3) suite de tests corrida y en verde, (4) confirmación explícita antes de pasar a la siguiente fase.

---

## 5. Riesgos

- **Migración Postgres (Fase 3):** riesgo de pérdida de datos si no se planifica la migración de los datos existentes en `swarm_trading.db`. Mitigación: dado que hoy son datos de paper trading en desarrollo, no de producción, se trata como *no crítico migrar el histórico* — se arranca Postgres limpio y el histórico SQLite queda archivado como backup. Si en el futuro esto corre con dinero real, esta decisión cambia y sí se planifica migración de datos con downtime controlado.
- **Reestructuración de carpetas (Fase 6):** riesgo de romper imports de forma silenciosa si no se corre la suite completa después de cada movimiento de archivo. Mitigación: se hace un módulo a la vez, con `pytest` verde entre cada movimiento, no un `git mv` masivo de una sola vez.
- **Event Bus (Fase 7):** riesgo de sobre-indirección (dificulta rastrear "qué reacciona a qué"). Mitigación: bus síncrono-en-proceso, registro de subscribers centralizado y visible, no distribuido.
- **Split Agent/Strategy (Fase 7):** riesgo de cambiar sutilmente el comportamiento de una estrategia al extraerla (ej. un threshold que dependía de un side-effect de `__init__` de la subclase). Mitigación: tests de paridad de comportamiento antes/después por cada estrategia migrada.
- **Alcance general:** es un refactor que toca casi todos los archivos del repo a lo largo de varias fases. Mitigación acordada: nunca big-bang, siempre fase por fase, siempre con el sistema funcional al final de cada una.

---

## 6. Impacto esperado

- **Rendimiento:** eliminar el bloqueo del event loop (Fase 1) es el cambio de mayor impacto/menor riesgo de todo el roadmap — resuelve una congelación total del sistema cada 15 segundos.
- **Confiabilidad 24/7:** Postgres + Docker + healthchecks + restart policies (Fases 3-4) es lo que realmente habilita "correr 24/7" de forma seria, más allá de un `nohup`.
- **Velocidad de desarrollo futuro:** el split Agent/Strategy + composición data-driven (Fases 7-8) es lo que convierte "agregar una estrategia" o "escalar a miles de agentes" de un cambio de código a un cambio de configuración — el apalancamiento más alto de todo el plan para tu horizonte de 3-5 años.
- **Confianza en cada cambio:** CI + tipado estricto (Fase 2) puesto temprano significa que cada fase posterior tiene una red de seguridad automática, no solo verificación manual.

---

## 7. Próximo paso

Con esto aprobado, empezamos por la **Fase 0** (desactivar retraining de RL en producción + limpieza) seguida de la **Fase 1** (fix del I/O bloqueante) — ambas aisladas, de bajo riesgo, y ya con tu aprobación explícita de contenido. Confirmo contigo antes de cada fase siguiente, como acordamos.
