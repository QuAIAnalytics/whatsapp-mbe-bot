# Plan de implementación — cambios pedidos por Fausto (v1)

Fuente: `notas_fausto.docx` ("Notas preliminares chatbot MBE").

Archivos involucrados: `clients/mbe.py`, `ai.py` (sin cambios de fondo, ya soporta
historial externo desde Chatwoot).

---

## 1. La conversación "reinicia muy rápido"

**Nota de Fausto:** *"Reinicia la conversación muy rápido. Lo más probable esto
es por falta de información pero, debe poder filtrar si es un cliente de
paquetería o de algún otro servicio (también puede existir un cliente de
paquetes con otras consultas)."*

**Diagnóstico:** el router (`_classify` en `clients/mbe.py:78`) clasifica
**cada mensaje** como `PACKAGES` u `OTHER` de forma aislada, sin ver el
historial de la conversación. Si un cliente ya venía hablando de una cotización
de envío (`OTHER`, con memoria/historial vía `ai.chat_reply`) y en algún
momento menciona algo que suena a "paquete", el router lo manda por la rama
`PACKAGES`, que **no usa historial ni memoria** (`_packages_reply` no llama a
`ai.chat_reply`). Desde el punto de vista del cliente esto se siente como que
"la conversación se reinició": perdió todo el contexto de la cotización.

**Cambio propuesto:**
- El router debe considerar el turno en curso *más* si ya hay una conversación
  activa de paquetes o de otro tipo, para no saltar de una rama a otra a mitad
  de flujo. En concreto: si ya estamos dentro de un flujo de rastreo (p. ej.
  esperando el número de guía, ver punto 4) o dentro de un flujo de cotización
  con datos parcialmente recolectados, un mensaje ambiguo debe interpretarse
  primero como continuación de ese flujo, no reclasificarse desde cero.
- Sí puede haber "cliente de paquetes con otra consulta": eso es válido y debe
  seguir funcionando (ej. pregunta por su paquete y luego por horario). El
  punto es no perder el hilo de una conversación ya en curso.
- Requiere guardar un pequeño estado de conversación por teléfono (ej. "en
  medio de flujo de rastreo, esperando tracking" / "en medio de cotización,
  faltan estos datos") para que el router no dependa solo de clasificar el
  mensaje suelto.

---

## 2. Cotización de envío: no recolecta bien la información en mensajes en cadena

**Nota de Fausto:** *"Con un envío, siento que le costó recolectar la
información, pareciera que no puede manejar mensajes en cadena y siento que no
estaba recordando la información."*

**Diagnóstico:** el flujo de cotización hoy vive completo dentro de
`GENERAL_PROMPT` (`clients/mbe.py:43-67`), sin estructura: se le pide al modelo,
por instrucciones de `negocio_mbe.txt`, que recolecte tipo de carga, peso,
volumen y costo del artículo, todo dentro de una conversación libre. No hay
checklist explícito de qué datos ya tiene y cuáles faltan, así que el modelo
puede perder de vista un dato mencionado 2-3 mensajes atrás, especialmente si
el cliente los manda en mensajes separados ("marítima", luego "pesa 3kg", luego
"cuesta 50 dólares") en vez de todo junto.

**Cambio propuesto:**
- Agregar al `GENERAL_PROMPT` una sección explícita de "cotización de envío"
  con los 4 datos requeridos (tipo de carga, peso, volumen, costo del
  artículo con impuestos) y la instrucción de ir confirmando cuáles ya tiene
  y cuáles le faltan en cada respuesta (esto ayuda a que el propio modelo
  mantenga el checklist en vez de depender de memoria implícita).
- Confirmar que el historial de Chatwoot (ya reconstruido según el commit
  `f4542d5`) se está pasando correctamente en cada turno de este flujo —
  validar con un caso de prueba real de cotización en varios mensajes.

---

## 3. Handoff automático ante bucles / preguntas repetidas, y al completar datos de cotización

**Nota de Fausto:** *"Se quedó pegada y tuve que volver a darle la
información, me gustaría que siempre que identifique problemas o se repita la
misma pregunta, su respuesta sea pasar la conversación. En esta ocasión apenas
reciba toda la información necesaria debe pasar la conversación para darle el
estimado al cliente."*

**Cambio propuesto (dos disparadores nuevos de `[[HANDOFF]]`):**
1. **Detección de bucle:** si el asistente está a punto de repetir una
   pregunta que ya hizo (mismo dato ya fue pedido antes en el historial y el
   cliente ya lo dio, o el modelo nota que está "atascado"), en vez de
   insistir debe hacer handoff inmediato. Esto se agrega como instrucción
   explícita en `GENERAL_PROMPT`, separada de la regla actual de "fuera de
   tema" (`clients/mbe.py:59-63`), que hoy solo dispara handoff por insistencia
   fuera de tema, no por bucles dentro del mismo tema.
2. **Cotización completa → handoff, no estimado del bot:** en cuanto el
   modelo tenga los 4 datos de la cotización (tipo de carga, peso, volumen,
   costo del artículo), la respuesta correcta ya no es seguir conversando ni
   inventar un número — es avisar al cliente que ya tiene todo y pasar la
   conversación con `[[HANDOFF]]` para que una persona dé el estimado exacto.
   Esto es consistente con la regla que ya existe en `negocio_mbe.txt:41` de
   "no inventes precios", solo que ahora el handoff debe ser automático en
   vez de reactivo.

**Nota de implementación:** ambos casos reusan el mecanismo `[[HANDOFF]]` /
`ai.HANDOFF_REQUESTS` que ya existe (`clients/mbe.py:227-230`), solo se amplía
cuándo se dispara.

---

## 4. Flujo de rastreo de paquetes: pedir el número de guía primero

**Nota de Fausto:** *"Como trabajamos esta parte del módulo? La principal
respuesta es pedir el número de rastreo del paquete, no mandar el detalle de
todo."*

**Diagnóstico:** hoy, en cuanto el router detecta intención `PACKAGES`,
`handle()` llama directo a `_packages_reply(phone)` (`clients/mbe.py:214-217`),
que busca por teléfono en el Sheet y devuelve **todos** los paquetes
encontrados con tracking, estado, peso, volumen y costos, en un solo mensaje.

**Cambio propuesto:**
- Cuando el intent es `PACKAGES`, la primera respuesta del bot debe ser pedir
  el número de guía/tracking al cliente, no disparar el lookup ni mandar el
  detalle completo.
- Cuando el cliente da el número de tracking, buscar ese tracking puntual en
  el Sheet (nueva función de búsqueda por tracking, en vez de por teléfono) y
  responder solo con el detalle de ese paquete.
- Si el cliente no tiene el número a mano, como respaldo se puede ofrecer el
  lookup por teléfono actual (`_fetch_packages`), pero ya no como primera
  opción.
- Esto requiere manejar un estado de "esperando tracking" por teléfono, similar
  al mencionado en el punto 1, para que el siguiente mensaje del cliente
  (que puede ser solo el número, sin contexto) se interprete correctamente
  como respuesta a esa pregunta y no se re-clasifique como `OTHER`.

---

## Resumen de cambios de código

| # | Cambio | Archivo(s) |
|---|--------|-----------|
| 1 | Estado de conversación por teléfono para no perder el hilo entre flujos | `clients/mbe.py` |
| 2 | Checklist explícito de datos de cotización en el prompt | `clients/mbe.py` (`GENERAL_PROMPT`) |
| 3 | Handoff automático por bucle/pregunta repetida y al completar cotización | `clients/mbe.py` (`GENERAL_PROMPT`) |
| 4 | Pedir tracking primero; búsqueda por tracking en vez de por teléfono como default | `clients/mbe.py` (`handle`, `_fetch_packages` / nueva función) |

## Pendiente de confirmar con Fausto antes de implementar
- Punto 1 y 4 comparten la necesidad de un "estado de conversación" simple
  por teléfono (ej. `esperando_tracking`, `cotizacion_en_curso`). ¿Se guarda
  en RAM (como `GREETED` hoy) o debe sobrevivir reinicios igual que el
  historial de Chatwoot?
- Punto 4: ¿el lookup por teléfono como respaldo se mantiene, o se elimina del
  todo y siempre se pide tracking?
