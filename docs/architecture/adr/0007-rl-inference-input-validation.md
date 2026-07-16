# ADR-0007: Validación de entrada en el límite de inferencia RL

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Fase 2 (cierre)

## Contexto

Durante la verificación en vivo de Fase 2 se observó `RL_OIL_*` fallando con `Categorical(logits: ...) invalid values: tensor([[nan, nan, nan]])`. Causa raíz: cuando `MarketFeed` recibe muy pocas barras de Yahoo (el mismo problema de datos de ADR-0006), los indicadores calculados con ventanas móviles — `rsi_14`/`atr_14` vía `.rolling(14)` — quedan en `NaN` en vez de faltar del diccionario. `RLAgent.analyze()` no validaba nada antes de construir el vector de observación ni antes de llamar `model.predict()`: un `NaN` viajaba sin control desde el indicador hasta la red neuronal, donde PyTorch lo rechaza de forma tardía y menos informativa (aísla el fallo, per ADR-0006, pero no explica qué feature específica estaba mal ni con qué frecuencia pasa).

## Alternativas consideradas

1. **Imputar valores por defecto (ej. 0.0) para features `NaN`/`Inf`** — descartada explícitamente: una señal de trading construida sobre datos que sabemos inválidos es peor que no operar ese tick. Imputar silenciosamente además oculta el problema real (datos insuficientes) en vez de exponerlo.
2. **Dejar que PyTorch/`Categorical` siga rechazando el `NaN` como hasta ahora** — descartada: el error solo es visible en logs no estructurados, sin símbolo/agente/feature explícitos, y sin ningún contador para saber si es un evento raro o algo que pasa constantemente.
3. **Validación explícita en 3 puertas antes de `model.predict()`, sin imputación, con log estructurado y contador** (la elegida).

## Decisión

`RLAgent.analyze()` valida en tres puertas, cada una con su propia razón de rechazo y contador:

1. **Historial mínimo** (`MIN_CANDLES_REQUIRED = 14`, alineado con la ventana de `rolling(14)` en `MarketFeed._compute_indicators`) — rechaza antes de siquiera leer los indicadores, es la puerta más barata.
2. **`atr_14`/`close` finitos** — estos dos alimentan `sl_price`/`tp_price` *fuera* del vector de observación; un `NaN` aquí llegaría al broker como precio de orden inválido, no solo como una mala entrada al modelo.
3. **Vector de observación completo** (`agents/rl/features.py::validate_observation`) — verifica forma y que las 6 features sean finitas, nombrando la(s) feature(s) específica(s) que fallaron.

Cualquier rechazo: incrementa un contador (`insufficient_history_count` / `invalid_observation_count`, atributos públicos del agente — se convierten en contadores de Prometheus reales cuando Fase 9 conecte `/metrics`, sin cambiar esta interfaz), emite un log estructurado (`logger.bind(agent_id=..., symbol=..., ...)`) y retorna `None` (sin operar ese tick) — nunca imputa.

## Ventajas

- Ningún `NaN`/`Inf` puede llegar a `model.predict()` ni a `sl_price`/`tp_price` de una orden real.
- Cada rechazo queda atribuido a una causa específica y a una feature específica — se puede diagnosticar sin adivinar.
- El contador permite distinguir "pasó una vez" de "está pasando todo el tiempo" sin necesitar todavía la infraestructura completa de Fase 9.
- `validate_observation()` vive en `agents/rl/features.py`, el mismo módulo ya compartido entre inferencia en vivo (`RLAgent`) y entrenamiento (`SwarmTradingEnv`) — disponible para ambos caminos sin duplicar lógica.
- Recuperación automática verificada: un tick inválido no deja al agente "trabado" — el siguiente tick con datos válidos vuelve a operar con normalidad (test de regresión dedicado).

## Desventajas

- Un símbolo con datos crónicamente insuficientes (no solo temporalmente, ej. un ticker realmente delistado) generará warnings indefinidamente sin escalar a una alerta — mismo trade-off ya aceptado en ADR-0003/ADR-0006, se resuelve con observabilidad real en Fase 9.
- `MIN_CANDLES_REQUIRED = 14` está acoplado al valor hardcodeado del rolling window en `MarketFeed._compute_indicators` — si ese valor cambia ahí, hay que recordar actualizarlo aquí también (no hay una única fuente de verdad compartida todavía; se resuelve naturalmente cuando la composición de estrategias se vuelva data-driven en Fase 8).

## Consecuencias

- `agents/rl/features.py`: nueva función `validate_observation(obs) -> str | None`.
- `agents/rl/rl_agent.py`: `MIN_CANDLES_REQUIRED`, tres puertas de validación en `analyze()`, dos contadores públicos.
- Tests nuevos: `test_rl_features.py` (5 tests directos de `validate_observation`), `test_rl_agent.py` (6 tests: válido, NaN en `atr`, NaN en una EMA, infinito, historial insuficiente, recuperación automática).
- `tests/unit/test_rl_agent.py::_state()` ahora genera `MIN_CANDLES_REQUIRED` velas por defecto (antes generaba 1) — los tests existentes que dependían de una sola vela se actualizaron para reflejar el nuevo gate real, no para evadirlo.
