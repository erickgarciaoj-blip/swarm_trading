# ADR-0010: Halt diario (15%) y halt total (30%) en RiskEngine, con persistencia y reactivación explícita

**Estado:** Aceptado
**Fecha:** 2026-07-20
**Fase del roadmap:** Fase 4.5 (fuera de la numeración original de `ARCHITECTURE_REVIEW.md` — cierra la deuda D12 encontrada durante la verificación de Fase 4)

## Contexto

D12 en `ARCHITECTURE_REVIEW.md` documentó que `.env.example` llegó a listar `RISK_MAX_DAILY_LOSS_PCT` como si existiera un límite de pérdida diaria, pero `SwarmSettings` nunca tuvo ese campo — la variable se leía y se descartaba en silencio (`extra="ignore"`), y `RiskEngine.validate()` tenía un comentario explícito: "no daily-loss halt by design". Solo existía `risk_max_total_loss_pct` (50% por defecto), y ese halt era puramente en memoria: un reinicio del proceso lo borraba sin dejar rastro.

Se decidió cerrar esta deuda ahora, con semántica explícita: límite diario 15%, límite total 30%, ambos con persistencia real y reglas claras de reactivación — no otro parche silencioso.

## Alternativas consideradas

1. **Mantener el diseño actual (solo total, en memoria)** — descartada: es exactamente la brecha que D12 documentó como riesgo real de sobreestimar la protección FTMO-style del swarm.
2. **Halt total por-agente** (usando `AgentMetrics.initial_capital`, que ya existe como referencia en el chequeo `AGENT_MAX_DD`) — descartada. El texto de la decisión de negocio decía "la pérdida total máxima debe permanecer en 30%" (un concepto ya existente, singular) y añadía la salvedad explícita de reusar "una referencia equivalente [ya] claramente definida" si existía. `RiskEngine` ya tenía exactamente esa referencia a nivel swarm (`_total_pnl` vs. `swarm_total_capital_usd`) desde antes de este cambio — así que se reusa esa, sin migrar a un esquema de 100 filas (una por agente) que habría ampliado significativamente el alcance sin pedido explícito. `AGENT_MAX_DD` (el chequeo per-agente que ya existía) se deja intacto, solo hereda el nuevo umbral de 30% al leer el mismo `risk_max_total_loss_pct`.
3. **Escala entera 0-100 para los nuevos campos** (`risk_max_daily_loss_pct=15`) — descartada tras confirmar con el usuario: rompería en silencio el chequeo `AGENT_MAX_DD` existente, que compara un drawdown fraccional (0-1) contra `risk_max_total_loss_pct`. Se usa fracción 0-1 en ambos campos nuevos, consistente con los 3 campos `risk_*_pct` que ya existían (`risk_min_entry_pct`, `risk_max_entry_pct`, `risk_max_total_loss_pct`).
4. **Dos flags de halt completamente independientes (daily / manual / total), cada uno con su propio "sticky")** — descartada por complejidad innecesaria. Se usan dos flags: uno *sticky* que cubre tanto el halt manual como el halt total (ambos requieren `resume()` explícito, ambos sobreviven un reinicio), y uno *diario* que solo se limpia en el rollover de día UTC. Esto es lo mínimo necesario para expresar las dos semánticas de reactivación pedidas.
5. **Persistencia dentro del propio `RiskEngine`** (que importe `AsyncRepository` directamente) — descartada: rompería la separación de capas ya establecida (`risk/engine/` no depende de `data/historic/`). En su lugar, `RiskEngine` expone `snapshot_state()`/`restore_state()`/`consume_dirty()` (puro, sin I/O) y `SwarmOrchestrator` decide cuándo persistir — el mismo patrón de responsabilidades que ya existía para `save_trade`.
6. **La elegida**: dos halts en `RiskEngine` (diario 15%, total 30% sticky), persistidos en una tabla `risk_state` de una sola fila, cargados explícitamente al arrancar (`SwarmOrchestrator.restore_risk_state()`, llamado sin try/except en `main.py`).

## Decisión

### Semántica del halt diario (`RISK_MAX_DAILY_LOSS_PCT`, 15%)

- Referencia: equity total del swarm (`realized_equity + floating_pnl`, la misma fórmula que `get_swarm_summary`) al primer tick de cada día UTC. Se recalcula en `SwarmOrchestrator._compute_total_equity()` y se pasa a `RiskEngine.update_daily_tracking()` una vez por tick (cada 15s), después de refrescar `_floating_pnl`.
- Incluye PnL realizado del día, PnL no realizado de posiciones abiertas, y comisión cuando esté disponible (`ExecutedTrade.commission`, campo nuevo, default 0.0 — ningún broker lo puebla todavía, pero `BaseAgent.record_trade` ya la neta contra `equity`, así que fluye automáticamente al total_equity usado aquí en cuanto un adapter la reporte).
- Al alcanzar o superar 15% de caída respecto a la referencia diaria: `RiskEngine` entra en halt, `validate()` rechaza toda orden nueva con `SWARM_HALTED: cause=daily_loss`.
- El límite diario se reinicia únicamente al detectar un cambio de fecha UTC (`now.date() != daily_reference_date`) dentro de `update_daily_tracking()` — nunca por `resume()`. Esa transición (nueva referencia + halt limpiado si estaba activo) queda marcada como "dirty" y se persiste.

### Semántica del halt total (`RISK_MAX_TOTAL_LOSS_PCT`, 30%, antes 50%)

- Referencia: PnL realizado acumulado del swarm (`_total_pnl`) vs. `swarm_total_capital_usd` — la referencia que ya existía antes de este cambio (ver alternativa 2).
- Al alcanzar o superar 30%: `RiskEngine` entra en halt *sticky*, mismo mecanismo que el halt manual (`halt()`/`resume()`). No se limpia automáticamente al cambiar de día UTC — solo `resume()` (acción administrativa explícita, vía MCP `resume_swarm` o `POST /swarm/resume`) lo hace.
- `resume()` limpia el flag, no borra `_total_pnl` — si la pérdida subyacente sigue por debajo del umbral, la siguiente evaluación vuelve a halt inmediatamente. Esto es intencional: `resume()` es un acknowledgment del operador, no una forma de seguir operando con la misma cuenta reventada sin que la condición real mejore.
- Cuando ambos halts están activos a la vez, `halt_cause` reporta `"total_loss"` (prioridad total sobre diario) — implementado evaluando el chequeo de pérdida total *antes* del corte por halt diario dentro de `validate()`, no solo al principio de la función.

### Persistencia y comportamiento ante reinicio

- Tabla nueva `risk_state` (fila única, `id=1`): `daily_reference_equity`, `daily_reference_date`, `daily_halted`(+`_at`+`_observed_value`), `sticky_halted`, `halt_cause`, `halted_at`, `halt_observed_value`. Migración `alembic/versions/41cde2ea07b5_add_risk_state_table.py`, generada con `alembic revision --autogenerate` y verificada con `upgrade head` → `downgrade -1` → `upgrade head` (mismo patrón que CI's `postgres-integration`).
- `RiskEngine.consume_dirty()` es la única fuente de verdad de "hay algo que persistir" — se marca `True` en cada transición de halt (activar, limpiar por rollover, `resume()`). `SwarmOrchestrator._persist_risk_state_if_dirty()` lo consume una vez por tick, después de `update_daily_tracking()`, con `_fire_and_forget` (mismo patrón que `save_trade`).
- Halts manuales (`halt()`/`resume()` vía MCP/dashboard) **no** disparan una persistencia inmediata — quedan cubiertos por el mismo chequeo de "dirty" del siguiente tick (hasta 15s de latencia). Fuera de alcance de este ADR endurecer esto: el requisito explícito de "no reactivación silenciosa tras reinicio" es sobre los dos halts por pérdida, ambos ya persistidos de forma inmediata porque se disparan dentro del propio loop de ticks.
- `AsyncRepository.load_risk_state()` es la única lectura de este repositorio que **no** es fail-soft — lanza en vez de devolver `None` ante un fallo de DB, a propósito: un reinicio nunca debe interpretar "no pude leer" como "no había halt". `SwarmOrchestrator.restore_risk_state()` se llama sin `try/except` en `main.py`, antes de `build_swarm`/`orch.run()` — un fallo aquí aborta el arranque en vez de arrancar el swarm sin saber si debía estar detenido. Con `repository=None` (sin DB configurada), el halt simplemente no sobrevive reinicios — consistente con la filosofía existente de "la DB es opcional" del resto del proyecto.

### Concurrencia

`validate()`, `on_trade_closed()` y `update_daily_tracking()` no hacen ningún `await` internamente. Bajo el scheduler cooperativo de un solo hilo de asyncio, eso los hace atómicos entre sí frente a otras corrutinas (incluyendo el `asyncio.gather(*[_process_agent(...) ...])` concurrente de `SwarmOrchestrator._process_symbol`) sin necesidad de un `asyncio.Lock` explícito — el mismo argumento (implícito, no documentado) que ya sostenía la sección de estado mutable de `RiskEngine` antes de este cambio. Si en el futuro cualquiera de estos métodos necesita un `await` interno, esta garantía se rompe y hace falta un lock explícito.

### Configuración

`RISK_MAX_DAILY_LOSS_PCT=0.15`, `RISK_MAX_TOTAL_LOSS_PCT=0.30` — ambos activos por defecto en `core/config.py`, sin flag de opt-in. Un `model_validator` nuevo (`_valid_risk_loss_limits`) exige `0 < valor < 1` en ambos y `risk_max_daily_loss_pct <= risk_max_total_loss_pct`, fallando el arranque con `ValueError` si se viola (mismo patrón que `_no_silent_sqlite_in_production`).

## Ventajas

- Cierra D12 con el comportamiento que su hallazgo asumía que ya existía.
- El halt total pasa de "en memoria, se pierde en cualquier reinicio" a "sobrevive reinicios, requiere reconocimiento explícito del operador" — la brecha de seguridad más seria que tenía el diseño anterior.
- Reutiliza patrones ya existentes en el código (fail-soft en escrituras, `_fire_and_forget`, upsert dialecto-específico, single-row table) en vez de introducir mecanismos nuevos.
- `AGENT_MAX_DD` (per-agente) sigue funcionando sin tocarse, solo hereda el umbral más estricto.

## Desventajas

- Un halt manual (no por pérdida) puede tardar hasta 15s (un tick) en persistirse — ver nota en "Persistencia" arriba. Aceptado explícitamente, fuera del alcance pedido.
- `resume()` sobre un halt total cuya causa subyacente no mejoró vuelve a halt en la siguiente evaluación — puede sorprender a un operador que espere que `resume()` "arregle" la situación por sí solo. Documentado aquí y en el docstring de `resume()`.
- El halt total sigue sin usar equity flotante (solo PnL realizado), a diferencia del diario — asimetría intencional (ver alternativa 2: se reusa la referencia ya existente, no se amplía), pero es una inconsistencia real entre ambos halts que vale la pena revisar en una fase futura si se decide que el total también debe reaccionar a pérdidas no realizadas.
- No hay backfill para despliegues que ya tenían filas en `swarm_snapshots`/`trades` antes de esta migración — `risk_state` arranca vacía (`daily_reference_equity=None`), así que el primer tick tras desplegar este cambio establece la referencia diaria desde la equity de ese momento, no desde el inicio del día real.

## Consecuencias

- `core/config.py`: `risk_max_daily_loss_pct` nuevo (0.15), `risk_max_total_loss_pct` cambia su default de 0.50 a 0.30, nuevo `model_validator`.
- `core/models.py`: `ExecutedTrade.commission: float = 0.0` nuevo.
- `agents/base/base_agent.py`: `record_trade` neta la comisión contra `equity`.
- `risk/engine/risk_engine.py`: reescrito — `RiskStateSnapshot`, dos halts, `update_daily_tracking()`, `consume_dirty()`/`snapshot_state()`/`restore_state()`. `reset_daily()` se mantiene (compatibilidad de API) pero ya no des-halta nada, solo resetea el acumulador `daily_pnl` de display.
- `data/historic/db_models.py`: `RiskStateORM` nuevo. `data/historic/repository.py`: `save_risk_state()`/`load_risk_state()` nuevos. `alembic/versions/41cde2ea07b5_add_risk_state_table.py` nuevo, reversibilidad verificada.
- `core/orchestrator/orchestrator.py`: `restore_risk_state()`, `_persist_risk_state_if_dirty()`, `_compute_total_equity()` nuevos; `run()` llama a `update_daily_tracking()` + persistencia cada tick; `get_swarm_summary()` expone `halt_cause`.
- `main.py`: llama a `orch.restore_risk_state()` sin `try/except`, antes de construir el swarm.
- `.env.example`: documenta ambas variables con su semántica.
- `tests/unit/test_risk_engine.py`: reescrito — se elimina `test_large_daily_loss_does_not_halt_the_swarm` (afirmaba la ausencia intencional del halt diario) y `test_total_loss_limit_halts_at_50_percent` se actualiza a 30%; se agregan casos para ambos umbrales (por debajo/exacto/por encima), PnL realizado+no realizado, posición abierta cruzando el límite, concurrencia, snapshot/restore, reinicio, reset UTC, halt total no reseteado por cambio de día, y prioridad total-sobre-diario.
- `tests/unit/test_config.py` nuevo: valida los límites de rango y la relación diario ≤ total.
- `tests/unit/test_repository.py`: casos nuevos para `save_risk_state`/`load_risk_state`, incluyendo que `load_risk_state` sí lanza ante un fallo de DB (a diferencia del resto de lecturas de este repositorio).
- `tests/integration/test_postgres_repository.py`: el set de tablas esperado ahora incluye `risk_state`.
- **Deuda técnica pendiente, explícita**: el halt total sigue sin considerar equity flotante (ver Desventajas); un halt manual puede tardar hasta un tick en persistirse.
