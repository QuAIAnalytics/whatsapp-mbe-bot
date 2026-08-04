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

import os
import random
import threading
import time
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
        """Pasa la conversación a un humano: la cambia a 'abierta' en la bandeja."""
        try:
            requests.post(
                f"{CW_BASE}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conversation_id}/toggle_status",
                json={"status": "open"}, headers=_cw_headers(), timeout=10,
            )
            print(f"[CHATWOOT] handoff conv {conversation_id} -> humano")
        except Exception as e:
            print(f"[CHATWOOT] handoff ERROR: {e}")

    def cw_fetch_history(conversation_id: int, exclude_ids=None, limit: int = 15) -> list:
        """Trae los últimos mensajes de la conversación (ya persistidos en Chatwoot)
        y los arma como historial para el cerebro, excluyendo los mensajes que
        disparon este turno (pueden ser varios si el cliente mandó mensajes en
        cadena y se agruparon, ver _queue_chatwoot).
        """
        exclude_ids = set(exclude_ids or ())
        try:
            r = requests.get(
                f"{CW_BASE}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conversation_id}/messages",
                headers=_cw_headers(), timeout=10,
            )
            r.raise_for_status()
            payload = r.json().get("payload", [])
        except Exception as e:
            print(f"[CHATWOOT] history ERROR: {e}")
            return []

        # La API de Chatwoot no garantiza orden ascendente (normalmente devuelve
        # los mensajes más recientes primero, pensado para paginar hacia atrás).
        # Ordenamos explícitamente por fecha para no armar el historial al revés
        # o cortarlo del lado equivocado con `history[-limit:]`.
        payload = sorted(payload, key=lambda m: m.get("created_at") or 0)

        history = []
        for m in payload:
            if m.get("id") in exclude_ids:
                continue
            content = (m.get("content") or "").strip()
            # message_type: 0 = incoming (cliente), 1 = outgoing (bot/agente).
            if not content or m.get("message_type") not in (0, 1):
                continue
            role = "user" if m.get("message_type") == 0 else "model"
            history.append({"role": role, "text": content})
        return history[-limit:]

    def cw_process(conversation_id: int, phone: str, text: str, message_id: str = "", exclude_ids=None) -> None:
        """Corre el cerebro y responde por Chatwoot, en segundo plano (igual que WhatsApp)."""
        start = time.time()
        # Muestra "escribiendo..." en WhatsApp usando el id (wamid) del mensaje entrante.
        send_typing(message_id)
        history = cw_fetch_history(conversation_id, exclude_ids)
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
            _CW_HANDOFF[conversation_id] = time.time()
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

        # Solo nos interesan mensajes nuevos ENTRANTES (del cliente), no los salientes/bot.
        if event != "message_created" or data.get("message_type") != "incoming":
            return make_response("ok", 200)

        text = (data.get("content") or "").strip()
        if not conversation_id or not text:
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
            _CW_HANDOFF[conversation_id] = time.time()
            razon = "lo pidió" if pide_humano else "repitió mensaje"
            print(f"[CHATWOOT] handoff conv {conversation_id} ({razon})")
            cw_send(conversation_id, HANDOFF_REPLY)
            cw_handoff(conversation_id)
            return make_response("ok", 200)

        phone = _cw_phone(data)
        # source_id = id del mensaje original de WhatsApp (wamid); lo usamos para el typing.
        message_id = data.get("source_id") or ""
        cw_message_id = data.get("id")
        print(f"[CHATWOOT] conv {conversation_id} ({phone}) wamid={message_id or '∅'}: {text}")
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
