"""
ai.py — helpers compartidos de Gemini
======================================
Todo lo que tiene que ver con hablar con Gemini vive aquí, para que cada
"cliente" (QUAI, MBE, ...) lo reutilice sin repetir código.

Dos formas de usar el modelo:
    chat_reply()  -> conversación CON memoria (guarda historial por sesión)
    ask_once()    -> una sola pregunta SIN memoria (ideal para clasificar intención)
"""

import os
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Inicializa el cliente usando la API Key provista por el usuario.
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Historial en RAM (se borra al reiniciar). La clave es libre: cada cliente
# usa algo como "mbe:5076..." para no mezclar conversaciones entre demos.
SESSIONS: dict[str, list] = {}


def load_info(env_var: str, default_filename: str) -> str:
    """Lee la info del negocio desde un .txt editable (sin tocar código).

    La ruta se toma de la variable de entorno `env_var`; si no está, usa
    `default_filename` junto al proyecto. Así una persona no técnica solo
    edita el .txt y el asistente responde con esa info.
    """
    path = os.environ.get(env_var) or os.path.join(os.path.dirname(__file__), default_filename)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"[WARN] No encontré {path} — el asistente responderá sin esa info.")
        return ""

# Teléfonos donde un humano tomó el control (Bea/el bot calla).
HUMAN_MODE: set[str] = set()

# Teléfonos cuyo cerebro decidió pasar la conversación a un humano en este turno.
# Lo PONE el cliente (ej. quai.py) y lo LEE el canal (main.py) para ejecutar el
# handoff que corresponda (en Chatwoot: pasar a "abierta"; en WhatsApp: modo humano).
HANDOFF_REQUESTS: set[str] = set()


def chat_reply(session_key: str, system_prompt: str, text: str, history: list | None = None) -> str | None:
    """Conversación con memoria. Devuelve la respuesta del modelo o None si falla.

    Por defecto usa el historial en RAM (`SESSIONS`), keyed por `session_key`.
    Si se pasa `history` (lista de dicts {"role": "user"|"model", "text": ...}),
    se usa esa en su lugar (ej. reconstruida desde la conversación de Chatwoot,
    que ya es persistente) y no se toca `SESSIONS`.
    """
    if not client:
        return None
    external_history = history is not None
    if external_history:
        history = [
            types.Content(role=h["role"], parts=[types.Part(text=h["text"])])
            for h in history
        ]
    else:
        history = SESSIONS.get(session_key, [])
    try:
        chat = client.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
            history=history,
        )
        response = chat.send_message(text)
        if not external_history:
            SESSIONS[session_key] = chat.get_history()
        return response.text.strip()
    except Exception as e:
        print(f"[GEMINI] chat ERROR: {e}")
        return None


def ask_once(system_prompt: str, text: str, model: str | None = None) -> str | None:
    """Una sola consulta sin memoria. Útil para clasificar, extraer o decidir rutas.

    `model` permite usar un modelo más barato (ej. flash-lite) para tareas internas.
    """
    if not client:
        return None
    try:
        response = client.models.generate_content(
            model=model or GEMINI_MODEL,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
            contents=text,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[GEMINI] ask_once ERROR: {e}")
        return None
