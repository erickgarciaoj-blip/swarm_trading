# ADR-0004: mypy strict como baseline real, no un plan de ratchet

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Fase 2

## Contexto

`pyproject.toml` declaraba `mypy strict = true` desde el commit inicial, pero mypy nunca había estado instalado en el entorno — nadie lo había corrido nunca. La expectativa inicial (ver `ARCHITECTURE_REVIEW.md` §5) era que la primera corrida de mypy strict sobre un codebase de ~4700 líneas sin tipado histórico iba a generar una cantidad de errores lo bastante grande como para justificar un "ratchet": empezar permisivo, subir gradualmente módulo por módulo.

## Alternativas consideradas

1. **Ratchet gradual módulo por módulo** — la asumida por defecto en `ARCHITECTURE_REVIEW.md`. Se descartó en la práctica: la primera corrida real dio 185 errores, y tras triage la mayoría resultaron mecánicos (`dict` sin parámetros genéricos, funciones sin anotar) o imprecisiones de tipado reales pero baratas de corregir (ver más abajo) — no había volumen suficiente para justificar la complejidad de mantener un ratchet activo.
2. **Suprimir la sección `[tool.mypy]` / bajar a modo no-estricto permanente** — descartado: renunciaría al valor real de "tipado estricto" que pediste, solo para evitar un esfuerzo de una sola vez.
3. **Corregir todo hasta cero errores, con excepciones puntuales documentadas donde el costo/beneficio no lo justifica** (la elegida).

## Decisión

Se corrigieron los 185 errores originales hasta llegar a **cero errores en los 78 archivos fuente**, con dos categorías de excepción permanente, documentadas y acotadas:

1. **`disallow_untyped_defs`/`disallow_untyped_calls` = false** a nivel de proyecto — funciones internas sin anotar (sobre todo helpers de test como `_state()`, `_agent()`) siguen siendo válidas sin forzar una anotación de tipo en cada una. El resto de `strict` permanece activo.
2. **Override para `swarm_trading.tests.*`** que desactiva `arg-type`, `assignment`, `comparison-overlap` y `func-returns-value` — los test doubles (`_FakeBroker`, `_FakeWebSocket`, `_FakeIBClient`, etc.) duck-typean deliberadamente contra interfaces reales en vez de heredar de ellas; eso es un patrón de testing legítimo, no un hueco de seguridad de tipos. El código de aplicación (todo fuera de `tests/`) mantiene el chequeo completo de estos errores.

## Ventajas

- El "tipado estricto" que pediste es real, no aspiracional — corre en CI (Fase 2) y falla si alguien introduce un error de tipos nuevo.
- Corregir de una vez, con evidencia módulo por módulo, resultó más simple que construir y mantener infraestructura de ratchet (baseline files, exclusión temporal por módulo) para un volumen de errores que no la ameritaba.
- Varias correcciones no fueron solo "callar a mypy" — revelaron imprecisiones reales:
  - `agents/templates/swarm_factory.py`: la variable `agent` del loop se reusaba sin anotación entre 5 tipos de agente distintos (`ScalperAgent`, `SwingAgent`, ...) — ahora anotada explícitamente como `BaseAgent`, coherente con la separación Agent/Strategy planeada para Fase 7.
  - `tests/unit/test_ibkr_broker.py`: un test verificaba `pnl` de un trade pero no su `trade_id`, a diferencia de su caso hermano — se agregó la aserción faltante.
  - `main.py`/`data/historic/repository.py`: variables `Optional` sin anotar explícitamente, ahora tipadas correctamente.

## Desventajas

- Los `# type: ignore` puntuales (7 en total: 2 en `repository.py` por el patrón de `Insert` dialect-dinámico, 2 en `ibkr_broker.py` por subclasear `ibapi` — paquete no instalable en este entorno —, 1 en `market_feed.py` por una limitación de pandas-stubs, 2 en `mcp_server.py` por decoradores sin stubs) son deuda de tipado real, aunque acotada y documentada línea por línea. Se revisan si las librerías correspondientes publican stubs mejores en el futuro.
- El override de `tests.*` significa que un test mal escrito que compare tipos incompatibles por error (no por duck-typing intencional) no lo va a atrapar mypy — sigue cubierto por la ejecución real de la suite (pytest), que si detecta esos casos.

## Consecuencias

- `mypy --config-file pyproject.toml .` pasa limpio; se integra a `make typecheck` y al pipeline de CI (Fase 2) como gate real, no decorativo.
- `python_version` en la config quedó en `"3.12"` (no `"3.11"`, el mínimo del proyecto) — ver comentario en `pyproject.toml`: es solo para que mypy pueda parsear los stubs de numpy 2.x (usan sintaxis `type` de PEP 695), no cambia qué sintaxis puede usar el código propio.
- De paso, se corrigió un desfase de dependencias no relacionado directamente con mypy pero descubierto en el proceso: `numpy`/`pandas`/`torch` estaban instalados en versiones más nuevas que las fijadas en `requirements.txt` (arrastradas por la instalación de `stable-baselines3`/`gymnasium` en una fase anterior) — se actualizaron los pines para que coincidan con el entorno real y probado.
