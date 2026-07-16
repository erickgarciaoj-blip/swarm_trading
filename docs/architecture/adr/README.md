# Architecture Decision Records

Registro de decisiones arquitectónicas relevantes del proyecto Swarm Trading. Cada ADR documenta el contexto, las alternativas consideradas, la decisión tomada, sus ventajas/desventajas y las consecuencias — no solo el "qué", sino el "por qué".

Se escriben de forma incremental, fase por fase (ver `ARCHITECTURE_REVIEW.md` en la raíz del repo para el roadmap completo), no todas de una vez al inicio del proyecto.

## Índice

| # | Título | Fase | Estado |
|---|---|---|---|
| [0001](0001-rl-inference-only-in-production.md) | RL: solo inferencia en producción, entrenamiento offline separado | Fase 0 | Aceptado |
| [0002](0002-async-io-blocking-calls-must-use-executor.md) | I/O bloqueante debe correr en executor, nunca directo en una corrutina | Fase 1 | Aceptado |
| [0003](0003-rl-model-hotswap-failure-isolation.md) | Aislamiento de fallos en el hot-swap de modelos RL (carga corrupta, reintentos) | Revisión final Fase 0/1 | Aceptado |
| [0004](0004-mypy-strict-baseline.md) | mypy strict como baseline real (cero errores), no un plan de ratchet | Fase 2 | Aceptado |
| [0005](0005-dependency-cleanup-and-dev-runtime-split.md) | Limpieza de dependencias fantasma + separación runtime/dev (requirements-dev.txt) | Fase 2 | Aceptado |
| [0006](0006-per-symbol-error-isolation-in-orchestrator-loop.md) | Aislamiento de errores por símbolo en el loop del orchestrator (bug encontrado en verificación en vivo) | Fase 2 | Aceptado |
| [0007](0007-rl-inference-input-validation.md) | Validación de entrada (NaN/Inf/historial mínimo) en el límite de inferencia RL, sin imputación | Fase 2 (cierre) | Aceptado |
| [0008](0008-postgresql-alembic-schema-authority.md) | PostgreSQL como base de datos de runtime, Alembic como única autoridad de esquema (fin de `create_all()`) | Fase 3 | Aceptado |

## Formato

Cada ADR usa la plantilla en [`0000-template.md`](0000-template.md): Contexto, Alternativas consideradas, Decisión, Ventajas, Desventajas, Consecuencias.

## Cuándo escribir un ADR

Cuando una decisión: (a) es difícil o costosa de revertir, (b) afecta a múltiples módulos/capas, o (c) alguien razonablemente podría preguntar "¿por qué se hizo así y no de otra forma?" dentro de 6 meses.
