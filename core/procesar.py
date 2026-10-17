"""Single entry point shared by every channel (Telegram, CSV batch)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Callable

from core.modelos import Accion, Contexto, Resultado

log = logging.getLogger(__name__)

Interprete = Callable[[str, Contexto], list[Accion]]


def _interpretar_default(texto: str, contexto: Contexto) -> tuple[list[Accion], str]:
    from core import agente

    if hasattr(agente, "interpretar_con_traza"):
        return agente.interpretar_con_traza(texto, contexto)
    acciones = agente.interpretar(texto, contexto)
    return acciones, json.dumps([asdict(a) for a in acciones], ensure_ascii=False)


def procesar_mensaje(
    texto: str,
    *,
    tienda_id: str,
    id_mensaje: str,
    dia: str | None,
    memoria,
    interpretar: Interprete | None = None,
) -> Resultado:
    from core.ejecutor import ejecutar

    contexto = memoria.contexto(tienda_id, dia)
    if interpretar is None:
        acciones, traza = _interpretar_default(texto, contexto)
    else:
        acciones = interpretar(texto, contexto)
        traza = json.dumps([asdict(a) for a in acciones], ensure_ascii=False)

    resultado = ejecutar(
        acciones, texto, tienda_id=tienda_id, id_mensaje=id_mensaje, dia=dia, memoria=memoria
    )
    try:
        memoria.log(tienda_id, id_mensaje, texto, traza, resultado)
    except Exception:  # logging must never break a reply
        log.exception("no se pudo guardar el log de %s", id_mensaje)
    return resultado
