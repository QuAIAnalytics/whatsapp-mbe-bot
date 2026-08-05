# Pendientes

## Formato real del número de tracking/guía

`clients/mbe.py` (`_TRACKING_CANDIDATE_RE`) hoy reconoce como "posible tracking"
cualquier bloque de letras/números/guiones de al menos 5 caracteres dentro del
mensaje del cliente — es una heurística genérica para no exigir que el
mensaje completo sea *exactamente* el tracking (ver `_extract_tracking_candidates`
en `_tracking_followup_reply`).

Cuando el cliente confirme las características reales del tracking de MBE
(largo fijo, si tiene prefijo/letras, si siempre es numérico, etc.), hay que
ajustar esa expresión regular para que sea más precisa y evite falsos
positivos (ej. que confunda un número de teléfono o de guía de otra empresa
mencionado de pasada con el tracking real).

## Permisos de los tokens de Chatwoot (Agent Bot vs. agente normal)

Un token de **Agent Bot** de Chatwoot tiene una lista de acciones permitidas
muy corta (ver `access_token_auth_helper.rb` del repo de Chatwoot):
conversaciones (`show`, `toggle_status`, `toggle_typing_status`,
`toggle_priority`, `create`, `update`, `custom_attributes`), mensajes de
conversación (**solo `create`**, NO `index`/listar), asignaciones (`create`),
y etiquetas (`index`, `create`).

**Probado y descartado (2026-08-05):** intentamos leer el historial vía
`GET /conversations/{id}` (acción "show", sí permitida para bots) en vez de
`GET /conversations/{id}/messages` (acción "index", bloqueada para bots),
esperando que "show" trajera los mensajes recientes adentro. En la práctica
**solo trae 1 mensaje** (no documentado en la API, comprobado en logs
reales: `"conv X: 1 mensajes en 'show'"`) — insuficiente para la memoria de
la conversación, causó que el bot se volviera a presentar a mitad de una
cotización. Se revirtió a `GET /conversations/{id}/messages`.

**Conclusión:** con las acciones que Chatwoot permite hoy a un Agent Bot, no
hay forma de leer suficiente historial. Mientras sigamos necesitando
memoria de conversación completa, `CHATWOOT_API_TOKEN` tiene que ser el
token de un agente humano dedicado (no un Agent Bot real) — es la única
combinación que funciona con lo que expone la API. `_cw_is_other_human` ya
contempla ese caso comparando el `sender.id` contra el ID propio (vía
`GET /api/v1/profile`) para no autosilenciarse.
