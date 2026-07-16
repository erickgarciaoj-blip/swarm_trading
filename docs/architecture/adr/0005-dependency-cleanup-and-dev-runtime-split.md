# ADR-0005: Limpieza de dependencias + separación runtime/dev

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Fase 2

## Contexto

Durante la instalación de las herramientas de calidad (ruff, mypy, pytest-cov, pre-commit) para Fase 2 aparecieron dos problemas independientes en `requirements.txt`:

1. **Desfase de versiones real.** `numpy` (1.26.4 fijado, 2.5.1 instalado), `pandas` (2.2.2 fijado, 3.0.3 instalado) y `torch` (2.3.1 fijado, 2.13.0 instalado) — arrastrados a versiones más nuevas cuando se instaló `stable-baselines3`/`gymnasium` en una fase anterior de esta sesión, sin que nadie actualizara los pines. `pip install -r requirements.txt` hoy no reproduce el entorno que realmente corre y pasa los tests.
2. **Dependencias fantasma.** `TA-Lib`, `scikit-learn` y `xgboost` están fijadas pero **nunca se importan en ningún archivo del proyecto** (verificado con `grep` exhaustivo). `TA-Lib` ni siquiera estaba instalada en este venv. El propio código documenta explícitamente que evita TA-Lib a propósito (`agents/swing/swing_agent.py`: "no TA-Lib dependency required").

Además, todas las herramientas de desarrollo (pytest, ruff, mypy, pre-commit) vivían en el mismo `requirements.txt` que el runtime — cualquier imagen Docker construida con `pip install -r requirements.txt` terminaría cargando mypy/pre-commit en producción.

## Alternativas consideradas

1. **Dejar los pines desactualizados y las dependencias fantasma como estaban** — descartado: un `requirements.txt` que no reproduce el entorno probado es peor que no tener pines, y arrastrar ~200MB+ de `scikit-learn`/`xgboost`/TA-Lib sin usarlos contradice directamente el objetivo de "no imágenes gigantes" del plan de Docker.
2. **Forzar downgrade a los pines viejos** (`numpy==1.26.4`, etc.) — riesgoso: `stable-baselines3`/`gymnasium` en sus versiones actuales fueron los que forzaron la actualización; revertir podría romper el stack de RL que ya está probado y funcionando.
3. **Actualizar los pines a lo que realmente está instalado y probado, eliminar lo no usado, separar runtime de dev** (la elegida).

## Decisión

- `requirements.txt` (runtime): `numpy`/`pandas`/`torch` actualizados a las versiones realmente instaladas; `TA-Lib`, `scikit-learn`, `xgboost` eliminados.
- `requirements-dev.txt` (nuevo, `-r requirements.txt` + herramientas de dev): pytest, pytest-asyncio, pytest-cov, pandas-stubs, ruff, mypy, pre-commit. El `Dockerfile` de producción solo instala `requirements.txt`.
- `black` eliminado — se usa `ruff format` (compatible con el output de black) en su lugar, para no mantener dos herramientas de formateo con configuración que puede divergir (line-length, target-version).
- `Dockerfile`, `Makefile` y `scripts/install_macos.sh` actualizados para reflejar el split y la eliminación del paso de compilación nativa de TA-Lib.

## Ventajas

- `pip install -r requirements.txt` (o `requirements-dev.txt`) ahora reproduce fielmente lo que corre y pasa la suite completa.
- Imagen Docker de producción más chica y con menos superficie: sin `mypy`/`pre-commit`/`pytest`, sin el build nativo de TA-Lib (que además requería `wget`+compilar desde fuente).
- Un solo tool (`ruff`) para lint + format, no dos configs que pueden desincronizarse.

## Desventajas

- Si en el futuro alguien sí necesita `scikit-learn`/`xgboost`/TA-Lib para una estrategia nueva, hay que volver a agregarlos explícitamente — aceptable, es el comportamiento correcto (agregar dependencias cuando se usan, no antes).

## Consecuencias

- `requirements.txt`, `requirements-dev.txt` (nuevo), `Dockerfile`, `Makefile`, `scripts/install_macos.sh` modificados.
- `core/models.py`: los 6 enums de dominio (`Symbol`, `Side`, `AgentType`, `AgentStatus`, `OrderStatus`, `NewsImpact`) migraron de `(str, Enum)` a `StrEnum` (Python 3.11+) como parte del mismo pase de limpieza — ruff lo señaló como modernización (`UP042`) y se verificó que el comportamiento de serialización/comparación/`str()` es idéntico o mejor (`str(Symbol.XAUUSD)` ahora devuelve `"XAUUSD"` de forma consistente) antes de aplicarlo.
