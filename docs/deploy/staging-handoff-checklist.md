# Checklist de handoff — Bootstrap VPS staging (Fase 4.6)

Para el operador del VPS. Marca cada casilla a medida que completas el paso correspondiente del [runbook](staging-vps-bootstrap.md) y devuelve este archivo (o su contenido) como evidencia de que el bootstrap quedó completo. No hace falta entender el porqué de cada paso para completarlo — el runbook lo explica; esto es solo el registro de que se hizo.

## Datos del VPS (rellenar, no son secretos salvo que se indique)

- Proveedor: ____________________
- IP pública: ____________________
- Dominio (si existe; puede quedar vacío en esta fase): ____________________
- Sistema operativo y versión: ____________________
- Arquitectura de CPU (x86_64 / arm64): ____________________
- Consola de rescate/VNC del proveedor disponible: Sí / No — cómo se accede: ____________________
- Puerto SSH final (tras el hardening, si cambia del 22): ____________________

## Acceso inicial

- [ ] Snapshot de `sshd_config` guardado antes de cualquier edición (paso 1).
- [ ] Confirmado el método de acceso de emergencia (consola out-of-band del proveedor) antes de tocar SSH.

## Cuentas humanas

- [ ] Cuenta creada para admin 1 — usuario: ____________________
- [ ] Cuenta creada para admin 2 — usuario: ____________________
- [ ] Clave pública de admin 1 instalada en su propia cuenta (no en la del otro admin, no en `swarm-deploy`).
- [ ] Clave pública de admin 2 instalada en su propia cuenta.
- [ ] Acceso de ambos admins probado y confirmado **antes** de tocar `sshd_config` (paso 4 del runbook).

**Evidencia** — resultado real de `ssh <admin>@<VPS_IP>` para cada uno:
```
admin 1:

admin 2:
```

## Hardening SSH

- [ ] `PermitRootLogin no` aplicado.
- [ ] `PasswordAuthentication no` aplicado.
- [ ] `AllowUsers` restringido a los dos admins + `swarm-deploy`.
- [ ] `sshd -t` validado sin errores antes de recargar.
- [ ] `systemctl reload sshd` (no `restart`) usado para aplicar.
- [ ] Verificado, con la sesión original todavía abierta: login root rechazado, login de ambos admins sigue funcionando, login por contraseña rechazado.
- [ ] Sesión root original cerrada solo después de la verificación anterior.

**Evidencia** — salida efectiva de `sshd` (lo que el daemon aplicó realmente, no solo el archivo de config) y el resultado de los cuatro intentos de conexión del paso 5 del runbook:
```
sshd -T | grep -Ei "permitrootlogin|passwordauthentication|allowusers":


ssh root@<VPS_IP> →

ssh <admin1>@<VPS_IP> →

ssh <admin2>@<VPS_IP> →

ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password <admin1>@<VPS_IP> →
```

## Firewall y hardening del sistema

- [ ] Regla de SSH confirmada con `ufw show added` **antes** de correr `ufw enable` (paso 6 del runbook).
- [ ] `ufw` con default-deny entrante, SSH permitido y con rate-limit (`ufw limit`).
- [ ] Sesión SSH probada en una terminal nueva después de `ufw enable`, con la original todavía abierta antes de cerrarla.
- [ ] Firewall de red del proveedor configurado también, si el proveedor lo ofrece.
- [ ] Puertos 80/443/5432/6379 **no** abiertos en el firewall.
- [ ] 8000 y 8080 **no** tienen regla de firewall — quedan ligados a `127.0.0.1` por `docker-compose.yml`, no necesitan ni deben abrirse.
- [ ] `fail2ban` instalado y activo (`systemctl is-active fail2ban` → `active`).
- [ ] `unattended-upgrades` configurado para parches de seguridad del SO.

**Evidencia** — salida real:
```
ufw status verbose:


systemctl is-active fail2ban:
```

## Docker

- [ ] Docker Engine instalado desde el repositorio oficial (no `docker.io` del SO).
- [ ] `docker compose version` funciona.

## Usuario técnico `swarm-deploy`

- [ ] Cuenta creada, sin contraseña utilizable (`passwd -l`).
- [ ] Añadida al grupo `docker`.
- [ ] Clave SSH generada **fuera del VPS** (en la máquina del operador, no en el servidor).
- [ ] Clave pública instalada en `authorized_keys` de `swarm-deploy` con `command=` forzado hacia `ci-deploy-entrypoint.sh` + `no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding`.
- [ ] Clave privada pegada en GitHub → Environment `staging` → secret `STAGING_SSH_PRIVATE_KEY`.
- [ ] Copias locales de la clave privada borradas (`shred -u` o equivalente) tras confirmar el secret guardado.
- [ ] Host key del VPS obtenida (`ssh-keyscan`) y guardada en el secret `STAGING_SSH_KNOWN_HOSTS`.
- [ ] **Nota:** el archivo `ci-deploy-entrypoint.sh` llega en el PR 2 de esta fase — esta sección queda "pendiente de contenido" hasta que ese PR esté mergeado. No marcar como probado hasta entonces.

## `sudo` acotado

- [ ] `/etc/sudoers.d/swarm-deploy-admins` creado con `visudo -f` (nunca editado directamente).
- [ ] Reglas limitadas exactamente a `deploy.sh` y `rollback.sh`, sin `NOPASSWD`.
- [ ] Cada admin tiene su propia contraseña local, distinta, usada solo para `sudo` (nunca para login SSH).
- [ ] `visudo -cf /etc/sudoers.d/swarm-deploy-admins` corrido como verificación independiente (sección 21 del runbook), sin errores.

**Evidencia** — salida real:
```
visudo -cf /etc/sudoers.d/swarm-deploy-admins:


ssh <admin1>@<VPS_IP> "sudo -l" →

ssh <admin2>@<VPS_IP> "sudo -l" →
```

## Estructura y datos

- [ ] `/opt/swarm-trading/{releases,shared,backups,logs,scripts}` creada, propiedad de `swarm-deploy`.
- [ ] `shared/.env` creado con los valores reales de staging, `chmod 600`.
- [ ] Confirmado que `shared/.env` **no** está en ningún repositorio git.
- [ ] Login persistente a GHCR realizado como `swarm-deploy` con `docker login` interactivo (nunca `echo <token> | docker login --password-stdin`).
- [ ] `~swarm-deploy/.docker/config.json` con `chmod 600` aplicado explícitamente, no solo verificado.

**Evidencia** — salida real:
```
stat -c '%a' /opt/swarm-trading/shared/.env:


stat -c '%a' /home/swarm-deploy/.docker/config.json:
```

## Backups

- [ ] `pg_dump` de prueba corrido y verificado (integridad gzip, tamaño no cero) — puede quedar pendiente hasta que exista el primer release.

## Verificación final

- [ ] Todos los comandos de la sección 21 del runbook corridos y en el estado esperado.
- [ ] Este checklist devuelto, con los campos de "Datos del VPS" completos, como evidencia de bootstrap terminado.

## Pendiente explícito hasta PR 2/3/4

- [ ] Primer deploy manual (paso 18 del runbook) — requiere `deploy.sh`/`rollback.sh` (PR 2) y una imagen publicada en GHCR (PR 3).
- [ ] Prueba de conexión real con la clave de CI — requiere `ci-deploy-entrypoint.sh` instalado (PR 2) y el job de deploy en el workflow (PR 4).

No se espera que estas dos últimas casillas estén marcadas al entregar este checklist — el bootstrap del VPS (este PR) puede darse por completo sin ellas.
