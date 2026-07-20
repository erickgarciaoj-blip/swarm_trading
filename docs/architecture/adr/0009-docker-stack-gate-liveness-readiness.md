# ADR-0009: Stack Docker completo — migración como gate mecánico, Redis efímero, nginx opcional, liveness ≠ readiness

**Estado:** Aceptado
**Fecha:** 2026-07-20
**Fase del roadmap:** Fase 4

## Contexto

ADR-0008 (Fase 3) dejó documentada una desventaja explícita, no resuelta en su momento: *"`swarm.depends_on` en `docker-compose.yml` solo exige que `postgres` esté saludable, no que `migrate` haya corrido — Compose no tiene forma de expresar 'espera a un servicio de perfil `tools`' sin acoplar su ciclo de vida al de `swarm`."* La secuencia correcta (`postgres` → `migrate` → `swarm`) dependía de que el operador siguiera `make docker-up` o el orden documentado en `README.md` — es decir, de disciplina humana, no de algo que Compose pudiera hacer cumplir.

Además, Fase 4 (ver `ARCHITECTURE_REVIEW.md`, tabla de roadmap) pedía explícitamente completar la dockerización: `app` + `postgres` + `redis` + `nginx`, healthchecks reales, y separar liveness de readiness para que Docker no reinicie el contenedor de la app cuando el problema real está en una dependencia externa (Postgres), no en el proceso mismo.

## Alternativas consideradas

### Gate de migración
1. **Mantener `migrate` en `profiles: [tools]`, disciplina documental** (el statu quo de ADR-0008) — descartada: es exactamente la limitación que este ADR existe para cerrar.
2. **Script wrapper externo** (`entrypoint.sh` en la imagen de `swarm` que corre `alembic upgrade head` antes de `exec python main.py`) — descartada: mezclaría la responsabilidad de "aplicar esquema" dentro del propio contenedor de la app, volviendo a la ambigüedad que ADR-0008 ya cerró (la app nunca gestiona su propio esquema, ver `data/historic/repository.py::init()`).
3. **`migrate` sin perfil, con `swarm.depends_on.migrate.condition: service_completed_successfully`** (la elegida) — Compose nativo, sin scripts adicionales; `migrate` sigue siendo un servicio separado e inspeccionable (`docker compose logs migrate`), solo que ahora su éxito es una precondición mecánica para que `swarm` arranque.

### Redis
1. **No incluirlo hasta que Fase 5 lo use** — descartada: Fase 4 del roadmap pide explícitamente el stack completo (`app+postgres+redis+nginx`) preparado de antemano, para que Fase 5 sea solo código de aplicación detrás de `CachePort`/`PubSubPort` (ver `ARCHITECTURE_REVIEW.md` §3.8) sin tocar infraestructura otra vez.
2. **Redis con volumen persistente** — descartada: nada lo usa todavía; persistir un caché vacío no tiene sentido y añade una superficie de "¿por qué hay datos viejos en Redis?" el día que sí se use, sin haber definido aún qué se cachea ni con qué TTL.
3. **Redis efímero (`--save "" --appendonly no`, sin volumen), sin `depends_on` desde `swarm`** (la elegida).

### nginx
1. **Activo por defecto** — descartada: hoy no aporta nada que `swarm` no haga ya (no hay TLS, no hay más de un backend que balancear); forzar su presencia solo añade un contenedor más a monitorear sin beneficio inmediato.
2. **No incluirlo hasta que haga falta TLS real** — descartada: el objetivo de Fase 4 es dejar la forma del cambio ya escrita y probada en CI, para que activar TLS más adelante sea "descomentar y añadir certificados", no "diseñar la integración desde cero".
3. **Presente pero opt-in vía `profiles: [proxy]`** (la elegida).

### Pinning de la imagen base
1. **`python:3.11-slim` (flotante)** — descartada: ya re-apuntó de bullseye a bookworm una vez sin que nada en este repo cambiara; el conjunto de paquetes `apt` bajo la imagen puede cambiar sin aviso.
2. **Digest de contenido exacto (`python:3.11-slim-bookworm@sha256:...`)** — descartada por ahora: es el pinning más fuerte posible, pero exige un proceso explícito de actualización de digest (que hoy no existe) o el build se congela silenciosamente en una imagen con CVEs sin parchear. Queda como candidato natural para cuando exista ese proceso.
3. **Codename de Debian (`python:3.11-slim-bookworm`)** (la elegida) — punto medio: fija el conjunto de paquetes `apt` al release de Debian, deja que los parches de seguridad de patch-version de Python y de Debian sigan llegando.

## Decisión

- `migrate` deja de tener `profiles: [tools]`; `swarm` declara `depends_on: migrate: condition: service_completed_successfully`. Un `docker compose up` (con o sin `-d`) aplica el esquema antes de arrancar la app, y si la migración falla, `swarm` no arranca — no hay forma de saltarse el paso por error.
- `redis` se añade a `docker-compose.yml`: efímero (sin volumen, persistencia RDB/AOF desactivada), sin que `swarm` dependa de él. Ningún código de la aplicación lo importa todavía (`core/config.py` solo expone `redis_url`); es infraestructura preparada para Fase 5, no una dependencia funcional hoy.
- `nginx` se añade bajo `profiles: [proxy]` (`docker compose --profile proxy up -d` o `make docker-up-proxy`). Proxea HTTP y el upgrade de WebSocket (`/ws`) hacia `swarm:8000`; sin TLS todavía (ver `nginx/nginx.conf`, bloque `443` comentado con lo que falta para activarlo).
- `/health` (liveness) y `/health/ready` (readiness) quedan separados en `dashboard/api/routes.py` — `/health` nunca toca Postgres/Redis y responde `{"status": "ok"}` mientras el proceso esté vivo; `/health/ready` sí valida conectividad real vía `AsyncRepository.is_ready()`. El `HEALTHCHECK`/healthcheck de Compose del servicio `swarm` usa **`/health`**, deliberadamente: si Postgres cae unos segundos, el contenedor de la app no debe reiniciarse por eso — reiniciarlo no arregla Postgres y solo añade un segundo problema (la app cayendo) al primero.
- Imagen base fijada a `python:3.11-slim-bookworm` (codename de Debian, no solo `slim`).

## Ventajas

- El orden `postgres` → `migrate` → `swarm` ya no depende de que un operador recuerde el orden correcto ni de seguir `README.md` al pie de la letra — Compose lo fuerza estructuralmente.
- Redis y nginx están provisionados y probados en CI (ver Consecuencias) antes de que exista presión de plazo para usarlos, evitando que Fase 5 tenga que diseñar infraestructura y funcionalidad al mismo tiempo.
- Un Postgres momentáneamente lento o reiniciándose ya no puede producir un bucle de reinicios del contenedor `swarm` vía Docker healthcheck — liveness y readiness fallan de forma independiente y visible (`docker compose ps`, `/health/ready` devuelve 503 sin tumbar el healthcheck de liveness).
- Activar TLS en el futuro es extender un bloque ya escrito en `nginx/nginx.conf`, no diseñarlo desde cero.

## Desventajas

- Un `docker compose run --rm migrate` manual sigue siendo posible y sigue funcionando igual que antes (para aplicar migraciones sin reiniciar `swarm`), pero ahora coexiste con el gate automático — dos caminos hacia el mismo resultado, documentados ambos en el `Makefile` (`make migrate` vs. el gate implícito en `make docker-up`) para que no parezcan inconsistentes entre sí.
- `redis` consume un contenedor y un healthcheck que hoy no protegen ningún dato — coste operativo real (un proceso más que monitorear) a cambio de un beneficio que todavía no existe. Aceptado conscientemente por la razón de secuenciación de Fase 5 explicada arriba.
- El pinning por codename (`slim-bookworm`) sigue permitiendo que parches de seguridad de Debian cambien paquetes bajo el build sin que nada en este repo lo refleje — es una elección deliberada de punto medio, no el pinning más fuerte posible (ver alternativa de digest, descartada por falta de proceso de actualización, no por ser inferior en principio).
- `nginx` bajo `profiles: [proxy]` significa que su ruta de WebSocket (`/ws` vía nginx, distinta de `/ws` directo contra `swarm:8000`) solo se ejerce en CI cuando el job explícitamente activa ese profile — un regresión ahí no se detecta en un `docker compose up` sin el flag.

## Consecuencias

- `docker-compose.yml`: servicios `redis` y `nginx` nuevos; `migrate` sin perfil; `swarm.depends_on` extendido con la condición de `migrate`; `logging:` con rotación (`json-file`, `max-size: 10m`, `max-file: 3`) en todos los servicios.
- `nginx/nginx.conf`: nuevo — reverse proxy HTTP + WebSocket, bloque TLS comentado como plantilla futura.
- `nginx/Dockerfile`: nuevo, mínimo (`FROM nginx:1.27-alpine` + `apk add curl`) — el servicio `nginx` en `docker-compose.yml` usa `build:`, no `image:` directo. Necesario porque la imagen `nginx:alpine` corriente ya no incluye `wget` ni `curl` (se construye desde una variante `-alpine-slim` que los purga tras el build de nginxinc/docker-nginx), y el `healthcheck` del servicio necesita poder hacer una petición HTTP contra sí mismo. Detectado por el propio job `docker-stack-integration` fallando en CI (nginx nunca reportaba `healthy`).
- `Makefile`: `docker-up` simplificado a `docker compose up -d --wait` (la orquestación manual de tres pasos que documentaba ADR-0008 ya no es necesaria); `docker-up-proxy` nuevo; `db-backup`/`db-restore` nuevos (ver sus propios comentarios en el `Makefile` para las garantías que cumplen: no sobrescriben, no imprimen la contraseña, validan el archivo antes de aceptarlo).
- `.env.example`: `REDIS_URL` apunta al hostname `redis` de `swarm_net` por defecto.
- CI (`.github/workflows/ci.yml`, job `docker-stack-integration`): construye la imagen, levanta el stack con `--wait`, verifica liveness/readiness, prueba en negativo que una migración fallida efectivamamente bloquea a `swarm` (proyecto Compose aislado, limpieza con `if: always()`), y prueba el WebSocket real a través de `--profile proxy`.
- Queda fuera de alcance de esta fase: TLS real en nginx (requiere dominio y certificados — ver plantilla comentada), y el uso funcional de Redis (Fase 5).
