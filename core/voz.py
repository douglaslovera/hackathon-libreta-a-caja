"""Transcripción directa de notas de voz de Telegram, sin conversión local."""
from __future__ import annotations

import os
import time

from openai import APIConnectionError, APIStatusError, OpenAI

VOCABULARIO_BASE = (
    "fiado", "soles", "gaseosa", "abarrotes", "balón de gas", "Gloria", "Primor",
    "mayorista", "rebajó", "ventas sueltas", "bodega", "casera", "yape", "Plin",
    "atún", "azúcar", "huevos", "proveedor", "cobro", "leche", "aceite",
)


def transcribir(audio: bytes, nombre_archivo: str = "nota.ogg", vocabulario=(), *, cliente=None) -> str:
    """Transcribe en español; reintenta hasta dos veces ante errores transitorios."""
    # SDK retries are disabled on our client so the total attempt budget is explicit.
    cliente = cliente if cliente is not None else OpenAI(max_retries=0)
    palabras = dict.fromkeys((*VOCABULARIO_BASE, *(str(v).strip() for v in vocabulario if v)))
    prompt = "Nota de una bodega peruana. Vocabulario: " + ", ".join(palabras)
    for intento in range(3):
        try:
            resultado = cliente.audio.transcriptions.create(
                model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
                file=(nombre_archivo, audio),
                language="es",
                prompt=prompt,
            )
            return resultado.text.strip()
        except (APIConnectionError, APIStatusError) as error:
            transitorio = isinstance(error, APIConnectionError) or error.status_code in (408, 409, 429) or error.status_code >= 500
            if not transitorio or intento == 2:
                raise
            time.sleep(0.5 * 2**intento)
    raise AssertionError("unreachable")
