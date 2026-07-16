"""
clients/mbe.py — Asistente de Mail Boxes Etc (MBE)
==================================================
Flujo distinto al de QUAI:

    1. Saluda de forma natural y conversacional (sin menús ni opciones numeradas).
    2. Un "router" entiende, por lenguaje natural, si el cliente está preguntando
       por el estado de sus paquetes.
    3. Si es así -> un sub-agente liviano se conecta al Google Sheet y busca por el
       número de WhatsApp del que escribe. (No carga el contexto del negocio: solo paquetes.)
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

import requests

import ai

# Info del negocio (editable en negocio_mbe.txt, sin tocar código).
BUSINESS_INFO = ai.load_info("MBE_INFO_PATH", "negocio_mbe.txt")

# Teléfonos que ya recibieron el saludo inicial.
GREETED: set[str] = set()

# Saludo natural de primer contacto (cálido, sin menús ni opciones numeradas).
WELCOME = (
    "Hola, qué tal. Te habla Gia de Mail Boxes Etc Costa del Este. "
    "¿En qué te puedo ayudar hoy?"
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

FUERA DE TEMA:
- Solo atiendes temas de Mail Boxes Etc (envíos, paquetes, casillero, cotizaciones, horarios, etc.).
- Si el cliente pregunta algo que no tiene nada que ver con esto, coméntale con cortesía que
  aquí solo puedes ayudarle con temas de Mail Boxes Etc, e invítalo a volver a ese tema.
- Si, después de eso, el cliente insiste en lo mismo fuera de tema, o la conversación se pone
  complicada y no la puedes resolver con la información que tienes, dile con amabilidad y de forma
  explícita que lo vas a pasar con un supervisor para que lo atienda (usa la palabra "supervisor").
  En ese caso, al final de tu respuesta agrega en una línea aparte, exactamente así: [[HANDOFF]]
  (esa marca es una señal interna, el cliente nunca debe verla ni debes mencionarla).

INFORMACIÓN DEL NEGOCIO:
{BUSINESS_INFO}
"""

# Router de intención: detecta, por lenguaje natural, si pregunta por sus paquetes.
ROUTER_PROMPT = """Lees el mensaje de un cliente de una empresa de paqueteria y decides su intencion.
Responde UNA sola palabra, sin explicar:
- PACKAGES  si quiere saber por el estado, ubicacion, llegada o seguimiento de sus paquetes,
            pedidos o envios (ej: "llego mi paquete?", "donde esta mi envio", "tengo algo pendiente?").
- OTHER     para cualquier otra cosa (saludos, dudas de servicios, horarios, precios, etc).
"""


def _classify(text: str) -> str:
    answer = (ai.ask_once(ROUTER_PROMPT, text) or "").upper()
    return "PACKAGES" if "PACKAGES" in answer else "OTHER"


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


def _packages_reply(phone: str) -> str:
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

    # Armamos un bloque por paquete. Los montos se calculan aquí (deterministas):
    # impuesto = neto * 7%, total a pagar = neto + impuesto.
    blocks: list[str] = []
    grand_total = 0.0
    hay_costos = False

    for i, row in enumerate(rows, 1):
        tracking = _find(row, TRACKING_KEYS) or "sin tracking"
        estado   = _find(row, ESTADO_KEYS) or "sin estado"
        volumen  = _find(row, VOLUMEN_KEYS)
        peso     = _find(row, PESO_KEYS)
        neto     = _parse_money(_find(row, COSTO_KEYS))

        lines = [f"Paquete {i}"]
        lines.append(f"- Tracking: {tracking}")
        lines.append(f"- Estado: {estado}")
        if volumen:
            lines.append(f"- Volumen: {volumen}")
        if peso:
            lines.append(f"- Peso: {peso}")
        if neto is not None:
            impuesto = round(neto * IMPUESTO_RATE, 2)
            total = round(neto + impuesto, 2)
            grand_total += total
            hay_costos = True
            lines.append(f"- Costo total neto: ${neto:,.2f}")
            lines.append(f"- Impuesto (7%): ${impuesto:,.2f}")
            lines.append(f"- Costo total a pagar: ${total:,.2f}")
        blocks.append("\n".join(lines))

    msg = "Esto es lo que veo a tu nombre:\n\n" + "\n\n".join(blocks)
    if hay_costos and len(rows) > 1:
        msg += f"\n\nEl total a cancelar por todos sus paquetes es ${grand_total:,.2f}"
    return msg


def handle(phone: str, text: str, history: list | None = None) -> str | None:
    """Punto de entrada del cliente MBE.

    `history`: si viene (modo Chatwoot, reconstruido desde la conversación real,
    que ya es persistente), se usa para decidir "¿ya lo saludé?" y como memoria
    del chat, en vez de los sets/diccionarios en RAM (`GREETED`, `ai.SESSIONS`).
    """
    intent = _classify(text)
    if history is not None:
        first_time = len(history) == 0
    else:
        first_time = phone not in GREETED
        if first_time:
            GREETED.add(phone)

    if intent == "PACKAGES":
        reply = _packages_reply(phone)
        print(f"[MBE] paquetes -> {phone}: {reply[:80]}")
        return reply

    # Primer contacto sin intención de paquetes: saludo natural.
    if first_time:
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
