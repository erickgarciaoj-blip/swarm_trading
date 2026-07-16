# ADR-0003: Aislamiento de fallos en el hot-swap de modelos RL

**Estado:** Aceptado
**Fecha:** 2026-07-16
**Fase del roadmap:** Revisión final de Fase 0/1

## Contexto

Durante la revisión final de las Fases 0-1 se pidió verificar explícitamente si el hot-swap de modelos (ADR-0001) puede generar condiciones de carrera cuando un modelo se reemplaza mientras un agente está infiriendo. Esa revisión encontró dos hallazgos reales, no solo una confirmación de que "todo estaba bien":

1. **`PPO.load()`/construcción de un modelo fresco no estaban envueltos en executor.** `_ensure_model()` llamaba a `self._load_model(...)`/`self._build_fresh_model()` de forma síncrona dentro de un método también síncrono, invocado directamente (sin `await`) desde `analyze()`. Deserializar pesos de un `.zip` es I/O + trabajo de CPU no trivial — el mismo tipo de operación que ADR-0002 identificó como bloqueante, simplemente en un archivo que aún no existía cuando se escribió ADR-0002.
2. **No hay condición de carrera *dentro del proceso*** — el loop principal (`SwarmOrchestrator.run()`) es estrictamente secuencial: cada tick completa `await asyncio.gather(...)` antes de que empiece el siguiente, y cada agente aparece una sola vez en `self._agents`. Por diseño, `analyze()` (y por lo tanto `_ensure_model()`) de una misma instancia de `RLAgent` nunca se invoca dos veces en simultáneo.
3. **Sí hay una condición de carrera real, pero es externa al proceso:** un `scp` (u otra copia) no es atómico desde el punto de vista del proceso que lee el archivo. Si `_ensure_model()` hace `stat()` + `PPO.load()` mientras la copia está a mitad de camino, `PPO.load()` puede leer un `.zip` truncado/corrupto y lanzar una excepción — que, sin manejo explícito, tumbaría al agente (`analyze()` propagaría la excepción hacia `_process_agent`, que sí la captura con `except Exception`, así que no tumba el swarm completo, pero sí deja a ese agente sin operar ese tick, y sin ningún registro claro de por qué).

## Alternativas consideradas

1. **No hacer nada — confiar en que el deploy del modelo sea disciplinado** (ej. siempre usar `scp` a un archivo temporal + `mv`). Descartada como única mitigación: depende de que un humano nunca se equivoque en el proceso de deploy, y el código no tiene ninguna defensa si eso pasa.
2. **Bloquear la lectura con un lock de archivo (`fcntl`/similar) hasta que el deploy termine.** Descartada: requiere coordinación entre el proceso que escribe (fuera de este código, potencialmente en otra máquina) y el que lee — complejidad desproporcionada para un problema que tiene una solución mucho más simple.
3. **Degradación elegante: si el load falla, seguir sirviendo el último modelo bueno en memoria y reintentar en el siguiente tick** (la elegida), combinada con la recomendación operativa de que el deploy use un rename atómico.

## Decisión

`RLAgent._ensure_model()` se vuelve `async` y envuelve `_load_model()`/`_build_fresh_model()` en `asyncio.to_thread`. Si `_load_model()` lanza una excepción **y ya existe un modelo cargado en memoria**, se registra un warning y se sigue sirviendo ese modelo — `self._model_mtime` **no** se actualiza, así que el archivo se reintenta en cada tick subsiguiente hasta que una carga tenga éxito. Si es el primer load de la vida del agente (no hay modelo previo al cual volver), la excepción sí se propaga — no hay nada razonable que servir.

Recomendación operativa (no forzada por código, documentada para quien despliegue modelos): copiar a un nombre temporal y hacer `mv`/`os.replace` al nombre final, que es atómico en el mismo filesystem — así ningún `stat()` puede observar jamás un archivo a medio escribir.

## Ventajas

- Un deploy de modelo corrupto o interrumpido nunca saca a un agente de producción — se degrada a "sigue operando con el modelo anterior", nunca a "deja de operar".
- El reintento automático (mtime no avanza en fallo) significa que no hace falta intervención manual para que el hot-swap eventualmente tenga éxito una vez el archivo bueno esté completo.
- Corrige, de paso, el gap real de I/O bloqueante en `PPO.load()`/construcción de modelo que la Fase 1 no había cubierto.

## Desventajas

- Un modelo corrupto que se queda así indefinidamente generará warnings en cada tick hasta que se corrija — ruido de logs si nadie lo nota. Aceptable por ahora; con observabilidad (Fase 9) esto se vuelve una métrica/alerta en vez de solo un log.
- La recomendación de `mv` atómico no está forzada por el código — depende de disciplina operativa. Se documenta aquí y se puede formalizar más adelante como un script de deploy (`scripts/deploy_model.sh`) si hace falta, no es necesario todavía con un solo operador (tú).

## Consecuencias

- `agents/rl/rl_agent.py`: `_ensure_model()` async, con manejo de excepción y fallback.
- `agents/rl/rl_agent.py`: `analyze()` ahora hace `await self._ensure_model()`.
- Nuevos tests: `test_ensure_model_falls_back_to_previous_on_corrupt_checkpoint` (verifica fallback y reintento), y el test de hot-swap existente actualizado a `async`.
- Sobre fugas de memoria por recarga repetida (también parte de la revisión): no se encontró ningún patrón de fuga por código — cada hot-swap reasigna `self._model`, sin ninguna caché/registro externo reteniendo referencias al modelo anterior, así que el conteo de referencias de CPython libera el objeto viejo de inmediato; `PPO` en modo inferencia (sin entrenamiento) no retiene buffers de replay ni grafos de autograd. No se agregó ninguna limpieza manual (`gc.collect()`/`del` explícito) porque no hay evidencia de un ciclo de referencias que la justifique, y forzar GC en el hot path sin necesidad sería una pesimización, no una mejora. Esto queda como una conclusión de revisión de código, no una medición empírica — se verifica con datos reales una vez exista la métrica de "uso de memoria por agente" planeada para Fase 9.
