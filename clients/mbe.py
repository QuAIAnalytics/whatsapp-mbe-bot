"""
clients/mbe.py — Asistente de Mail Boxes Etc (MBE)
==================================================
Flujo distinto al de QUAI:

    1. Saluda de forma natural y conversacional (sin menús ni opciones numeradas).
    2. Un "router" entiende, por lenguaje natural, si el cliente está preguntando
       por el estado de sus paquetes (usa el historial reciente para no perder
       el hilo si el cliente ya está en medio de otro flujo, ej. una cotización).
    3. Si es así -> primero se le pide el número de tracking/guía (no se manda el
       detalle completo de una vez). Cuando el cliente responde con el tracking,
       un sub-agente liviano busca ESE paquete puntual en el Google Sheet. Si el
       cliente dice que no tiene el número a la mano, se hace de respaldo el
       lookup por su número de WhatsApp (comportamiento anterior).
    4. Para cualquier otra cosa -> un asistente conversacional general de MBE, que
       responde con la info de negocio_mbe.txt.

La info del negocio (dirección, horario, servicios) vive en negocio_mbe.txt, un
archivo de texto editable sin tocar código.

Configuración (.env):
    MBE_INFO_PATH       ruta al .txt con la info (default: negocio_mbe.txt)
    MBE_SHEET_CSV_URL   URL del Google Sheet publicado como CSV (ver README).
"""

import csv
import io
import os
import re

import requests

import ai

# Info del negocio (editable en negocio_mbe.txt, sin tocar código).
BUSINESS_INFO = ai.load_info("MBE_INFO_PATH", "negocio_mbe.txt")

# Teléfonos que ya recibieron el saludo inicial.
GREETED: set[str] = set()

# Teléfonos a los que ya se les pidió el tracking y estamos esperando su respuesta.
# Solo se usa cuando no hay `history` (modo sin Chatwoot); con `history` el estado
# se deriva del propio historial (ver `_bot_last_asked_tracking`), que es lo que
# corre en producción (Cloud Run + Chatwoot) y sí sobrevive reinicios/instancias.
WAITING_TRACKING: set[str] = set()

# Saludo natural de primer contacto (cálido, sin menús ni opciones numeradas).
WELCOME = (
    "Hola, qué tal. Te habla Gia de Mail Boxes Etc Costa del Este. "
    "¿En qué te puedo ayudar hoy?"
)

# Primera respuesta cuando el cliente pregunta por sus paquetes: se pide el
# tracking antes de mandar cualquier detalle (punto 4 de fausto_cambios_v1.md).
ASK_TRACKING_MSG = (
    "Claro, ¿me compartes el número de tracking o guía del paquete? "
    "Si no lo tienes a la mano, dime y lo reviso con tu número de teléfono."
)

# Frases con las que el cliente indica que no tiene el número de tracking a mano.
NO_TRACKING_PHRASES = (
    "no tengo", "no lo tengo", "no se", "no sé", "no cuento",
    "no lo encuentro", "no lo tengo a la mano", "no tengo el numero", "no tengo el número",
)

# Asistente general (cuando NO está consultando paquetes).
GENERAL_PROMPT = f"""Te llamas Gia y eres parte de la atención al cliente de Mail Boxes Etc (MBE) Costa del Este, en Panamá.
Atiendes a clientes por WhatsApp.

Estilo (muy importante):
- Actúa como una persona real del local, nunca uses frases obvias de inteligencia artificial (ej: "Soy una inteligencia artificial", "Como modelo de lenguaje", "Asistente virtual", etc.). Eres una persona del equipo.
- Hablas en español de Panamá, cálido, cercano y natural.
- Sin emojis y sin signos de exclamación. Respuestas cortas, como en un chat real.
- Nada robótico: no des menús, no digas "marca 1" ni "escribe la palabra X". Conversa normal.
- No inventes precios, plazos ni datos que no estén abajo. Si no lo sabes o no tienes la información, no digas que es un error; simplemente indícale amablemente al cliente que vas a elevar su consulta con el área correspondiente para que le den seguimiento.
- Si el cliente pregunta por otra sucursal o no es de Costa del Este, indícale con amabilidad
  que aquí se atiende solo Costa del Este y comparte el localizador de centros.
- Si el historial de la conversación está vacío (es la primera vez que te escribe este cliente),
  empieza tu respuesta con un saludo breve, presentándote como parte del equipo de Mail Boxes
  Etc Costa del Este, antes de atender lo que te está preguntando.

FUERA DE TEMA:
- Solo atiendes temas de Mail Boxes Etc (envíos, paquetes, casillero, cotizaciones, horarios, etc.).
- Si el cliente pregunta algo que no tiene nada que ver con esto, coméntale con cortesía que
  aquí solo puedes ayudarle con temas de Mail Boxes Etc, e invítalo a volver a ese tema.
- Si, después de eso, el cliente insiste en lo mismo fuera de tema, o la conversación se pone
  complicada y no la puedes resolver con la información que tienes, dile con amabilidad y de forma
  explícita que lo vas a pasar con un supervisor para que lo atienda (usa la palabra "supervisor").
  En ese caso, al final de tu respuesta agrega en una línea aparte, exactamente así: [[HANDOFF]]
  (esa marca es una señal interna, el cliente nunca debe verla ni debes mencionarla).

COTIZACIÓN DE ENVÍO (muy importante, punto 2 de fausto_cambios_v1.md):
- Para cotizar un envío necesitas exactamente estos 4 datos: (1) tipo de carga
  (marítima o aérea), (2) peso del paquete, (3) volumen del paquete, (4) costo
  del artículo incluyendo impuestos.
- El cliente puede darte estos datos en mensajes separados (uno por mensaje, o
  varios juntos). Antes de pedir el siguiente dato, revisa TODO el historial de
  la conversación (no solo el último mensaje) para ver cuáles de los 4 ya te
  dio, y no vuelvas a pedir uno que ya tienes.
- En cada respuesta dentro de este flujo, ten claro mentalmente cuáles de los 4
  datos ya tienes y cuáles faltan, y pide únicamente los que falten (uno o
  varios a la vez, sin repetir los que ya te dieron).
- No calcules ni des un estimado de costo tú mismo: cuando ya tengas los 4
  datos, dile al cliente que ya tienes todo lo necesario y que le van a
  confirmar el costo exacto, y haz handoff (ver "HANDOFF AUTOMÁTICO
  ADICIONAL" abajo, punto de "cotización completa").

HANDOFF AUTOMÁTICO ADICIONAL (punto 3 de fausto_cambios_v1.md):
Además de la regla de "fuera de tema" de arriba, haz handoff automático
(mismo mecanismo: agrega [[HANDOFF]] en una línea aparte al final de tu
respuesta) en estos dos casos:
- Bucle o pregunta repetida: si, revisando el historial, notas que estás por
  volver a pedir un dato que el cliente ya te dio antes, o que te vas a repetir
  preguntando lo mismo sin avanzar (estás "atascado" dentro del mismo tema, no
  fuera de tema), no insistas ni repitas la pregunta. En su lugar, dile con
  amabilidad que lo vas a pasar con un supervisor y agrega [[HANDOFF]].
- Cotización completa: en cuanto tengas los 4 datos de la cotización de envío
  (tipo de carga, peso, volumen, costo del artículo), no sigas conversando ni
  inventes un estimado; avísale al cliente que ya tienes todo lo necesario y
  que le van a confirmar el costo exacto, y agrega [[HANDOFF]] en esa misma
  respuesta.

INFORMACIÓN DEL NEGOCIO:
{BUSINESS_INFO}
"""

# Router de intención: detecta, por lenguaje natural, si pregunta por sus paquetes.
ROUTER_PROMPT = """Lees el mensaje mas reciente de un cliente de una empresa de paqueteria,
junto con el historial reciente de la conversacion (si lo hay), y decides la intencion de
ESE mensaje nuevo. Responde UNA sola palabra, sin explicar:
- PACKAGES  si quiere saber por el estado, ubicacion, llegada o seguimiento de sus paquetes,
            pedidos o envios (ej: "llego mi paquete?", "donde esta mi envio", "tengo algo pendiente?").
- GREETING  si el mensaje es solo un saludo o cortesia, sin ninguna pregunta o pedido real
            (ej: "hola", "buenas", "que tal", "buenos dias").
- OTHER     para cualquier otra cosa con una pregunta o pedido real (dudas de servicios,
            horarios, cotizaciones, precios, etc), aunque venga acompañado de un saludo.

Importante - no reclasifiques a mitad de un flujo ya en curso: si el historial muestra que
ya hay una conversacion activa sobre otro tema (por ejemplo una cotizacion de envio donde se
estan recolectando datos como tipo de carga, peso, volumen o costo), y el mensaje nuevo es
ambiguo o solo menciona de pasada una palabra relacionada con "paquete" o "envio", NO lo
clasifiques como PACKAGES: responde OTHER para que esa conversacion continue sin perder el
hilo. Responde PACKAGES unicamente cuando el cliente este preguntando, sin ambiguedad, por el
estado o rastreo de un envio.
"""


def _classify(text: str, history: list | None = None) -> str:
    prompt_text = text
    if history:
        # Usamos la misma ventana que el resto del flujo (la que ya viene
        # acotada a 15 mensajes desde cw_fetch_history), en vez de cortarla
        # más todavía: si el router ve menos contexto que el flujo de
        # cotización, puede "olvidar" que hay una cotización en curso antes
        # que el resto del sistema y reclasificar mal (ver punto 1 de
        # fausto_cambios_v1.md).
        convo = "\n".join(f"{h['role']}: {h['text']}" for h in history)
        prompt_text = f"Historial reciente:\n{convo}\n\nMensaje nuevo del cliente: {text}"
    answer = (ai.ask_once(ROUTER_PROMPT, prompt_text) or "").upper()
    if "PACKAGES" in answer:
        return "PACKAGES"
    if "GREETING" in answer:
        return "GREETING"
    return "OTHER"


def _only_digits(value: str) -> str:
    return "".join(c for c in str(value) if c.isdigit())


def _looks_like_phone_column(name: str) -> bool:
    name = (name or "").lower()
    return any(k in name for k in ("telefono", "teléfono", "phone", "celular", "whatsapp", "numero", "número"))


# Palabras clave para reconocer cada columna del Sheet (no importa el nombre exacto).
TRACKING_KEYS = ("tracking", "guia", "guía", "rastreo", "seguimiento")
ESTADO_KEYS   = ("estado", "status")
VOLUMEN_KEYS  = ("volumen", "volume", "vol")
PESO_KEYS     = ("peso", "weight")
# Para el costo buscamos primero "neto", luego variantes generales.
COSTO_KEYS    = ("neto", "costo", "monto", "valor", "precio")

IMPUESTO_RATE = 0.07  # 7%


def _find(row: dict, keys: tuple) -> str | None:
    """Devuelve el valor de la primera columna cuyo encabezado contiene alguna keyword."""
    for key in keys:
        for col, value in row.items():
            if key in (col or "").lower() and str(value).strip():
                return str(value).strip()
    return None


def _parse_money(text: str | None) -> float | None:
    """Convierte '$1,234.56' o '1234.56' a número. Devuelve None si no se puede."""
    if not text:
        return None
    cleaned = "".join(c for c in text if c.isdigit() or c in ".,").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fetch_packages(phone: str) -> list[dict] | None:
    """Lee el Sheet (CSV) y devuelve las filas cuyo teléfono coincide con `phone`.

    Devuelve None si el Sheet no está configurado. Lanza excepción si falla la red.
    """
    url = os.environ.get("MBE_SHEET_CSV_URL", "")
    if not url:
        return None

    r = requests.get(url, timeout=10)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))

    # Comparamos por los últimos 8 dígitos (largo del número local en Panamá),
    # así no importa si el Sheet guarda "6123-4567" y WhatsApp manda "50761234567".
    tail = _only_digits(phone)[-8:]

    rows: list[dict] = []
    for row in reader:
        for key, value in row.items():
            digits = _only_digits(value)
            if digits and digits[-8:] == tail:
                rows.append(row)
                break
    return rows


def _format_package_block(row: dict, index: int | None = None) -> tuple[str, float | None]:
    """Arma el bloque de texto de un paquete. Devuelve (texto, total_a_pagar_o_None).

    Los montos se calculan aquí (deterministas): impuesto = neto * 7%,
    total a pagar = neto + impuesto.
    """
    tracking = _find(row, TRACKING_KEYS) or "sin tracking"
    estado   = _find(row, ESTADO_KEYS) or "sin estado"
    volumen  = _find(row, VOLUMEN_KEYS)
    peso     = _find(row, PESO_KEYS)
    neto     = _parse_money(_find(row, COSTO_KEYS))

    lines = [f"Paquete {index}"] if index is not None else []
    lines.append(f"- Tracking: {tracking}")
    lines.append(f"- Estado: {estado}")
    if volumen:
        lines.append(f"- Volumen: {volumen}")
    if peso:
        lines.append(f"- Peso: {peso}")

    total = None
    if neto is not None:
        impuesto = round(neto * IMPUESTO_RATE, 2)
        total = round(neto + impuesto, 2)
        lines.append(f"- Costo total neto: ${neto:,.2f}")
        lines.append(f"- Impuesto (7%): ${impuesto:,.2f}")
        lines.append(f"- Costo total a pagar: ${total:,.2f}")
    return "\n".join(lines), total


def _packages_reply(phone: str) -> str:
    """Respaldo: busca todos los paquetes por número de teléfono (comportamiento
    anterior). Se usa solo cuando el cliente dice que no tiene el tracking a mano.
    """
    try:
        rows = _fetch_packages(phone)
    except Exception as e:
        print(f"[MBE] error leyendo Sheet: {e}")
        return "Disculpa, ahorita no pude revisar el sistema. Dame unos minutos y lo vemos de nuevo."

    if rows is None:
        return "Disculpa, ahorita no puedo revisar el estado de los paquetes. Llamanos al (507) 271-5975 y te ayudamos."
    if not rows:
        return ("Reviso y no veo paquetes a nombre de este numero. "
                "Si crees que deberia haber algo, llamanos al (507) 271-5975 y lo verificamos.")

    blocks: list[str] = []
    grand_total = 0.0
    hay_costos = False
    for i, row in enumerate(rows, 1):
        block, total = _format_package_block(row, index=i)
        blocks.append(block)
        if total is not None:
            grand_total += total
            hay_costos = True

    msg = "Esto es lo que veo a tu nombre:\n\n" + "\n\n".join(blocks)
    if hay_costos and len(rows) > 1:
        msg += f"\n\nEl total a cancelar por todos sus paquetes es ${grand_total:,.2f}"
    return msg


# Heurística genérica para reconocer un tracking dentro de texto libre (letras,
# números y guiones, de al menos 5 caracteres). Sirve para no exigir que el
# mensaje completo sea EXACTAMENTE el tracking: lo encuentra aunque venga
# mezclado con otras palabras, o con otro mensaje pegado por el agrupado de
# mensajes en cadena (ver TODO.md: ajustar esto al formato real de tracking
# del negocio en cuanto lo tengamos, ej. largo fijo o prefijo conocido).
_TRACKING_CANDIDATE_RE = re.compile(r"[A-Za-z0-9-]{5,}")


def _extract_tracking_candidates(text: str) -> list[str]:
    return _TRACKING_CANDIDATE_RE.findall(text or "")


def _fetch_by_tracking_candidates(candidates: list[str]) -> list[dict] | None:
    """Descarga el Sheet (CSV) una sola vez y prueba cada candidato de
    tracking en orden contra sus filas. Devuelve las filas del primer
    candidato que coincida (exacto o parcial), o [] si ninguno coincidió.
    Devuelve None si el Sheet no está configurado. Lanza excepción si falla
    la red.
    """
    url = os.environ.get("MBE_SHEET_CSV_URL", "")
    if not url:
        return None

    r = requests.get(url, timeout=10)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))

    def _norm_tracking(row: dict) -> str | None:
        value = _find(row, TRACKING_KEYS)
        return "".join(value.split()).lower() if value else None

    for candidate in candidates:
        query = "".join((candidate or "").split()).lower()
        if not query:
            continue
        exact = [row for row in rows if _norm_tracking(row) == query]
        if exact:
            return exact
        partial = [row for row in rows if (_norm_tracking(row) or "") and query in _norm_tracking(row)]
        if partial:
            return partial
    return []


def _looks_like_no_tracking(text: str) -> bool:
    t = (text or "").lower()
    return any(phrase in t for phrase in NO_TRACKING_PHRASES)


def _tracking_followup_reply(phone: str, text: str) -> str:
    """Responde al mensaje del cliente después de que se le pidió el tracking.

    Primero intenta encontrar un tracking real dentro del texto (aunque venga
    mezclado con otras palabras o mensajes agrupados); solo si ninguno
    coincide con el Sheet interpreta que el cliente no lo tiene a la mano, o
    que no se encontró.
    """
    candidates = _extract_tracking_candidates(text) or [text]

    try:
        rows = _fetch_by_tracking_candidates(candidates)
    except Exception as e:
        print(f"[MBE] error leyendo Sheet (tracking): {e}")
        return "Disculpa, ahorita no pude revisar el sistema. Dame unos minutos y lo vemos de nuevo."

    if rows is None:
        return "Disculpa, ahorita no puedo revisar el estado de los paquetes. Llamanos al (507) 271-5975 y te ayudamos."

    if rows:
        block, _ = _format_package_block(rows[0])
        return "Esto es lo que veo con ese tracking:\n\n" + block

    if _looks_like_no_tracking(text):
        return _packages_reply(phone)

    # No se encontró ningún candidato: en vez de pedirle que lo confirme (lo
    # que rompía el estado "esperando tracking" si volvía a fallar), se pasa
    # directo con una persona.
    ai.HANDOFF_REQUESTS.add(phone)
    return ("No encuentro ese tracking en el sistema. Te voy a pasar con alguien del equipo "
            "para que te ayude con esto.")


def _bot_last_asked_tracking(history: list) -> bool:
    """True si el último mensaje del bot en el historial fue pedir el tracking,
    es decir, si el cliente está respondiendo a esa pregunta."""
    if not history:
        return False
    last = history[-1]
    return last.get("role") == "model" and ASK_TRACKING_MSG in (last.get("text") or "")


def handle(phone: str, text: str, history: list | None = None) -> str | None:
    """Punto de entrada del cliente MBE.

    `history`: si viene (modo Chatwoot, reconstruido desde la conversación real,
    que ya es persistente), se usa para decidir "¿ya lo saludé?" y como memoria
    del chat, en vez de los sets/diccionarios en RAM (`GREETED`, `ai.SESSIONS`).
    También se le pasa al router (`_classify`) para que no reclasifique a mitad
    de un flujo ya en curso (ej. una cotización) solo porque el mensaje nuevo
    menciona algo ambiguo relacionado con paquetes.

    El estado "le acabo de pedir el tracking, esta es su respuesta" se deriva
    del propio `history` (`_bot_last_asked_tracking`) en vez de guardarse aparte:
    así sobrevive a que Cloud Run reinicie o escale a otra instancia. Cuando no
    hay `history` (modo directo, sin Chatwoot) se usa `WAITING_TRACKING` en RAM
    como respaldo, igual que `GREETED`.
    """
    waiting_tracking = (
        _bot_last_asked_tracking(history) if history is not None else phone in WAITING_TRACKING
    )
    if waiting_tracking:
        if history is None:
            WAITING_TRACKING.discard(phone)
        reply = _tracking_followup_reply(phone, text)
        print(f"[MBE] tracking -> {phone}: {reply[:80]}")
        return reply

    intent = _classify(text, history)
    if history is not None:
        first_time = len(history) == 0
    else:
        first_time = phone not in GREETED
        if first_time:
            GREETED.add(phone)

    if intent == "PACKAGES":
        if history is None:
            WAITING_TRACKING.add(phone)
        print(f"[MBE] paquetes -> {phone}: pidiendo tracking")
        return ASK_TRACKING_MSG

    # Primer contacto que es solo un saludo (sin pregunta ni pedido real):
    # saludo natural. Si el primer mensaje ya trae una intención real (ej.
    # "quiero cotizar un envío"), se atiende directo, sin descartarlo.
    if first_time and intent == "GREETING":
        return WELCOME

    reply = ai.chat_reply(f"mbe:{phone}", GENERAL_PROMPT, text, history=history)
    if reply is None:
        return "Disculpa, ahorita tengo un inconveniente tecnico. En un momento te atiendo."

    if "[[HANDOFF]]" in reply:
        reply = reply.replace("[[HANDOFF]]", "").strip()
        ai.HANDOFF_REQUESTS.add(phone)
        print(f"[MBE] handoff -> {phone} (fuera de tema o conversacion compleja)")

    print(f"[MBE] general -> {phone}: {reply[:80]}")
    return reply
