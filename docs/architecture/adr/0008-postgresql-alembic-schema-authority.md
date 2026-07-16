# ADR-0008: PostgreSQL como base de datos de runtime, Alembic como única autoridad de esquema

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Fase 3

## Contexto

Hasta Fase 2, `DATABASE_URL` por defecto (`core/config.py`) apuntaba a un archivo SQLite local, y `AsyncRepository.init()` llamaba a `Base.metadata.create_all()` en cada arranque. Esto funcionaba para desarrollo local, pero tenía tres problemas para un despliegue 24/7 real:

1. **SQLite no está pensado para el patrón de escritura de este proyecto**: 100 agentes concurrentes escribiendo trades, con un solo archivo y sin un servidor real gestionando bloqueos/concurrencia más allá del nivel de archivo.
2. **`create_all()` es una segunda autoridad de esquema compitiendo con Alembic**: `create_all()` crea tablas directamente desde los modelos ORM (`db_models.py`) sin pasar por ninguna migración. Ya existía una migración inicial (`alembic/versions/4dad4f1eecd3_initial_tables.py`) que hace exactamente lo mismo, pero un despliegue que solo arrancara `main.py` nunca la ejecutaba — el esquema real en producción nunca pasaba por Alembic, `alembic current` mentía (mostraría "sin aplicar" aunque las tablas ya existieran), y cualquier migración futura (agregar una columna, un índice) quedaría sin aplicar silenciosamente porque las tablas ya "existían".
3. **No había ninguna base de datos Postgres real en `docker-compose.yml`** — el plan de Fase 5+ (Supabase/Postgres) llevaba placeholders en `core/config.py` (`supabase_url`, `supabase_key`) sin URL de conexión real ni contenedor.

## Alternativas consideradas

1. **Mantener SQLite + `create_all()` indefinidamente** — descartada: no resuelve el problema de concurrencia real de 100 agentes, y perpetúa la doble autoridad de esquema (ORM directo vs. Alembic) que ya generaba drift silencioso.
2. **Migrar a Postgres pero mantener `create_all()` como red de seguridad "por si Alembic no corrió"** — descartada explícitamente: es exactamente el tipo de auto-corrección silenciosa que se quiere eliminar. Si Alembic no corrió, el fallo debe ser visible (tablas faltantes → error claro al primer query), no disimulado.
3. **Postgres como base de datos real, Alembic como única autoridad de esquema, `AsyncRepository.init()` reducido a una validación de conectividad** (la elegida).

## Decisión

- `DATABASE_URL` en producción (`APP_ENV=paper` o `APP_ENV=live`) debe apuntar a PostgreSQL (`postgresql+asyncpg://...`). `core/config.py` valida esto al arrancar (`SwarmSettings._no_silent_sqlite_in_production`) y falla con un `ValueError` explícito si no se cumple — no hay fallback silencioso a SQLite.
- SQLite (`sqlite+aiosqlite:///./swarm_trading.db`, el default de `database_url`) sigue siendo válido únicamente para `APP_ENV=development` y para `tests/unit/*`. Nunca se presenta como equivalente a producción.
- `AsyncRepository.init()` ya **no** llama a `Base.metadata.create_all()`. Ahora solo valida conectividad (`SELECT 1`) y lanza `ConnectionError` con un mensaje claro si la base no responde. La creación/modificación de esquema es responsabilidad exclusiva de `alembic upgrade head`, ejecutado externamente (por un operador, por CI, o por el servicio `migrate` de `docker-compose.yml`) — nunca por la aplicación en tiempo de arranque.
- `docker-compose.yml` gana un servicio `postgres` (imagen fijada a `postgres:16`, no `latest`), con `pg_isready` como healthcheck, volumen nombrado persistente, y credenciales configurables por variables de entorno sin default para la contraseña (`POSTGRES_PASSWORD:?...` — falla al parsear el compose si falta, no arranca con una contraseña vacía). El servicio `swarm` depende de `postgres` con `condition: service_healthy`, no solo `depends_on` sin healthcheck.
- Un servicio `migrate` (perfil `tools`, nunca arranca automáticamente) aplica `alembic upgrade head` como paso explícito y separado antes de que `swarm` sirva tráfico.

### Hallazgo: no había conflicto sync/async en Alembic

Antes de implementar, se revisó `alembic/env.py` completo para verificar si Alembic corría en modo síncrono (lo cual habría sido un bloqueante real con `asyncpg`, driver que no expone una API síncrona). **No es el caso**: `alembic/env.py` ya usa `async_engine_from_config()` + `connection.run_sync(do_run_migrations)` dentro de `asyncio.run()` desde antes de esta fase — es decir, Alembic ya corre completamente en modo async-nativo contra el mismo driver `asyncpg` que usa la aplicación. No fue necesario ningún driver síncrono adicional (`psycopg2`) ni ningún workaround. Se documenta aquí como hallazgo explícito, no como "arreglado", porque no había nada que arreglar.

## Ventajas

- Una única fuente de verdad para el esquema: `alembic/versions/*`. `alembic current` refleja siempre la realidad.
- Un despliegue con migraciones pendientes falla de forma visible (query contra una tabla que no existe → error claro) en vez de que la aplicación "arregle" el esquema por su cuenta sin que nadie se entere.
- Postgres real resuelve la concurrencia de escritura de 100 agentes de forma nativa, sin el cuello de botella de archivo único de SQLite.
- El patrón de upsert dialecto-específico (`postgresql.insert()` / `sqlite.insert()` en `repository.py::save_trade`) ya existía desde antes de esta fase — solo necesitaba quedar cubierto por un test contra Postgres real (`tests/integration/test_postgres_repository.py::test_real_upsert_against_postgres`), no reescribirse.

## Desventajas

- No hay Docker ni Postgres disponibles en el entorno de desarrollo local usado para esta fase — la primera validación real contra un `postgres:16` de verdad ocurre en CI (`.github/workflows/ci.yml`, job `postgres-integration`), no localmente. Esto es un riesgo residual documentado, no oculto: si CI pasa en verde, es la primera confirmación end-to-end real.
- Un operador que arranque `docker compose up` sin antes correr `docker compose run --rm migrate` obtiene una app que arranca (la conectividad es válida) pero cuyas lecturas/escrituras fallarán o devolverán vacío contra tablas inexistentes — es el comportamiento correcto (fallar visible, no auto-crear), pero requiere que el paso de migración esté documentado y no se salte. Mitigado documentando el flujo exacto en `README.md`.
- `swarm_data` (el volumen SQLite anterior) se eliminó de `docker-compose.yml` — cualquier despliegue previo que dependiera de ese archivo debe migrar sus datos manualmente antes de actualizar (no hay migración de datos SQLite→Postgres automatizada; fuera de alcance de esta fase, que es sobre el esquema, no sobre backfill de datos históricos).
- `swarm.depends_on` en `docker-compose.yml` solo exige que `postgres` esté saludable, no que `migrate` haya corrido — Compose no tiene forma de expresar "espera a un servicio de perfil `tools`" sin acoplar su ciclo de vida al de `swarm`. La secuencia correcta (`postgres` → `migrate` → `swarm`) depende de que el operador siga `make docker-up` o el orden documentado en `README.md`, no de un enforcement mecánico de Compose. Es la misma situación que el punto anterior, no un problema nuevo.

## Rollback operativo

**Revertir el código a un estado anterior:** mientras esta rama no esté mergeada a `main`, revertir es simplemente no mergear el PR. Si ya se mergeó y hace falta deshacerlo, usar `git revert` del commit de merge — nunca `git reset --hard` sobre `main`. Revertir código no toca el esquema de Postgres por sí solo: Alembic solo actúa cuando alguien ejecuta `alembic upgrade`/`downgrade` explícitamente, así que un rollback de código con un esquema ya migrado hacia adelante puede dejar columnas/tablas de más que el código viejo simplemente ignora (no rompe, salvo que la migración también haya sido destructiva — ver más abajo).

**Si una migración futura falla en producción:**
1. `alembic current` para confirmar la revisión realmente aplicada — cada migración corre en su propia transacción en Postgres, así que un fallo a mitad de una revisión no la deja "parcialmente aplicada"; pero si esa misma corrida ya había completado revisiones previas, esas sí quedan aplicadas.
2. Si la revisión fallida no quedó registrada en `alembic_version`, corregir la migración (nunca editar una migración ya aplicada en otro entorno — ver "Principios obligatorios" de esta fase) y volver a correr `alembic upgrade head`.
3. Si hace falta revertir el esquema, `alembic downgrade <revision_anterior>` — cada migración debe mantener un `downgrade()` funcional (ver `alembic/versions/*`); la reversibilidad se verifica en CI (`postgres-integration`: `downgrade -1` + re-upgrade).
4. `swarm` no se reinicia automáticamente tras un fallo de migración — el operador decide cuándo repetir `make migrate` y solo entonces levantar `swarm` (ver punto anterior sobre por qué esto no está forzado por Compose).

## Consecuencias

- `core/config.py`: nuevo `model_validator` que rechaza `sqlite://` cuando `app_env` es `paper`/`live`.
- `data/historic/repository.py::init()`: ya no crea tablas; valida conectividad y lanza `ConnectionError` claro en caso de fallo.
- `docker-compose.yml`: servicios `postgres` y `migrate` nuevos; `swarm` ahora depende de `postgres` saludable y usa `DATABASE_URL` con `asyncpg`; volumen `swarm_data` eliminado.
- `.env.example`: documenta Postgres como runtime real y SQLite como dev/test-only.
- `pyproject.toml`: marcador `integration` registrado; `addopts` excluye `-m integration` por defecto (`pytest tests/` sigue siendo rápido y sin dependencia de Postgres).
- `tests/unit/test_repository.py`: el fixture ya no depende de `init()` para crear el esquema (crea las tablas directamente, emulando lo que haría una migración real); tres tests nuevos cubren el cambio de comportamiento de `init()`.
- `tests/integration/test_postgres_repository.py`: nuevo, 8 tests contra Postgres real — nunca simulan Postgres con SQLite.
- `.github/workflows/ci.yml`: nuevo job `postgres-integration` con contenedor de servicio `postgres:16`, aplica migraciones, verifica reversibilidad (`downgrade -1` + re-upgrade), corre los tests de integración.
- **Deuda técnica pendiente, explícita**: no existe backfill/migración de datos desde el SQLite de despliegues anteriores hacia Postgres. No existe todavía un chequeo de arranque en `main.py` que verifique que la revisión de Alembic aplicada coincide con `head` antes de servir tráfico (se decidió mantener esa responsabilidad completamente fuera de la aplicación por ahora, ver "Alternativas consideradas" punto 2 — revisar si esto debe endurecerse en una fase futura).
