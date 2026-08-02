# Lógica del chatbot — Mail Boxes Etc (MBE) Costa del Este

Este documento explica, en términos generales, cómo responde Gia (el asistente
de WhatsApp) a los clientes, y qué se ajustó a partir de las notas de Fausto
("Notas preliminares chatbot MBE").

## 1. Cómo funciona por dentro

El bot recibe cada mensaje del cliente y sigue estos pasos:

1. **Saludo inicial:** si es la primera vez que ese cliente escribe, responde
   con un saludo natural (sin menús ni opciones numeradas).
2. **Router de intención:** decide si el mensaje es sobre *"estado de mis
   paquetes"* o sobre *"cualquier otra cosa"* (cotizaciones, horarios,
   servicios, dudas generales). Esta decisión toma en cuenta el historial
   reciente de la conversación, no solo el mensaje suelto (ver punto 2 abajo).
3. Según la intención, sigue por una de dos ramas:
   - **Paquetes** → flujo de rastreo (ver punto 3 abajo).
   - **Cualquier otra cosa** → un asistente conversacional que responde con la
     información del negocio (dirección, horario, servicios, cotizaciones,
     etc.), y que puede pasar la conversación a un supervisor si el cliente
     insiste en un tema fuera de lugar o la conversación se complica.

Todo el historial de la conversación viene de Chatwoot (no se guarda memoria
aparte), así que el bot recuerda lo que ya se habló aunque el servidor se
reinicie.

## 2. Ajuste: no perder el hilo de una conversación en curso

**Reportado por Fausto:** la conversación "reiniciaba muy rápido" — si un
cliente ya estaba, por ejemplo, cotizando un envío, y en algún momento
mencionaba de pasada algo relacionado con "paquete", el bot saltaba al flujo
de rastreo y el cliente sentía que había perdido todo el contexto.

**Cómo quedó:** el router ahora recibe el historial reciente de la
conversación junto con el mensaje nuevo. Si ya hay una conversación activa
sobre otro tema (por ejemplo, una cotización a medio recolectar) y el mensaje
nuevo es ambiguo, el bot **no** salta al flujo de rastreo — sigue en el tema
en el que estaba. Solo cambia a rastreo cuando el cliente pregunta, sin
ambigüedad, por el estado de un envío.

## 3. Ajuste: pedir el tracking antes de mandar el detalle

**Reportado por Fausto:** *"La principal respuesta es pedir el número de
rastreo del paquete, no mandar el detalle de todo."*

**Cómo quedó — nuevo flujo de rastreo:**

1. Cuando el bot detecta que el cliente pregunta por sus paquetes, **ya no
   manda el detalle de una vez**. Primero responde pidiendo el número de
   tracking/guía.
2. Cuando el cliente contesta con ese número, el bot busca **puntualmente
   ese paquete** en el sistema y responde solo con su detalle (estado,
   tracking, peso, volumen, costo con impuesto si aplica).
3. Si el cliente dice que no tiene el número a la mano (ej. "no lo tengo",
   "no sé"), el bot usa como respaldo la búsqueda anterior por número de
   WhatsApp, y muestra todos los paquetes asociados a ese teléfono.
4. Si el bot no encuentra el tracking en el sistema, ya no le pide que lo
   confirme ni insiste — pasa la conversación directo con una persona del
   equipo (ver punto 5), para no quedarse dando vueltas si el cliente se
   equivoca al escribir el número.

Este estado ("le acabo de pedir el tracking, esto que me manda es la
respuesta") se calcula revisando si el último mensaje del propio bot en el
historial fue esa pregunta — no se guarda en un lugar aparte, por lo que
sigue funcionando igual aunque el servidor en Cloud Run se reinicie o
escale a otra instancia.

## 4. Ajuste: checklist explícito de la cotización de envío

**Reportado por Fausto:** *"Con un envío, siento que le costó recolectar la
información, pareciera que no puede manejar mensajes en cadena y siento que
no estaba recordando la información."*

**Cómo quedó:** el prompt del asistente general (`GENERAL_PROMPT`) ahora
incluye una sección explícita de "cotización de envío" con los 4 datos
requeridos (tipo de carga, peso, volumen, costo del artículo con impuestos).
Se le indica al modelo que, antes de pedir el siguiente dato, revise **todo**
el historial de la conversación (no solo el último mensaje) para no volver a
pedir algo que el cliente ya dio, aunque lo haya mandado en mensajes
separados. Esto se apoya en que el historial completo de Chatwoot ya se le
pasa al modelo en cada turno (ver punto 1).

## 5. Ajuste: handoff automático (bucles y cotización completa)

**Reportado por Fausto:** *"Se quedó pegada y tuve que volver a darle la
información, me gustaría que siempre que identifique problemas o se repita
la misma pregunta, su respuesta sea pasar la conversación. En esta ocasión
apenas reciba toda la información necesaria debe pasar la conversación para
darle el estimado al cliente."*

**Cómo quedó:** se agregaron dos disparadores nuevos de handoff automático
(mismo mecanismo `[[HANDOFF]]` que ya existía para "fuera de tema"):

- **Bucle o pregunta repetida:** si el modelo está por volver a pedir un dato
  que el cliente ya dio antes en el historial, o nota que está atascado
  repitiendo la misma pregunta dentro del mismo tema, hace handoff en vez de
  insistir.
- **Cotización completa:** en cuanto el modelo tiene los 4 datos de la
  cotización, no sigue conversando ni inventa un estimado — le avisa al
  cliente que ya tiene todo lo necesario y pasa la conversación para que una
  persona confirme el costo exacto.

Además, el flujo de tracking (punto 3) también dispara este mismo handoff
cuando el número que da el cliente no se encuentra en el sistema.

Detalle técnico completo de estos puntos en `fausto_cambios_v1.md`.

## 6. Pendiente / a confirmar

- Validar con un caso de prueba real (mensajes en cadena) que el checklist de
  cotización del punto 4 funciona como se espera en producción.
- Confirmar qué valor tiene la variable de entorno `CHATWOOT` en el servicio
  de Cloud Run desplegado: todos estos ajustes dependen de que el historial
  de la conversación (`history`) llegue al cerebro, y eso solo pasa hoy por
  la ruta `/chatwoot` (ver `main.py`). Si el servicio corriera en modo
  directo de WhatsApp (`CHATWOOT=false`), estos ajustes no aplicarían.
