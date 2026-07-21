# ADR-0011: Deployment automático a staging en VPS propio — bundle firmado por SHA, frontera de seguridad fija, sin `git pull`

**Estado:** Aceptado
**Fecha:** 2026-07-21
**Fase del roadmap:** Fase 4.6

## Contexto

`ARCHITECTURE_REVIEW.md` deja explícito que Fase 4 (Dockerización) prepara el stack (`app+postgres+redis+nginx`, healthchecks, `migrate` como gate) para correr 24/7 en un VPS propio, pero no automatiza cómo un merge aprobado a `main` termina corriendo ahí. Fase 4.5 (ADR-0010) cerró el guardrail de riesgo (halts diario/total); esta fase cierra el guardrail operativo: hoy no existe ningún camino automático, auditable y reversible entre "CI verde en `main`" y "esa versión corriendo en staging" — el único camino sería manual (SSH, `git pull`, `docker compose up` a mano), sin registro de quién lo hizo ni forma mecánica de revertir.

Restricciones explícitas de alcance, acordadas antes de diseñar: sin Kubernetes, sin broker live/dinero real en esta fase, sin tocar `Agent`/`Strategy` ni código de producto, sin build de imagen en el propio VPS, sin `git pull` como mecanismo principal de deployment, sin secretos en Git, sin exponer Postgres/Redis públicamente, sin deploy automático a producción (esta fase es exclusivamente staging).

Dos personas operan este sistema: quien mantiene el repo y el lado GitHub (autor de este ADR) y un operador distinto responsable del bootstrap y la operación inicial del VPS — el diseño asume que ambos actúan con identidades separadas y auditables, no una cuenta compartida.

## Alternativas consideradas

### Disparo del deploy
1. **Jobs añadidos a `ci.yml` con `needs: [...]`** — descartada: mezclaría el archivo que ya es el gate de calidad (8 jobs, revisado a fondo en el PR #5) con lógica que tiene permisos de escritura sobre un servidor real y credenciales SSH; un cambio en la lógica de deploy no debería re-correr ni arriesgar ese archivo.
2. **Workflow separado (`deploy-staging.yml`) disparado por `workflow_run` sobre `ci.yml`** (la elegida) — separa responsabilidades y superficie de secretos; valida tres condiciones juntas (`conclusion == 'success'`, nombre exacto del workflow origen, `head_branch == 'main'`) antes de hacer nada, y usa siempre `workflow_run.head_sha` explícito — nunca checkout de la punta mutable de `main`, que puede haberse movido si dos merges llegan seguidos.

### Runner
1. **Self-hosted en el propio VPS** — descartada: eliminaría SSH, pero le daría a cualquiera que pueda mergear a `main` ejecución de código arbitrario directamente en el VPS sin control adicional. El flujo deseado ya especificaba SSH explícitamente.
2. **GitHub-hosted + SSH saliente hacia el VPS** (la elegida).

### Cómo llega el código al VPS
1. **`git pull` en el VPS** — descartada explícitamente por restricción: depende de que el VPS tenga acceso de lectura al repo, no dice nada sobre *qué SHA* se pretendía correr de forma verificable en el mismo paso, y mezcla "traer código" con "aplicar cambios" sin un punto de validación intermedio.
2. **`scp`/`rsync` de archivos sueltos** — descartada: exige que la clave de CI tenga acceso de shell más amplio que "ejecutar exactamente un comando", ampliando la superficie de lo que una clave filtrada podría hacer.
3. **Bundle `tar` con allowlist explícita, generado por Actions desde `workflow_run.head_sha`, enviado por `stdin` sobre la misma conexión SSH que ejecuta el `command=` forzado** (la elegida) — `tar -cf - <allowlist> | ssh swarm-deploy@host "deploy <sha>"`. Un solo canal, una sola credencial, sin habilitar `scp`/`rsync`/`SFTP`/shell libre en ningún momento. El propio bundle es lo único que el VPS recibe; su contenido se valida antes de confiar en él (ver frontera de seguridad).

### Qué archivos viven en el VPS por release vs. de forma fija
1. **Todo fijo, actualizado manualmente cuando cambia** — descartada: es exactamente el modo "copia desactualizada" que se buscaba evitar; nada garantiza que lo que corre coincida con lo que hay en el repo en ese SHA.
2. **Todo versionado por release, incluido el propio validador de entrada** — descartada: el componente que decide "confío en este bundle" no puede ser parte de lo que el bundle mismo trae, o un bundle malicioso podría reemplazar su propio portero.
3. **Frontera fija + resto versionado por release** (la elegida): `scripts/ci-deploy-entrypoint.sh` se instala una sola vez durante el bootstrap y nunca se actualiza automáticamente desde un release — es la única pieza de confianza fija. Todo lo demás (`docker-compose.yml`, `docker-compose.staging.yml`, `deploy.sh`, `rollback.sh`, archivos auxiliares) viaja versionado dentro del bundle de cada release y se extrae a `releases/<sha>/`, así que lo que se ejecuta siempre coincide exactamente con el SHA que se está desplegando.

### Registro/tags de imagen
1. **GHCR público** — descartada por ahora: sin razón real para exponerla todavía; queda como decisión reversible, no técnica.
2. **Tag `latest` usado en el deploy** — descartada explícitamente: un tag mutable no permite un rollback determinista ni saber con certeza qué corrió en un momento dado.
3. **GHCR privado, tag único e inmutable `sha-<sha_completo>`, con label OCI `org.opencontainers.image.revision` verificado antes de desplegar** (la elegida). `latest` puede publicarse más adelante como conveniencia manual, nunca como lo que el pipeline despliega o rollea.

### Acceso al VPS
1. **Cuenta de automatización con clave sin restricciones (shell libre)** — descartada: si `STAGING_SSH_PRIVATE_KEY` se filtra, da control total del VPS (la cuenta técnica está en el grupo `docker`, equivalente a root).
2. **Clave de automatización con `command=` forzado hacia `ci-deploy-entrypoint.sh`, más `no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding`** (la elegida) — una clave filtrada solo puede invocar ese entrypoint fijo, con las validaciones descritas abajo.
3. **Los dos administradores humanos comparten una cuenta** — descartada explícitamente: sin esto, un rollback manual de emergencia no sería atribuible a una persona concreta.
4. **Cada administrador con cuenta y clave propias, sin contraseña compartida, `swarm-deploy` sin login humano, elevación vía `sudo` acotado (`deploy.sh`/`rollback.sh` únicamente) con la contraseña personal de cada admin (sin `NOPASSWD` inicialmente)** (la elegida) — la atribución de un deploy manual queda en `auth.log`/`sudo` por identidad real, sin depender de que el script "recuerde" quién lo llamó.

### Rollback de esquema
1. **`alembic downgrade -1` automático como parte del rollback** — descartada: muchas migraciones no son seguras de revertir (pérdida de datos en un `DROP COLUMN`, por ejemplo); automatizarlo sería más peligroso que no tocar el esquema.
2. **Rollback solo de código (vuelve a la imagen `sha-<anterior>` explícita), esquema intacto; una reversión de esquema real queda como restauración manual del backup pre-deploy** (la elegida) — exige, como política, que las migraciones sean retrocompatibles con la versión N-1 de la app (patrón expand/contract), no algo que el pipeline pueda forzar por sí solo.

## Decisión

Un workflow separado (`deploy-staging.yml`, disparado por `workflow_run` sobre `ci.yml`, con las tres validaciones de origen ya descritas) construye la imagen desde `workflow_run.head_sha`, la publica en GHCR con el tag único e inmutable `sha-<sha_completo>` y el label OCI `org.opencontainers.image.revision`, arma un bundle `tar` con una allowlist explícita de los archivos versionados de deployment, y lo envía por `stdin` a través de una conexión SSH cuya clave está restringida por `command=` a `ci-deploy-entrypoint.sh` — un componente fijo, instalado en el bootstrap, nunca actualizado desde un release, responsable de validar el comando, el tamaño del bundle, el contenido del `tar` (sin rutas absolutas, sin `..`, sin symlinks peligrosos, solo archivos de la allowlist), que el manifest interno declare el mismo SHA que se pidió, que la imagen `sha-<sha>` exista y que su label OCI coincida, antes de extraer a `releases/<sha>/` y ejecutar el deploy de ese release (backup → migrate → swap → `/health/ready` → rollback automático a la imagen anterior si falla, sin `eval` ni interpolación de shell en ningún punto).

Acceso al VPS: dos administradores humanos con cuentas y claves independientes, sin contraseñas compartidas, `root` inalcanzable por SSH; `swarm-deploy` sin login humano interactivo, alcanzable solo por el comando forzado (CI) o por `sudo` acotado (admins, con su propia contraseña). El Environment `staging` de GitHub exige aprobación manual en los primeros despliegues, restringe la rama de despliegue a `main`, y contiene únicamente los secretos SSH (`STAGING_SSH_PRIVATE_KEY`, `STAGING_SSH_KNOWN_HOSTS`) — el token de lectura de GHCR se instala una sola vez en el bootstrap, nunca viaja en cada deploy.

El workflow declara `permissions:` mínimos y explícitos (`contents: read`, `packages: write`, nada más) y nunca reutiliza artefactos (`upload-artifact`/`download-artifact`) producidos por el run de `ci.yml` que lo disparó — el build siempre parte del código fuente en `workflow_run.head_sha`, nunca de binarios ajenos sin verificar. Contra deploys concurrentes, el job de deploy usa `concurrency: {group: staging-deploy, cancel-in-progress: false}` del lado de GitHub, y `deploy.sh` toma además un `flock` sobre un lockfile propio en el VPS como defensa independiente de GitHub Actions.

## Ventajas

- Lo que se ejecuta en cada deploy coincide exactamente con el SHA solicitado — verificado en al menos tres puntos independientes (manifest del bundle, existencia de la imagen, label OCI de la imagen), no asumido.
- Una clave de automatización filtrada tiene un radio de acción mínimo: un solo comando fijo, con sus propias validaciones, sin shell.
- Rollback determinista: siempre a una imagen inmutable nombrada explícitamente, nunca a "lo que sea que `latest` apunte ahora".
- Atribución real de cada deploy, automático o manual, sin depender de que nadie recuerde loguear nada a mano.
- El `ci.yml` ya revisado (PR #5) no se toca para nada de esto.

## Desventajas

- Superficie de validación no trivial en `ci-deploy-entrypoint.sh` (parsing de `stdin`, inspección de `tar` antes de extraer, verificación de manifest e imagen) — más código de seguridad que mantener que un simple `git pull`, a cambio de las garantías de arriba.
- El componente fijo (`ci-deploy-entrypoint.sh`) es, por diseño, lo único que *no* se actualiza automáticamente — un cambio en su lógica de validación requiere una intervención manual en el VPS (documentada como procedimiento aparte), no solo un merge a `main`.
- Sin backup off-site ni notificaciones (Telegram) en esta primera iteración — aceptado conscientemente para esta fase, dejado como trabajo futuro explícito.
- `swarm-deploy` en el grupo `docker` sigue siendo equivalente a root en la práctica — mitigado por las restricciones de la clave de CI y por que ningún humano tiene esa clave, no eliminado del todo (Docker rootless queda como upgrade de endurecimiento futuro).

## Consecuencias

- Nuevos archivos versionados: `docs/deploy/staging-vps-bootstrap.md`, `docs/deploy/staging-handoff-checklist.md` (este ADR, Fase 4.6 — PR 1); `scripts/ci-deploy-entrypoint.sh`, `scripts/deploy.sh`, `scripts/rollback.sh`, `docker-compose.staging.yml` (PR 2); `.github/workflows/deploy-staging.yml` (PR 3, solo build-and-push; PR 4, job de deploy completo).
- `ci.yml` no se modifica.
- El VPS requiere una estructura fija (`/opt/swarm-trading/{releases,shared,backups,logs,scripts}`) y un bootstrap manual único, ejecutado por el operador del VPS siguiendo el runbook — no automatizado, por diseño (es el único momento en que hay acceso root/administrativo directo).
- Queda fuera de alcance de esta fase: TLS/dominio público, broker live, Kubernetes, backup off-site, notificaciones de deploy, un Environment `production` separado con sus propias reglas (gate manual permanente, no solo inicial).
- Cada fase de este trabajo (PR 1-4) se confirma y aprueba antes de la siguiente, mismo patrón que Fase 4.5.
