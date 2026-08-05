"""
Bot de WhatsApp — Mail Boxes Etc (MBE)
========================================
Webhook de WhatsApp Cloud API (Meta) directo (sin Chatwoot). El "cerebro"
es clients/mbe.py: consulta de paquetes contra un Google Sheet publicado.

Variables de entorno (.env):
    WHATSAPP_TOKEN             token de Meta
    WHATSAPP_PHONE_NUMBER_ID   id del número
    WHATSAPP_VERIFY_TOKEN      string que tú inventas (ej. mbe-verify-2026)
    WHATSAPP_API_VERSION       v22.0
    GEMINI_API_KEY             api key de Gemini
    GEMINI_MODEL               gemini-2.5-flash
    MBE_SHEET_CSV_URL          Google Sheet publicado como CSV (paquetes)
    CHATWOOT                   true|false (default false) — deja en false
"""

import hashlib
import hmac
import os
import random
import threading
import time
from collections import deque

import requests
from dotenv import load_dotenv
from flask import Flask, request, make_response

# Carga el .env ANTES de importar los clientes (ellos leen variables al importarse).
load_dotenv()

import ai
from clients import get_handler

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
WA_TOKEN        = os.environ.get("WHATSAPP_TOKEN", "")
WA_PHONE_ID     = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WA_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "bea-verify-2026")
WA_API_VERSION  = os.environ.get("WHATSAPP_API_VERSION", "v22.0")

# Para que la respuesta se sienta humana: muestra "escribiendo..." y demora un poco
# (encima del debounce de abajo, así que se mantiene corto para no sumar demasiado).
TYPING_MIN_SECONDS = float(os.environ.get("TYPING_MIN_SECONDS", "2"))
TYPING_MAX_SECONDS = float(os.environ.get("TYPING_MAX_SECONDS", "8"))

# Si el cliente manda varios mensajes seguidos ("mensajes en cadena"), esperamos
# este tiempo desde el último mensaje antes de procesarlos, para juntarlos y
# responder una sola vez en vez de contestar cada uno por separado (lo que
# además puede generar dos respuestas corriendo en paralelo sobre el mismo
# historial y perder contexto entre sí).
DEBOUNCE_SECONDS = float(os.environ.get("DEBOUNCE_SECONDS", "15"))

# Cerebro activo (Bea para QUAI, asistente de paquetes para MBE, ...).
handle_message = get_handler()
print("[BOOT] cliente=MBE")


def _make_seen_tracker(maxlen: int = 2000):
    """Crea un detector de IDs ya vistos (acotado, sin crecer para siempre).

    Meta y Chatwoot pueden reintentar la entrega del mismo evento si no
    respondemos rápido (o por cualquier reintento de red); sin esto,
    procesaríamos el mismo mensaje dos veces y contestaríamos duplicado.
    Devuelve una función `mark_if_new(key) -> bool`: True la primera vez que
    ve ese id, False si ya lo había visto antes (y no hace nada más).
    """
    seen: set = set()
    order: deque = deque()

    def mark_if_new(key) -> bool:
        if not key:
            return True   # sin id no podemos deduplicar; seguimos como antes
        if key in seen:
            return False
        seen.add(key)
        order.append(key)
        if len(order) > maxlen:
            seen.discard(order.popleft())
        return True

    return mark_if_new


# Un tracker separado por canal: los IDs de WhatsApp y los de Chatwoot viven
# en espacios de nombres distintos, no tiene sentido compartir el set.
_wa_seen = _make_seen_tracker()
_cw_seen = _make_seen_tracker()


# ── ENVIAR POR WHATSAPP ─────────────────────────────────────────────────────────
def send_whatsapp(phone: str, text: str) -> None:
    """Envía un mensaje de texto via WhatsApp Cloud API."""
    if not WA_PHONE_ID or not WA_TOKEN:
        print("[WHATSAPP] ERROR: falta WHATSAPP_PHONE_NUMBER_ID o WHATSAPP_TOKEN en el .env")
        return
    try:
        r = requests.post(
            f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_ID}/messages",
            json={
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "text",
                "text": {"body": text},
            },
            headers={
                "Authorization": f"Bearer {WA_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        print(f"[WHATSAPP] -> {phone} status={r.status_code} {r.text[:150]}")
    except Exception as e:
        print(f"[WHATSAPP] ERROR: {e}")


def send_typing(message_id: str) -> None:
    """Muestra el indicador 'escribiendo...' (marca el mensaje como leído).

    Dura hasta ~25s o hasta que enviamos la respuesta. Si falla, no pasa nada:
    el mensaje igual se responde.
    """
    if not WA_PHONE_ID or not WA_TOKEN or not message_id:
        return
    try:
        requests.post(
            f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_ID}/messages",
            json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            },
            headers={
                "Authorization": f"Bearer {WA_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[WHATSAPP] typing ERROR: {e}")


def process_and_reply(phone: str, text: str, message_id: str) -> None:
    """Procesa el mensaje en segundo plano: muestra 'escribiendo...', espera un
    rato para sentirse humano, y luego responde. Corre en un hilo aparte para
    no demorar el 200 que Meta espera del webhook.
    """
    start = time.time()
    send_typing(message_id)

    reply = handle_message(phone, text)
    # Si el cerebro decidió pasar con una persona, lo recogemos para activar el modo humano.
    handoff = phone in ai.HANDOFF_REQUESTS
    ai.HANDOFF_REQUESTS.discard(phone)

    if reply:
        # Demora total objetivo entre 5 y 20s (descontando lo que ya tardó el modelo).
        target = random.uniform(TYPING_MIN_SECONDS, TYPING_MAX_SECONDS)
        remaining = target - (time.time() - start)
        if remaining > 0:
            time.sleep(remaining)
        # El cerebro puede devolver un texto o varios (lista de burbujas).
        for part in ([reply] if isinstance(reply, str) else reply):
            print(f"[AGENTE] {phone}: {part}")
            send_whatsapp(phone, part)

    if handoff:
        ai.HUMAN_MODE.add(phone)
        print(f"[HITL] {phone} pasado a modo humano (decidido por el cerebro)")


# Buffer de mensajes en cadena por teléfono (modo WhatsApp directo, sin Chatwoot).
_pending_lock = threading.Lock()
_pending_wa: dict[str, list[str]] = {}
_pending_wa_timers: dict[str, threading.Timer] = {}


def _flush_whatsapp(phone: str, message_id: str) -> None:
    """Se dispara cuando pasan DEBOUNCE_SECONDS sin mensajes nuevos de este
    teléfono: junta todo lo acumulado y lo procesa como un solo turno."""
    with _pending_lock:
        texts = _pending_wa.pop(phone, [])
        _pending_wa_timers.pop(phone, None)
    if not texts:
        return
    process_and_reply(phone, "\n".join(texts), message_id)


def _queue_whatsapp(phone: str, text: str, message_id: str) -> None:
    """Acumula el mensaje y reinicia el temporizador de espera. Si llegan más
    mensajes antes de que se cumpla DEBOUNCE_SECONDS, se van sumando y el
    temporizador se reinicia, así que todos se procesan juntos al final."""
    with _pending_lock:
        _pending_wa.setdefault(phone, []).append(text)
        old_timer = _pending_wa_timers.get(phone)
        if old_timer:
            old_timer.cancel()
        timer = threading.Timer(DEBOUNCE_SECONDS, _flush_whatsapp, args=(phone, message_id))
        timer.daemon = True
        _pending_wa_timers[phone] = timer
        timer.start()


# ── WEBHOOK ──────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """Meta llama este GET una sola vez al registrar la URL."""
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == WA_VERIFY_TOKEN):
        print("[WEBHOOK] verificado")
        return make_response(request.args.get("hub.challenge"), 200)
    return make_response("forbidden", 403)


@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe los mensajes entrantes de WhatsApp."""
    data = request.get_json(silent=True) or {}

    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return make_response("ok", 200)   # eventos de estado (entregado/leido)

        # Meta puede entregar más de un mensaje en el mismo payload (bajo carga);
        # los procesamos todos, no solo el primero, para no perder ninguno.
        for msg in messages:
            phone = msg["from"]                   # numero internacional sin "+", ej. 5076...

            # Reintento de Meta del mismo mensaje: lo ignoramos por completo,
            # ni siquiera lo mostramos como si fuera nuevo.
            if not _wa_seen(msg.get("id", "")):
                print(f"[WEBHOOK] mensaje duplicado ignorado ({phone})")
                continue

            if msg.get("type") != "text":
                send_whatsapp(phone, "Por ahora solo puedo leer mensajes de texto.")
                continue

            text = msg["text"]["body"].strip()
            print(f"[USUARIO] {phone}: {text}")

            # Si un humano tomó el control, el bot calla.
            if phone in ai.HUMAN_MODE:
                print(f"[HITL] {phone} en modo humano — el bot calla")
                continue

            # Mostramos "escribiendo..." de inmediato, pero el procesamiento real se
            # agrupa con cualquier mensaje en cadena que llegue en los próximos
            # DEBOUNCE_SECONDS (ver _queue_whatsapp) para responder una sola vez.
            send_typing(msg.get("id", ""))
            _queue_whatsapp(phone, text, msg.get("id", ""))

    except Exception as e:
        print(f"[WEBHOOK] ERROR: {e}")

    # Siempre 200 rápido: si tardas, Meta reintenta y puede deshabilitar el webhook.
    return make_response("ok", 200)


# ── CHATWOOT (human-in-the-loop, opcional) ────────────────────────────────────
# Chatwoot es un conector de canal alterno: solo aplica a los clientes cuyo número
# está conectado a una bandeja de Chatwoot en vez de recibir los mensajes directo
# en /webhook. CHATWOOT=false (default) apaga por completo esta ruta y el bot
# atiende únicamente por WhatsApp Cloud API directo (/webhook).
CHATWOOT_ENABLED = os.environ.get("CHATWOOT", "false").strip().lower() in ("1", "true", "yes")

if CHATWOOT_ENABLED:
    # Cuando el número está conectado a Chatwoot, Meta entrega los mensajes a Chatwoot
    # (no a /webhook). Chatwoot reenvía cada mensaje entrante a este endpoint como un
    # "Agent Bot". El bot responde por la API de Chatwoot, y al disparar el handoff la
    # conversación pasa a "abierta" para que un humano la atienda en la bandeja.
    CW_BASE      = os.environ.get("CHATWOOT_BASE_URL", "https://app.chatwoot.com").rstrip("/")
    CW_ACCOUNT   = os.environ.get("CHATWOOT_ACCOUNT_ID", "")
    CW_INBOX     = os.environ.get("CHATWOOT_INBOX_ID", "")
    CW_TOKEN     = os.environ.get("CHATWOOT_API_TOKEN", "")
    CW_RESUME_H  = float(os.environ.get("CHATWOOT_HANDOFF_RESUME_HOURS", "12"))
    # Secret del Agent Bot (distinto del access token): sirve para verificar
    # que los webhooks que llegan a /chatwoot realmente los mandó Chatwoot
    # (viene firmado con HMAC-SHA256 en el header X-Chatwoot-Signature). Si
    # no se configura, no se puede verificar y se deja pasar todo (igual que
    # antes de este cambio) — mejor eso que romper el bot por no tenerlo.
    CW_WEBHOOK_SECRET = os.environ.get("CHATWOOT_WEBHOOK_SECRET", "")

    def _cw_verify_signature(raw_body: bytes, signature_header: str) -> bool:
        if not CW_WEBHOOK_SECRET:
            return True
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(CW_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        provided = signature_header.split("=", 1)[1]
        return hmac.compare_digest(expected, provided)

    # Frases (normalizadas) con las que el cliente pide hablar con una persona.
    HANDOFF_KEYWORDS = (
        "hablar con", "con una persona", "con alguien", "una persona", "un humano",
        "una humana", "persona real", "asesor", "un agente", "ejecutivo", "operador",
        "representante", "con el equipo", "atienda alguien", "atienda una persona",
    )
    HANDOFF_REPLY = "Permíteme, te paso con una persona del equipo para atenderte mejor."

    # Estado por conversación de Chatwoot (en RAM, se borra al reiniciar).
    _CW_LAST: dict[int, str] = {}        # último mensaje normalizado (detecta repetición)
    _CW_HANDOFF: dict[int, float] = {}   # timestamp del handoff (para retomar tras N horas)

    def _cw_headers() -> dict:
        return {"api_access_token": CW_TOKEN, "Content-Type": "application/json"}

    def cw_send(conversation_id: int, text: str) -> None:
        """Envía un mensaje saliente a la conversación (Chatwoot lo entrega por WhatsApp)."""
        if not (CW_TOKEN and CW_ACCOUNT):
            print("[CHATWOOT] falta CHATWOOT_API_TOKEN o CHATWOOT_ACCOUNT_ID")
            return
        try:
            r = requests.post(
                f"{CW_BASE}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conversation_id}/messages",
                json={"content": text, "message_type": "outgoing"},
                headers=_cw_headers(), timeout=10,
            )
            print(f"[CHATWOOT] -> conv {conversation_id} status={r.status_code}")
        except Exception as e:
            print(f"[CHATWOOT] send ERROR: {e}")

    def cw_handoff(conversation_id: int) -> None:
        """Pasa la conversación a un humano: la cambia a 'abierta' en la
        bandeja (la etiqueta 'agente' la pone _cw_go_silent, que siempre se
        llama junto con esta función en los handoffs que decidimos nosotros)."""
        try:
            requests.post(
                f"{CW_BASE}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conversation_id}/toggle_status",
                json={"status": "open"}, headers=_cw_headers(), timeout=10,
            )
            print(f"[CHATWOOT] handoff conv {conversation_id} -> humano")
        except Exception as e:
            print(f"[CHATWOOT] handoff ERROR: {e}")

    def _cw_conversation_labels(conversation_id: int) -> list:
        """Trae las etiquetas actuales de la conversación (vacío si falla)."""
        try:
            r = requests.get(
                f"{CW_BASE}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conversation_id}/labels",
                headers=_cw_headers(), timeout=10,
            )
            r.raise_for_status()
            return r.json().get("payload", [])
        except Exception as e:
            print(f"[CHATWOOT] labels GET ERROR: {e}")
            return []

    def _cw_set_labels(conversation_id: int, labels: list) -> None:
        """Reemplaza las etiquetas de la conversación (la API de Chatwoot
        sobrescribe la lista completa, no la agrega — por eso siempre
        mandamos el set completo, no solo la etiqueta nueva)."""
        try:
            requests.post(
                f"{CW_BASE}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conversation_id}/labels",
                json={"labels": labels}, headers=_cw_headers(), timeout=10,
            )
            print(f"[CHATWOOT] conv {conversation_id}: etiquetas -> {labels}")
        except Exception as e:
            print(f"[CHATWOOT] labels POST ERROR: {e}")

    def _cw_mark_agente(conversation_id: int) -> None:
        """Cambia la etiqueta de la conversación a 'agente' (quita 'bot' si
        estaba), para que el chequeo de silencio (ver _cw_should_stay_silent)
        y la automatización de Chatwoot lean la misma señal."""
        labels = set(_cw_conversation_labels(conversation_id))
        labels.discard("bot")
        labels.add("agente")
        _cw_set_labels(conversation_id, sorted(labels))

    def _cw_labeled_agente(conversation_id: int) -> bool:
        """True si la conversación tiene la etiqueta 'agente' puesta (manual
        o por automatización de Chatwoot) — no respondemos en ese caso.
        Si no tiene esa etiqueta (ej. tiene 'bot', o ninguna todavía por una
        automatización lenta), respondemos normal: es más seguro fallar
        hacia "sí responder" que quedarse mudo por un error de lectura."""
        labels = _cw_conversation_labels(conversation_id)
        print(f"[CHATWOOT] conv {conversation_id}: etiquetas actuales = {labels or '(ninguna)'}")
        return "agente" in [l.lower() for l in labels]

    def _cw_conversation_messages(conversation_id: int) -> list:
        """Descarga y ordena cronológicamente los mensajes de la conversación.

        Probamos usar el endpoint "show" de la conversación (accesible para
        Agent Bots) para no depender del endpoint de mensajes ("index",
        bloqueado para bots) — pero en la práctica "show" solo trae 1
        mensaje (no documentado, comprobado en logs reales el 2026-08-05),
        insuficiente para la memoria de la conversación. Volvemos al
        endpoint de mensajes de siempre; solo funciona con un token de
        agente normal, no con un Agent Bot real (ver TODO.md).

        Un solo GET, que se reutiliza tanto para armar el historial que ve la
        IA como para detectar si un humano ya tomó el control (ver
        `_cw_human_took_over`) — así ninguna de las dos cosas cuesta un
        llamado extra a Chatwoot.
        """
        try:
            r = requests.get(
                f"{CW_BASE}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conversation_id}/messages",
                headers=_cw_headers(), timeout=10,
            )
            r.raise_for_status()
            messages = r.json().get("payload", [])
        except Exception as e:
            print(f"[CHATWOOT] history ERROR: {e}")
            return []

        # La API de Chatwoot no garantiza orden ascendente (normalmente devuelve
        # los mensajes más recientes primero, pensado para paginar hacia atrás).
        # Ordenamos explícitamente por fecha para no armar el historial al revés.
        return sorted(messages, key=lambda m: m.get("created_at") or 0)

    def cw_fetch_history(messages: list, exclude_ids=None, limit: int = 15) -> list:
        """Arma el historial para el cerebro a partir de los mensajes ya
        descargados, excluyendo los que disparon este turno (pueden ser
        varios si el cliente mandó mensajes en cadena y se agruparon, ver
        _queue_chatwoot).
        """
        exclude_ids = set(exclude_ids or ())
        history = []
        for m in messages:
            if m.get("id") in exclude_ids:
                continue
            content = (m.get("content") or "").strip()
            # message_type: 0 = incoming (cliente), 1 = outgoing (bot/agente).
            if not content or m.get("message_type") not in (0, 1):
                continue
            role = "user" if m.get("message_type") == 0 else "model"
            history.append({"role": role, "text": content})
        return history[-limit:]

    def _cw_human_took_over(conversation_id: int, messages: list, exclude_ids=None) -> bool:
        """True si el último mensaje saliente de la conversación (sin contar
        los del turno actual) lo mandó un agente humano y no pasó
        CW_RESUME_H desde entonces.

        No depende de RAM: se deriva del historial real de Chatwoot (mismos
        datos que ya trajo `_cw_conversation_messages`), así que sigue
        funcionando aunque `_CW_HANDOFF` se haya perdido por un reinicio en
        frío de la instancia (ver hallazgo del log del 2026-08-04).
        """
        exclude_ids = set(exclude_ids or ())
        for m in reversed(messages):
            if m.get("id") in exclude_ids or m.get("message_type") != 1:
                continue
            if not _cw_is_other_human(m):
                return False   # el último saliente fuimos nosotros: sigue libre
            created_at = m.get("created_at")
            if created_at and (time.time() - float(created_at)) >= CW_RESUME_H * 3600:
                return False   # pasó el tiempo de "retomar"
            sender_id = (m.get("sender") or {}).get("id")
            print(f"[CHATWOOT] conv {conversation_id}: ultimo saliente de otro agente (id={sender_id}) "
                  f"(detectado via historial, no via RAM)")
            return True
        return False

    def cw_process(conversation_id: int, phone: str, text: str, message_id: str = "", exclude_ids=None) -> None:
        """Corre el cerebro y responde por Chatwoot, en segundo plano (igual que WhatsApp)."""
        start = time.time()
        # Muestra "escribiendo..." en WhatsApp usando el id (wamid) del mensaje entrante.
        send_typing(message_id)
        messages = _cw_conversation_messages(conversation_id)

        if _cw_labeled_agente(conversation_id):
            # Señal explícita (manual o por automatización de Chatwoot): esta
            # conversación está marcada para que la atienda una persona.
            _cw_go_silent(conversation_id)
            print(f"[CHATWOOT] conv {conversation_id}: etiqueta 'agente' activa, bot en silencio")
            return

        if _cw_human_took_over(conversation_id, messages, exclude_ids):
            # La memoria en RAM no tenía este handoff (probable reinicio en
            # frío de la instancia), pero el historial real de Chatwoot
            # muestra que un humano ya contestó: nos callamos y de paso nos
            # "autorreparamos" en RAM para que los próximos mensajes de esta
            # conversación se resuelvan directo, sin este chequeo extra.
            _cw_go_silent(conversation_id)
            print(f"[CHATWOOT] conv {conversation_id}: handoff detectado via historial, bot en silencio")
            return

        history = cw_fetch_history(messages, exclude_ids)
        reply = handle_message(phone, text, history=history)
        # Si el cerebro pidió pasar con una persona, lo recogemos para el handoff en Chatwoot.
        handoff = phone in ai.HANDOFF_REQUESTS
        ai.HANDOFF_REQUESTS.discard(phone)

        if reply:
            target = random.uniform(TYPING_MIN_SECONDS, TYPING_MAX_SECONDS)
            remaining = target - (time.time() - start)
            if remaining > 0:
                time.sleep(remaining)
            for part in ([reply] if isinstance(reply, str) else reply):
                cw_send(conversation_id, part)

        if handoff:
            _cw_go_silent(conversation_id)
            cw_handoff(conversation_id)
            print(f"[CHATWOOT] handoff conv {conversation_id} (decidido por el cerebro)")

    def _cw_phone(data: dict) -> str:
        """Extrae el teléfono del contacto del payload de Chatwoot (sin '+')."""
        sender = data.get("sender") or {}
        phone = sender.get("phone_number")
        if not phone:
            meta = ((data.get("conversation") or {}).get("meta") or {})
            phone = (meta.get("sender") or {}).get("phone_number", "")
        return (phone or "").lstrip("+")

    # Buffer de mensajes en cadena por conversación (mismo patrón que _pending_wa
    # para WhatsApp directo, ver arriba).
    _pending_cw_lock = threading.Lock()
    _pending_cw_texts: dict[int, list[str]] = {}
    _pending_cw_ids: dict[int, list] = {}
    _pending_cw_timers: dict[int, threading.Timer] = {}

    def _cw_go_silent(conversation_id: int) -> None:
        """Marca la conversación como 'en modo humano', pone la etiqueta
        'agente' (quita 'bot'), y cancela cualquier mensaje en cadena que
        estuviera esperando el debounce, para que el bot no conteste después
        de que ya se pasó (o alguien más ya tomó) el control. Se llama tanto
        cuando el handoff lo decidimos nosotros (palabra clave, bucle,
        cotización completa) como cuando un agente humano contesta
        manualmente desde Chatwoot o se detecta vía historial — un solo
        lugar para que la etiqueta siempre quede sincronizada, sin que haya
        que cambiarla a mano."""
        _CW_HANDOFF[conversation_id] = time.time()
        _cw_mark_agente(conversation_id)
        with _pending_cw_lock:
            _pending_cw_texts.pop(conversation_id, None)
            _pending_cw_ids.pop(conversation_id, None)
            timer = _pending_cw_timers.pop(conversation_id, None)
        if timer:
            timer.cancel()

    def _cw_sender_type(data: dict) -> str:
        """Extrae quién mandó un mensaje/evento de Chatwoot ("user" = agente
        humano, "agent_bot" = nuestro bot, "contact" = el cliente, etc.).
        Se expone en los logs como campo visual para poder seguir el flujo
        de la demo en Cloud Logging sin adivinar."""
        sender_type = data.get("sender_type") or (data.get("sender") or {}).get("type") or ""
        if sender_type:
            return sender_type
        # El payload de Chatwoot no siempre trae sender_type en mensajes
        # entrantes, pero un entrante siempre es del cliente por definición.
        if data.get("message_type") in ("incoming", 0):
            return "contact"
        return "desconocido"

    _cw_self_agent_id_cache = [None]  # lista para poder mutar desde la closure

    def _cw_own_agent_id():
        """ID del agente de Chatwoot detrás de nuestro CHATWOOT_API_TOKEN
        (usamos un token de agente dedicado normal, no un Agent Bot — un
        Agent Bot no puede leer el historial de mensajes, ver TODO.md).

        Se consulta una sola vez por instancia (GET /api/v1/profile) y se
        cachea en memoria: como el token es de un agente normal, Chatwoot
        marca TODOS los mensajes salientes con sender_type='user', tanto los
        nuestros como los de cualquier otro agente humano — hay que comparar
        el id para saber cuál es cuál.
        """
        if _cw_self_agent_id_cache[0] is not None:
            return _cw_self_agent_id_cache[0]
        try:
            r = requests.get(f"{CW_BASE}/api/v1/profile", headers=_cw_headers(), timeout=10)
            r.raise_for_status()
            agent_id = r.json().get("id")
            _cw_self_agent_id_cache[0] = agent_id
            print(f"[CHATWOOT] identidad propia detectada: agent id={agent_id}")
            return agent_id
        except Exception as e:
            print(f"[CHATWOOT] profile ERROR: {e}")
            return None

    def _cw_is_other_human(data: dict) -> bool:
        """True si el remitente de este mensaje/evento es un agente humano
        DISTINTO de la cuenta que usa nuestro token (no nosotros mismos)."""
        if _cw_sender_type(data).lower() != "user":
            return False
        own_id = _cw_own_agent_id()
        if own_id is None:
            return True   # no pudimos confirmar quiénes somos: más seguro asumir que es otro humano
        sender_id = (data.get("sender") or {}).get("id")
        return sender_id != own_id

    def _flush_chatwoot(conversation_id: int, phone: str, message_id: str) -> None:
        """Se dispara cuando pasan DEBOUNCE_SECONDS sin mensajes nuevos en esta
        conversación: junta todo lo acumulado y lo procesa como un solo turno."""
        with _pending_cw_lock:
            texts = _pending_cw_texts.pop(conversation_id, [])
            ids = _pending_cw_ids.pop(conversation_id, [])
            _pending_cw_timers.pop(conversation_id, None)
        if not texts:
            return
        cw_process(conversation_id, phone, "\n".join(texts), message_id, exclude_ids=ids)

    def _queue_chatwoot(conversation_id: int, phone: str, text: str, message_id: str, cw_message_id) -> None:
        """Acumula el mensaje y reinicia el temporizador de espera (ver _queue_whatsapp)."""
        with _pending_cw_lock:
            _pending_cw_texts.setdefault(conversation_id, []).append(text)
            _pending_cw_ids.setdefault(conversation_id, []).append(cw_message_id)
            old_timer = _pending_cw_timers.get(conversation_id)
            if old_timer:
                old_timer.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, _flush_chatwoot, args=(conversation_id, phone, message_id))
            timer.daemon = True
            _pending_cw_timers[conversation_id] = timer
            timer.start()

    @app.route("/chatwoot", methods=["POST"])
    def chatwoot_webhook():
        """Recibe los eventos del Agent Bot de Chatwoot."""
        if not _cw_verify_signature(request.get_data(), request.headers.get("X-Chatwoot-Signature", "")):
            print("[CHATWOOT] firma invalida (X-Chatwoot-Signature no coincide) — request rechazado")
            return make_response("forbidden", 403)

        data = request.get_json(silent=True) or {}
        event = data.get("event")

        conv = data.get("conversation") or {}
        conversation_id = conv.get("id") or data.get("conversation_id")

        # Si el humano resuelve la conversación, el bot puede retomarla luego.
        if event in ("conversation_resolved", "conversation_status_changed"):
            status = conv.get("status") or data.get("status")
            if conversation_id and status in ("resolved", "pending"):
                _CW_HANDOFF.pop(conversation_id, None)
            return make_response("ok", 200)

        # Un agente humano contestó manualmente desde Chatwoot (no nosotros):
        # tratamos eso como handoff inmediato, sin esperar a que nosotros
        # mismos lo disparemos por algún otro camino.
        if event == "message_created" and data.get("message_type") == "outgoing":
            sender_id = (data.get("sender") or {}).get("id")
            if conversation_id and _cw_is_other_human(data):
                _cw_go_silent(conversation_id)
                print(f"[CHATWOOT] conv {conversation_id}: mensaje saliente de otro agente (id={sender_id}) "
                      f"-> bot en silencio")
            else:
                print(f"[CHATWOOT] conv {conversation_id}: mensaje saliente propio (id={sender_id}) "
                      f"(no dispara handoff)")
            return make_response("ok", 200)

        # Solo nos interesan mensajes nuevos ENTRANTES (del cliente), no los salientes/bot.
        if event != "message_created" or data.get("message_type") != "incoming":
            return make_response("ok", 200)

        text = (data.get("content") or "").strip()
        if not conversation_id or not text:
            return make_response("ok", 200)

        # Reintento del mismo evento de Chatwoot: se ignora antes de tocar
        # cualquier otro estado (handoff, detector de repetidos, etc.), para
        # que un reintento técnico nunca se confunda con que el cliente
        # repitió su mensaje por frustración.
        if not _cw_seen(data.get("id")):
            print(f"[CHATWOOT] evento duplicado ignorado (conv {conversation_id})")
            return make_response("ok", 200)

        # ¿Está en modo humano? Si no han pasado las horas de "retomar", el bot calla.
        handed = _CW_HANDOFF.get(conversation_id)
        if handed is not None:
            if (time.time() - handed) < CW_RESUME_H * 3600:
                print(f"[CHATWOOT] conv {conversation_id} en modo humano — el bot calla")
                return make_response("ok", 200)
            _CW_HANDOFF.pop(conversation_id, None)   # pasó el tiempo: el bot retoma

        norm = " ".join(text.lower().split())

        # Disparador 1: el cliente pide una persona.
        # Disparador 2: repite exactamente el mismo mensaje (señal de que no lo ayudamos).
        pide_humano = any(k in norm for k in HANDOFF_KEYWORDS)
        repitio = norm and _CW_LAST.get(conversation_id) == norm
        _CW_LAST[conversation_id] = norm

        if pide_humano or repitio:
            _cw_go_silent(conversation_id)
            razon = "lo pidió" if pide_humano else "repitió mensaje"
            print(f"[CHATWOOT] handoff conv {conversation_id} ({razon})")
            cw_send(conversation_id, HANDOFF_REPLY)
            cw_handoff(conversation_id)
            return make_response("ok", 200)

        phone = _cw_phone(data)
        # source_id = id del mensaje original de WhatsApp (wamid); lo usamos para el typing.
        message_id = data.get("source_id") or ""
        cw_message_id = data.get("id")
        print(f"[CHATWOOT] conv {conversation_id} ({phone}) sender_type={_cw_sender_type(data)} "
              f"wamid={message_id or '∅'}: {text}")
        # Mostramos "escribiendo..." de inmediato, pero el procesamiento real se
        # agrupa con cualquier mensaje en cadena que llegue en los próximos
        # DEBOUNCE_SECONDS (ver _queue_chatwoot) para responder una sola vez.
        send_typing(message_id)
        _queue_chatwoot(conversation_id, phone, text, message_id, cw_message_id)
        return make_response("ok", 200)
else:
    print("[BOOT] CHATWOOT=false — ruta /chatwoot deshabilitada, solo WhatsApp directo")


@app.route("/health", methods=["GET"])
def health():
    return make_response("ok", 200)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
