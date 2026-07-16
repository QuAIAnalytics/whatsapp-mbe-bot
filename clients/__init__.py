"""
clients — el "cerebro" del bot (solo MBE en este despliegue)
==============================================================
main.py llama a get_handler() para obtener handle(phone, text) -> str | list[str] | None.
"""

from clients import mbe


def get_handler():
    return mbe.handle
