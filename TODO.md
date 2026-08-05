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

Por eso `_cw_conversation_messages` (`main.py`) lee el historial vía
`GET /conversations/{id}` (acción "show", sí permitida para bots) en vez de
`GET /conversations/{id}/messages` (acción "index", bloqueada para bots) —
"show" ya trae los mensajes recientes adentro. Pendiente de verificar en la
práctica (con el log que deja `_cw_conversation_messages`) si ese array trae
suficiente historial para conversaciones largas, o si hace falta otra
estrategia (Chatwoot no documenta un límite fijo para ese array).

Si en algún momento `CHATWOOT_API_TOKEN` vuelve a ser el de un agente humano
dedicado (no un Agent Bot real), `_cw_is_other_human` ya contempla ese caso
comparando el `sender.id` contra el ID propio (vía `GET /api/v1/profile`) —
pero ese endpoint de perfil probablemente tampoco está permitido para un
Agent Bot real, así que con un Agent Bot esa parte del código simplemente no
se ejecuta (el chequeo de `sender_type` ya filtra antes de llegar ahí).
