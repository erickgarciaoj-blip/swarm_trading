# Runbook — Bootstrap del VPS de staging (Fase 4.6)

Procedimiento único, ejecutado a mano por el operador del VPS. No está automatizado a propósito: es el único momento en que hay acceso administrativo directo a la máquina, y automatizarlo significaría darle a algo (un script, un pipeline) el mismo nivel de acceso que aquí se le está negando deliberadamente a todo lo demás.

Ver [ADR-0011](../architecture/adr/0011-vps-staging-deployment.md) para el porqué de cada decisión de diseño referenciada aquí. Este documento es el "cómo", no el "por qué".

**Alcance de este PR:** este runbook deja el VPS listo para recibir despliegues, pero por sí solo **no produce un pipeline de deploy funcional** — `scripts/ci-deploy-entrypoint.sh`, `deploy.sh` y `rollback.sh` llegan en el PR 2, la imagen publicada en GHCR en el PR 3, y la conexión real por SSH desde GitHub Actions en el PR 4. Los pasos que dependen de eso (12, 18, 19) quedan marcados explícitamente como pendientes hasta entonces.

## Convenciones de este documento

- Todo lo que aparece entre `<...>` es un placeholder — reemplázalo por el dato real antes de ejecutar. Nada de este runbook asume una IP, dominio o nombre de usuario específico.
- Los bloques de comandos se ejecutan tal cual, en el orden en que aparecen, salvo que se indique lo contrario explícitamente.
- **Nunca cierres una sesión SSH que funciona hasta haber confirmado que la siguiente también funciona.** Esta regla aparece varias veces porque es la que evita quedarte fuera del VPS a mitad de camino.
- Este runbook asume Debian o Ubuntu con `systemd`, `apt` y `ufw`. Si tu VPS usa otra distribución, adapta los comandos de paquetes/firewall — la secuencia y las validaciones siguen aplicando igual.

## 0. Antes de empezar

Reúne esto antes de tocar nada:

- Acceso actual al VPS (usuario y método — root con contraseña del proveedor, root con una clave ya puesta, o ya existe un usuario con `sudo`).
- Si el proveedor ofrece una consola web/VNC/modo rescate fuera de banda — es tu plan de emergencia si algo del hardening de SSH sale mal. Confírmalo y anota cómo se accede a ella *antes* de empezar, no cuando ya la necesites.
- Nombre de usuario y clave pública SSH de cada uno de los dos administradores (cada persona genera su propio par en su máquina — ver paso 2 — la privada nunca se comparte ni se envía a nadie).
- Un token de GitHub con alcance `read:packages` para el login a GHCR (paso 17) — se genera desde GitHub, no antes de necesitarlo.

## 1. Snapshot de seguridad antes de tocar nada

Con el acceso que ya tienes hoy:

```bash
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%s)
```

Repite esta copia antes de cada edición posterior a `sshd_config` en este runbook — permite ver exactamente qué cambió si algo sale mal.

## 2. Crear las dos cuentas humanas de administrador

Cada administrador genera su propio par de claves **en su propia máquina**, nunca en el VPS:

```bash
# en la máquina de cada admin, no en el VPS
ssh-keygen -t ed25519 -C "<usuario>@swarm-staging"
```

Con passphrase (protege la clave si su laptop se compromete — no bloquea nada porque este login sí es interactivo). Solo la clave *pública* (`~/.ssh/<archivo>.pub`) se comparte para el siguiente paso.

En el VPS, con el acceso actual:

```bash
useradd --create-home --shell /bin/bash <admin1_user>
useradd --create-home --shell /bin/bash <admin2_user>

# grupo de sudo de la distro: "sudo" en Debian/Ubuntu, "wheel" en otras
usermod -aG sudo <admin1_user>
usermod -aG sudo <admin2_user>
```

## 3. Instalar la clave pública de cada admin

Para cada administrador:

```bash
su - <admin_user>
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# pega aquí la clave PÚBLICA de esa persona (una línea, empieza con ssh-ed25519)
echo "<clave_publica_del_admin>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
exit
```

## 4. Probar el acceso nuevo — antes de tocar `sshd_config`

**No sigas al paso 5 sin completar esto.** Mantén tu sesión actual (root) abierta.

Desde tu propia máquina, en una terminal nueva:

```bash
ssh <admin1_user>@<VPS_IP>
exit
```

Repite para `<admin2_user>`. Ambos deben poder entrar por clave — esto solo confirma el login SSH. `sudo` todavía no tiene nada que verificar aquí: las contraseñas de cada admin se configuran recién en el paso 14 y las reglas de `sudoers` en el paso 13 — ambas se verifican juntas al final, en la sección 22. Si el login por clave falla para cualquiera de los dos, corrige antes de continuar — la sesión root original sigue siendo tu red de seguridad.

## 5. Endurecer SSH

Edita `/etc/ssh/sshd_config` (o un archivo en `/etc/ssh/sshd_config.d/` si tu distro lo prefiere así):

```
PermitRootLogin no
PasswordAuthentication no
AllowUsers <admin1_user> <admin2_user> swarm-deploy
```

(`swarm-deploy` se crea en el paso 11 — puedes añadirlo a `AllowUsers` ahora aunque la cuenta todavía no exista, o volver a este archivo después.)

Valida la sintaxis **antes** de aplicar nada:

```bash
sshd -t
```

Si `sshd -t` no reporta errores, recarga (nunca reinicies) el servicio:

```bash
systemctl reload sshd
```

`reload` aplica la configuración nueva sin cortar las conexiones ya activas — tu sesión root original sigue viva incluso después de que root quede bloqueado para *nuevos* logins.

**Con la sesión root original todavía abierta**, abre una tercera terminal nueva y verifica:

```bash
ssh root@<VPS_IP>                                                          # debe ser RECHAZADO — confirma que el hardening aplicó
ssh <admin1_user>@<VPS_IP>                                                 # debe seguir funcionando
ssh <admin2_user>@<VPS_IP>                                                 # debe seguir funcionando
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password \
    -o BatchMode=yes <admin1_user>@<VPS_IP>                                # debe ser RECHAZADO — confirma que PasswordAuthentication no quedó activo
```

Solo si las cuatro verificaciones son correctas, cierra la sesión root original.

## 6. Firewall

**Mantén tu sesión SSH actual abierta durante todo este paso** — mismo principio que en el paso 5: no confíes en el cambio hasta haberlo verificado desde una sesión nueva.

El puerto SSH es **22**, salvo que ya lo hayas cambiado tú mismo en `sshd_config` (este runbook no lo cambia por defecto). Si usas un puerto distinto, sustituye el `22` de los comandos siguientes por ese valor — nunca dejes un placeholder sin sustituir en un comando que vas a ejecutar.

```bash
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp     # o tu puerto real si lo cambiaste — rate-limited, no solo "allow", mitiga fuerza bruta
```

**Antes de habilitar el firewall**, confirma que la regla de SSH quedó realmente registrada — no asumas que el comando anterior funcionó:

```bash
ufw show added | grep "22/tcp"    # debe aparecer la regla que acabas de añadir
```

Si esa línea no aparece, **no continúes** — revisa el comando anterior antes de seguir. Solo cuando la regla esté confirmada:

```bash
ufw enable
```

**Con la sesión actual todavía abierta**, abre una terminal nueva y confirma que el acceso SSH sigue funcionando antes de cerrar la anterior:

```bash
ssh <admin1_user>@<VPS_IP>   # debe seguir funcionando
```

Si el acceso falla, todavía tienes la sesión original abierta para revertir (`ufw disable`) o seguir el procedimiento de recuperación de la sección 21.

Si tu proveedor ofrece un firewall de red (Hetzner Cloud Firewall, DigitalOcean Cloud Firewalls, security groups de AWS, etc.), configúralo también como segunda capa — no dependas solo de `ufw`.

No abras aquí los puertos 80/443 todavía — quedan fuera de esta fase hasta que haya dominio y TLS reales. No abras 5432 (Postgres) ni 6379 (Redis) — nunca deben ser alcanzables desde fuera del `swarm_net` de Docker. Los puertos 8000 (dashboard) y 8080 (nginx, perfil `proxy`) tampoco necesitan regla de firewall: `docker-compose.yml` ya los bindea a `127.0.0.1`, así que no son alcanzables desde fuera del propio VPS pase lo que pase con el firewall — no los abras aquí.

## 7. fail2ban

```bash
apt-get update && apt-get install -y fail2ban
systemctl enable --now fail2ban
```

La configuración por defecto ya vigila `sshd` — suficiente para esta fase, sin necesitar jails adicionales todavía.

## 8. Actualizaciones automáticas de seguridad

```bash
apt-get install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

Deja las actualizaciones de seguridad del SO automáticas; Docker Engine se actualiza aparte, a mano (paso 9), para no arriesgar un cambio de comportamiento del daemon sin que nadie se entere.

## 9. Instalar Docker

Sigue la instalación oficial del [repositorio de Docker](https://docs.docker.com/engine/install/) para tu distro (no el paquete `docker.io` del repositorio del SO, que suele quedar desactualizado). Confirma al final:

```bash
docker --version
docker compose version
```

## 10. Verificar Python 3

`ci-deploy-entrypoint.sh` (PR 2 de esta fase) delega la inspección estructural del bundle recibido por SSH a `python3` (módulo `tarfile` de la librería estándar — ver el propio script) en vez de parsear la salida de `tar` por columnas. Es una dependencia real del host, no solo del entorno de desarrollo: sin `python3` en el `PATH` de `swarm-deploy`, todo intento de deploy falla en el momento de la inspección del bundle, antes de tocar nada.

La mayoría de las distros Debian/Ubuntu recientes ya traen Python 3 preinstalado, pero no lo asumas — verifícalo explícitamente:

```bash
command -v python3 || { echo "python3 no encontrado — instalar antes de continuar"; exit 1; }
python3 --version
```

Versión mínima soportada: **3.9** (usa sintaxis de type hints — `set[str]`, `dict`, etc. — disponible desde 3.9; no depende de nada más reciente). Si `python3 --version` reporta menos que eso, o el comando no existe:

```bash
apt-get update && apt-get install -y python3
```

Vuelve a correr `python3 --version` después de instalar y confirma que cumple el mínimo antes de seguir al siguiente paso. No continúes el bootstrap con esta verificación en rojo — es un prerrequisito directo de `ci-deploy-entrypoint.sh` (paso 12), y fallar temprano aquí es preferible a descubrirlo en el primer intento de deploy real.

## 11. Crear `swarm-deploy` — sin login humano

```bash
useradd --create-home --shell /bin/bash swarm-deploy
usermod -aG docker swarm-deploy
passwd -l swarm-deploy   # bloquea el login por contraseña a nivel de sistema — no tiene ninguna utilizable
mkdir -p /home/swarm-deploy/.ssh && chmod 700 /home/swarm-deploy/.ssh
chown -R swarm-deploy:swarm-deploy /home/swarm-deploy/.ssh
```

`swarm-deploy` queda en el grupo `docker` — en la práctica, equivalente a acceso root (puede montar `/` dentro de un contenedor). Es un trade-off aceptado y documentado en el [ADR-0011](../architecture/adr/0011-vps-staging-deployment.md), no un descuido — la mitigación real está en que ningún humano tiene la clave de esta cuenta y en las restricciones del paso 12.

## 12. Clave SSH de `swarm-deploy` — generada fuera del VPS

**Genera este par en tu propia máquina (la del operador haciendo el bootstrap), nunca en el VPS**, para que la privada no toque el disco del servidor en ningún momento:

```bash
# en tu máquina, en un directorio temporal
ssh-keygen -t ed25519 -f /tmp/swarm_deploy_ci_key -N ""
```

Sin passphrase — GitHub Actions la usa de forma no interactiva.

Copia la clave **pública** al VPS, en `authorized_keys` de `swarm-deploy`, con las restricciones ya decididas en el ADR:

```
command="/opt/swarm-trading/scripts/ci-deploy-entrypoint.sh",no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding ssh-ed25519 AAAA... swarm-deploy-ci
```

> **Nota importante:** `ci-deploy-entrypoint.sh` es el script que valida el bundle enviado por CI antes de desplegar nada (ver ADR-0011, sección "frontera de seguridad fija"). Su contenido se entrega en el PR 2 de esta fase — este paso del runbook se completa recién cuando ese PR esté mergeado. Hasta entonces, deja el `command=` apuntando a esa ruta igualmente; el archivo llegará ahí antes de que se use por primera vez. **No pruebes conexiones reales de `swarm-deploy` hasta que el script exista** — sin él, cualquier intento de conexión fallará por falta del ejecutable, lo cual es el comportamiento correcto (fail-closed, no fail-open).

La clave **privada** (`/tmp/swarm_deploy_ci_key`, sin extensión) se pega directamente en GitHub: Settings del repo → Environments → `staging` → Secrets → `STAGING_SSH_PRIVATE_KEY`. Un solo destino.

Después de confirmar que el secret quedó guardado:

```bash
shred -u /tmp/swarm_deploy_ci_key /tmp/swarm_deploy_ci_key.pub   # Linux
# en macOS, sin shred nativo: rm -P /tmp/swarm_deploy_ci_key /tmp/swarm_deploy_ci_key.pub
```

Limpia también el scrollback/historial de la terminal donde se mostró el contenido de la clave.

Por último, obtén la host key del VPS para pinearla del lado de GitHub (evita aceptar ciegamente la clave del servidor en la primera conexión):

```bash
ssh-keyscan -t ed25519 <VPS_IP>
```

El resultado va al secret `STAGING_SSH_KNOWN_HOSTS` (mismo Environment).

## 13. `sudo` acotado para los dos admins

```bash
visudo -f /etc/sudoers.d/swarm-deploy-admins
```

Contenido:

```
<admin1_user> ALL=(swarm-deploy) /opt/swarm-trading/scripts/deploy.sh, /opt/swarm-trading/scripts/rollback.sh
<admin2_user> ALL=(swarm-deploy) /opt/swarm-trading/scripts/deploy.sh, /opt/swarm-trading/scripts/rollback.sh
```

Sin `NOPASSWD` — cada admin debe escribir su propia contraseña local (paso 14) para elevar a `swarm-deploy`, incluso para estos dos comandos concretos. Es una confirmación deliberada extra antes de tocar el sistema real.

## 14. Contraseñas locales — solo para `sudo`, nunca para SSH

```bash
passwd <admin1_user>
passwd <admin2_user>
```

Cada uno la define para sí mismo, en el momento, sin compartirla con nadie más (ni siquiera contigo). El login SSH sigue siendo exclusivamente por clave (`PasswordAuthentication no`, paso 5) — estas contraseñas solo sirven para la elevación de `sudo` del paso 13.

## 15. Estructura de directorios

```bash
mkdir -p /opt/swarm-trading/{releases,shared,backups,logs,scripts}
chown -R swarm-deploy:swarm-deploy /opt/swarm-trading
chmod 750 /opt/swarm-trading
```

## 16. `shared/.env` — creación segura

```bash
su - swarm-deploy
cp /opt/swarm-trading/releases/<primer_sha_o_referencia>/.env.example /opt/swarm-trading/shared/.env 2>/dev/null || true
# si el paso anterior no aplica todavía (aún no hay ningún release), crea el archivo a mano
# usando .env.example del repo como referencia de qué variables existen
nano /opt/swarm-trading/shared/.env   # rellena con los valores reales de staging
chmod 600 /opt/swarm-trading/shared/.env
exit
```

Este archivo **nunca** se genera ni se toca desde CI/CD — vive solo aquí, se crea una vez, se edita a mano cuando haga falta. Ningún workflow de GitHub lo lee ni lo transfiere. Ningún bundle puede transportar un `.env` tampoco: `scripts/ci-deploy-entrypoint.sh` rechaza cualquier archivo fuera de su allowlist explícita (PR 2 de esta fase).

En cada deploy, `ci-deploy-entrypoint.sh` enlaza automáticamente `releases/<sha>/.env -> ../../shared/.env` — nunca copia el contenido — después de validar, en este orden, que `shared/.env` existe, que es un archivo regular (no un symlink), que no es legible por otros (`chmod 600`, como en el comando de arriba) y que pertenece al mismo usuario que corre el entrypoint (`swarm-deploy`). Si cualquiera de esas validaciones falla, el deploy se aborta ahí mismo — antes de tocar backup, migración o cualquier contenedor. Por eso los permisos `600` de este paso no son solo buena práctica: si se relajan (por ejemplo con un `chmod 644` accidental), el siguiente deploy se rechaza en vez de arrancar con un archivo de secretos legible por cualquiera.

## 17. Login persistente a GHCR

Con el token `read:packages` que generaste en GitHub (Settings → Developer settings → Personal access tokens):

```bash
su - swarm-deploy
docker login ghcr.io -u <tu_usuario_github>
```

Cuando pida `Password:`, pega el token ahí — la entrada queda oculta y no se guarda en el historial de shell. **No uses `echo "<token>" | docker login --password-stdin`**: aunque funciona, deja el token en texto plano en `.bash_history`.

Esto guarda las credenciales en `~swarm-deploy/.docker/config.json`. Aplica permisos restrictivos explícitamente — no asumas que el valor por defecto ya es correcto:

```bash
chmod 600 /home/swarm-deploy/.docker/config.json
ls -l /home/swarm-deploy/.docker/config.json   # confirma: legible solo por swarm-deploy
exit
```

Este login es **una sola vez**, aquí, en el bootstrap — el `GHCR_READ_TOKEN` nunca viaja por GitHub Actions en cada deploy.

## 18. Backups — preparación

Confirma que `pg_dump` funciona contra el stack real antes de depender de él (requiere que `docker-compose.yml` y `.env` ya estén desplegados — si todavía no hay ningún release, vuelve a este paso después del primer deploy manual, punto 19):

```bash
cd /opt/swarm-trading/releases/<sha>/
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  | gzip > /opt/swarm-trading/backups/test_$(date +%Y%m%dT%H%M%SZ).sql.gz
gzip -t /opt/swarm-trading/backups/test_*.sql.gz && echo OK
```

Retención acordada: últimos 14 backups (la poda la maneja `deploy.sh`, PR 2 — por ahora, si el directorio crece, revisa a mano).

## 19. Primer deploy manual — antes de automatizar nada

Este paso **depende de que los PR 2 (scripts) y al menos una imagen publicada por PR 3 estén disponibles**. No se puede completar solo con este PR 1. Cuando ambos estén listos:

1. Copia el bundle de un release (o constrúyelo a mano siguiendo la misma allowlist que usará CI) a `/opt/swarm-trading/releases/<sha>/`.
2. Corre `deploy.sh <sha>` directamente como `swarm-deploy` (no vía SSH todavía, sesión local) para validar la lógica completa: backup → migrate → swap → `/health/ready`.
3. Confirma `current -> releases/<sha>/` y que `curl -sf http://127.0.0.1:8000/health/ready` responde `{"status": "ok"}`.
4. Solo después de que esto funcione a mano, se prueba el camino por SSH con la clave de CI (PR 4).

## 20. Rollback manual

```bash
sudo -u swarm-deploy /opt/swarm-trading/scripts/rollback.sh <sha_al_que_volver>
```

El SHA objetivo es siempre explícito — el script nunca infiere "el anterior" por su cuenta, para que quede claro en el log a qué versión exacta se volvió y quién lo pidió.

## 21. Procedimiento de recuperación

Si en algún punto del hardening de SSH (paso 5) o del firewall (paso 6) quedas sin acceso:

1. Si todavía tienes **cualquier** sesión SSH abierta (root o admin), úsala para revertir:
   - Si el problema es `sshd_config`: `cp /etc/ssh/sshd_config.bak.<timestamp> /etc/ssh/sshd_config && sshd -t && systemctl reload sshd`.
   - Si el problema es el firewall: `ufw disable` (desactiva por completo; vuelve a intentar el paso 6 desde cero una vez resuelto lo que falló).
2. Si no queda ninguna sesión abierta, usa la consola de rescate/VNC del proveedor (la que confirmaste en el paso 0) — entra sin pasar por `sshd` en absoluto y corrige `sshd_config` o corre `ufw disable` manualmente desde ahí.
3. Nunca reinicies el droplet/instancia esperando que "se arregle solo" — ni un `sshd_config` roto ni un `ufw` mal configurado se arreglan con un reinicio; usa la consola de rescate primero.

## 22. Verificación final — checklist técnico

```bash
ssh root@<VPS_IP>                                    # debe fallar
ssh <admin1_user>@<VPS_IP> "sudo -l"                  # debe pedir contraseña y listar deploy.sh/rollback.sh
ssh <admin2_user>@<VPS_IP> "sudo -l"                  # ídem
visudo -cf /etc/sudoers.d/swarm-deploy-admins         # valida la sintaxis de forma independiente a la edición con visudo -f
ufw status verbose                                    # default deny incoming, SSH permitido y limitado
systemctl is-active fail2ban                          # active
docker --version && docker compose version
id swarm-deploy | grep docker                         # confirma el grupo
ls -ld /opt/swarm-trading                             # propietario swarm-deploy
stat -c '%a' /opt/swarm-trading/shared/.env           # 600
stat -c '%a' /home/swarm-deploy/.docker/config.json   # 600
docker --context default info 2>&1 | grep -i "Username"  # confirma login GHCR persistente, corrido como swarm-deploy
```

Cuando todo lo anterior verifique en verde, completa el checklist de handoff (`docs/deploy/staging-handoff-checklist.md`) y devuélvelo como evidencia.
