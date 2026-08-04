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
