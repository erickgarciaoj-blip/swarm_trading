# ADR-0001: RL — solo inferencia en producción, entrenamiento offline separado

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Fase 0

## Contexto

`RLAgent` (`agents/rl/rl_agent.py`) usa Stable-Baselines3 (PPO) para decidir señales de trading. Antes de esta decisión, `RLAgent.on_trade_closed()` disparaba `_retrain()` cada `N` trades (`settings.rl_retrain_every_n_trades`), que llamaba `model.learn(...)` **dentro del mismo proceso** que ejecuta el motor de riesgo y envía órdenes al broker. Aunque el entrenamiento corría en un worker thread (`asyncio.to_thread`) y por lo tanto no bloqueaba el event loop, seguía siendo trabajo CPU-bound compitiendo por recursos (CPU, memoria, GIL) con el motor de ejecución en vivo, dentro del mismo contenedor.

El requisito explícito del proyecto es: "el servidor de producción será exclusivamente un motor de ejecución" — el entrenamiento debe vivir en otra máquina, como proceso completamente separado y offline.

## Alternativas consideradas

1. **Mantener el retraining in-process**, aislado en un thread — ya era el estado antes de esta decisión. Descartada: contradice el requisito explícito de separar completamente entrenamiento e inferencia, y expone al motor de ejecución a spikes de latencia/memoria impredecibles durante el entrenamiento.
2. **Mover el entrenamiento a un proceso separado dentro del mismo contenedor/VPS** (ej. un segundo contenedor Docker que entrena y escribe al volumen compartido). Válida a futuro, pero prematura ahora — agrega infraestructura (otro servicio en compose, coordinación de cuándo entrenar) sin necesidad inmediata, dado que hoy el entrenamiento se hace manualmente y de forma esporádica.
3. **Entrenamiento 100% offline, en otra máquina, con hot-swap de modelo por archivo** (la elegida). El motor de producción solo lee `{symbol}_ppo.zip`; nunca escribe uno.

## Decisión

`RLAgent` pasa a ser inference-only: se eliminan `_retrain()` y `_train_and_save()` de `agents/rl/rl_agent.py`. El único punto de entrada de entrenamiento es el script ya existente `agents/rl/train.py`, ejecutado manualmente (o por cron, a futuro) en la máquina de desarrollo o en un servidor dedicado — nunca dentro del proceso de `main.py`.

Para permitir actualizar un modelo sin downtime, `RLAgent._ensure_model()` compara el `mtime` del archivo de checkpoint en cada tick (`Path.stat()`, una syscall local, no requiere executor — ver ADR-0002 sobre el criterio de cuándo sí hace falta) y recarga automáticamente si detecta un archivo más nuevo. Actualizar un modelo en producción se reduce a copiar el archivo nuevo (`scp`) al VPS.

## Ventajas

- Separación completa de responsabilidades: el proceso de trading nunca hace trabajo CPU-bound no relacionado con ejecutar el swarm.
- Imagen Docker del motor de producción no necesita el pipeline de entrenamiento completo corriendo en runtime (aunque `torch`/`stable-baselines3` siguen siendo dependencias de inferencia).
- Actualizar un modelo no requiere reiniciar el swarm — cero downtime.
- `agents/rl/train.py` ya existía y ya hacía exactamente lo necesario; no hubo que construir infraestructura de entrenamiento nueva.

## Desventajas

- El entrenamiento deja de reaccionar automáticamente a cada N trades — ahora es un proceso manual/programado por separado, que alguien tiene que recordar ejecutar y desplegar.
- El hot-swap por `mtime` asume relojes de archivo consistentes; si el archivo se sube con una herramienta que no preserva mtime correctamente, el reload podría no dispararse. Mitigación simple si aparece en la práctica: forzar `touch` después de cada `scp`.

## Consecuencias

- `core/config.py` pierde `rl_retrain_every_n_trades` y `rl_incremental_timesteps` (configuración muerta tras este cambio).
- `tests/unit/test_rl_agent.py` reemplaza su test de retrain-trigger por un test de hot-swap (`test_ensure_model_hot_swaps_on_file_change`) y un test que confirma que `on_trade_closed` solo registra el trade.
- Queda pendiente, fuera de esta fase: automatizar el pipeline de entrenamiento offline (ej. un job programado en la máquina de desarrollo que entrena y hace `scp` del checkpoint al VPS) — no es necesario todavía, se evalúa cuando haya más de una estrategia RL en uso real.
