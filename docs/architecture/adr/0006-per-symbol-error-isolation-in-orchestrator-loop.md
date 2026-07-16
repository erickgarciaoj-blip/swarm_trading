# ADR-0006: Aislamiento de errores por símbolo en el loop del orchestrator

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Fase 2 (verificación en vivo)

## Contexto

Durante la verificación en vivo de los cambios de Fase 2 (dependencias actualizadas, `StrEnum`, etc.), el host relanzado dejó de operar por completo: 0 trades nuevos, `total_trades` congelado, cada tick repitiendo el mismo error.

Investigación: `yfinance` (en cualquiera de sus dos versiones probadas, la vieja 0.2.40 y la nueva 1.5.1 — se confirmó explícitamente que **no es una regresión de la actualización de dependencias**) no tenía barras intradía de 1 minuto disponibles para los tickers de futuros `GC=F` (oro/XAUUSD) y `CL=F` (petróleo/OIL) en ese momento — un problema real y externo del proveedor de datos, no del código.

El bug real estaba en `SwarmOrchestrator.run()`: el `for symbol in Symbol:` no tenía manejo de errores por iteración. Cuando `MarketFeed.get_state()` lanzaba una excepción para XAUUSD (el primer símbolo del enum), esa excepción se propagaba hasta el `except Exception` externo del loop principal, que loguea, duerme 5s, y **reinicia el tick completo desde el primer símbolo**. Como XAUUSD es siempre el primero en fallar, el swarm nunca lograba avanzar a PLTR/NAS100/US100/OIL — quedaba atascado reintentando el mismo símbolo roto indefinidamente, con el resto del swarm completamente parado.

## Alternativas consideradas

1. **No hacer nada — es un problema de datos de Yahoo, no de código** — descartado: aunque la causa raíz externa es real, el código tiene una responsabilidad de aislamiento de fallos que no estaba cumpliendo. Un solo símbolo con datos temporalmente no disponibles no debería poder tumbar el 100% del swarm.
2. **Reintentar con backoff solo para el símbolo que falla, dentro del mismo tick** — más sofisticado, pero innecesario: el loop ya tiene un tick cada 15s: cada iteración completa del `while` YA es un reintento natural. Agregar backoff anidado sería complejidad sin beneficio real.
3. **Try/except por símbolo dentro del loop, seguir con los demás** (la elegida) — mínimo cambio posible que resuelve el problema completo.

## Decisión

`SwarmOrchestrator.run()` ahora envuelve cada símbolo individualmente:

```python
for symbol in Symbol:
    try:
        await self._process_symbol(symbol)
    except Exception as exc:
        logger.warning(f"[Swarm] Skipping {symbol.value} this tick: {exc}")
```

El cuerpo que antes vivía inline en el loop (`get_state`, `get_upcoming`, `check_tp_sl`, despacho de agentes) se extrajo a un método propio `_process_symbol(symbol)`, para que el try/except tenga una unidad clara que envolver y el método sea testeable de forma aislada.

## Ventajas

- Verificado en vivo inmediatamente después del fix: con XAUUSD y OIL ambos fallando por el mismo problema de datos, PLTR/NAS100/US100 siguieron operando con normalidad — se vieron órdenes nuevas enviadas para múltiples agentes en la misma corrida donde antes el swarm entero estaba parado.
- Cambio mínimo y de bajo riesgo: no toca la lógica de negocio de ningún agente, broker o risk engine — solo el aislamiento de errores del loop de orquestación.
- Test de regresión (`test_one_broken_symbol_does_not_block_the_others_in_the_same_tick`) verificado contra el código viejo: falla exactamente como se esperaba (solo XAUUSD se intenta, los otros 4 símbolos nunca se llaman) antes del fix, y pasa después.

## Desventajas

- Un símbolo roto de forma persistente (ej. un ticker realmente delisted, no solo temporalmente sin datos) va a generar un warning en cada tick indefinidamente, sin backoff ni alerta — aceptable por ahora; con observabilidad (Fase 9) esto se vuelve una métrica/alerta en vez de solo ruido de logs, mismo razonamiento que ADR-0003 para el hot-swap de modelos RL.

## Consecuencias

- `core/orchestrator/orchestrator.py`: nuevo método `_process_symbol()`, `run()` con aislamiento por símbolo.
- Nuevo test en `tests/unit/test_orchestrator_broadcast.py` con un `_FlakyMarketFeed` que reproduce el escenario real observado (no una excepción hipotética inventada).
- Confirma, de paso, que la separación Agent/Strategy y Event Bus planeadas para Fase 7 tienen más terreno fértil del anticipado: este mismo patrón de "aislar el fallo de una unidad para no tumbar el resto" es exactamente lo que un Event Bus con subscribers independientes facilita de forma más general.
