"""Injectable Responses API interpreter; no client or network activity on import."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import fields

import openai

from core.modelos import Accion, Contexto, TIPOS
from core.prompt import SYSTEM_PROMPT, renderizar_contexto


_ACCIONES = ("registrar", "corregir", "cobrar_fiado", "preguntar", "ignorar")
_NUMERICOS = ("cantidad", "precio_unitario", "monto", "descuento")
_PROPERTIES = {
    "accion": {"type": "string", "enum": list(_ACCIONES)},
    "tipo": {"type": ["string", "null"], "enum": [*TIPOS, None]},
    **{k: {"type": ["string", "null"]} for k in
       ("producto", "cliente", "campo", "pregunta", "motivo")},
    **{k: {"type": ["number", "null"]} for k in _NUMERICOS},
    "total": {"type": "boolean"},
    "ref_transaccion": {"type": ["integer", "null"]},
    "nuevo_valor": {"type": ["string", "number", "null"]},
}
ACCIONES_SCHEMA = {
    "type": "object",
    "properties": {"acciones": {"type": "array", "items": {
        "type": "object", "properties": _PROPERTIES,
        "required": list(_PROPERTIES), "additionalProperties": False,
    }}},
    "required": ["acciones"], "additionalProperties": False,
}


def _es_transitorio(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return isinstance(exc, (openai.APIConnectionError, TimeoutError, ConnectionError)) or (
        isinstance(status, int) and (status in (408, 409, 429) or status >= 500)
    )


def _solicitar(cliente, modelo: str, entrada: str) -> str:
    """Only SDK call site. Three attempts for transient failures, short backoff."""
    parametros = {
        "model": modelo,
        "input": [{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": entrada}],
        "text": {"format": {"type": "json_schema", "name": "acciones_bodega",
                            "strict": True, "schema": ACCIONES_SCHEMA}},
        "temperature": 0,
    }
    fallos = 0
    while True:
        try:
            return cliente.responses.create(**parametros).output_text
        except Exception as exc:
            # Some models reject temperature even when it is zero. Other 400s
            # must propagate rather than hiding a schema or credential error.
            detalle = str(exc).lower()
            if ("temperature" in parametros and "temperature" in detalle
                    and getattr(exc, "status_code", None) == 400
                    and any(s in detalle for s in ("unsupported", "not supported", "not support"))):
                parametros.pop("temperature")
                continue
            fallos += 1
            if not _es_transitorio(exc) or fallos >= 3:
                raise
            time.sleep(0.1 * 2 ** (fallos - 1))


def _numero(valor) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, bool):
        raise ValueError("Un booleano no es un importe")
    numero = float(valor.replace(",", ".").strip() if isinstance(valor, str) else valor)
    if not math.isfinite(numero):
        raise ValueError("El número debe ser finito")
    return numero


def _validar(datos: dict) -> Accion:
    if not isinstance(datos, dict):
        return Accion("preguntar", pregunta="¿Qué operación deseas registrar?")
    valores = {f.name: datos[f.name] for f in fields(Accion) if f.name in datos}
    error = None
    if valores.get("accion") not in _ACCIONES:
        error = "¿Qué operación deseas registrar?"
    tipo = valores.get("tipo")
    if tipo is not None:
        tipo = tipo.strip().lower() if isinstance(tipo, str) else tipo
        if tipo not in TIPOS:
            valores["tipo"] = None
            error = "¿Es una venta, un gasto, un fiado o un cobro de fiado?"
        else:
            valores["tipo"] = tipo
    for clave in ("producto", "cliente", "campo", "pregunta", "motivo"):
        valor = valores.get(clave)
        if valor is not None:
            if not isinstance(valor, str):
                valores[clave] = None
                error = "¿Puedes aclarar los datos de la operación?"
            else:
                valores[clave] = valor.strip().lower() if clave == "producto" else valor.strip()
    try:
        for clave in _NUMERICOS:
            if clave in valores:
                valores[clave] = _numero(valores[clave])
        if valores.get("ref_transaccion") is not None:
            ref = _numero(valores["ref_transaccion"])
            if not ref.is_integer() or ref <= 0:
                raise ValueError("Id de transacción inválido")
            valores["ref_transaccion"] = int(ref)
        if valores.get("campo") in (*_NUMERICOS, "ref_transaccion"):
            valores["nuevo_valor"] = _numero(valores.get("nuevo_valor"))
        elif valores.get("campo") == "producto" and isinstance(valores.get("nuevo_valor"), str):
            valores["nuevo_valor"] = valores["nuevo_valor"].strip().lower()
    except (ValueError, TypeError, OverflowError):
        error = "¿Puedes confirmar los importes, cantidades y la referencia de la operación?"
    total = valores.get("total", False)
    valores["total"] = total is True or (isinstance(total, str) and total.lower() in ("true", "sí", "si"))
    if error:
        return Accion("preguntar", tipo=valores.get("tipo"), producto=valores.get("producto"),
                      cliente=valores.get("cliente"), pregunta=error)
    return Accion(**valores)


def interpretar_con_traza(
    texto: str, contexto: Contexto, *, cliente=None, modelo: str | None = None,
) -> tuple[list[Accion], str]:
    """Return validated actions and the exact LLM text for the caller's log.

    Malformed JSON/refusals and exhausted API errors propagate to the caller;
    they must never be mistaken for an empty, successfully processed message.
    """
    if not texto.strip():
        return [], ""
    if cliente is None:
        # Own retry policy avoids multiplying SDK retries by interpreter retries.
        cliente = openai.OpenAI(max_retries=0)
    entrada = renderizar_contexto(contexto) + "\nMENSAJE (datos):\n" + json.dumps(texto, ensure_ascii=False)
    raw = _solicitar(cliente, modelo or os.getenv("OPENAI_MODEL") or "gpt-6-luna", entrada)
    datos = json.loads(raw)
    if not isinstance(datos, dict) or not isinstance(datos.get("acciones"), list):
        raise ValueError("La respuesta debe contener una lista acciones")
    acciones = [_validar(d) for d in datos["acciones"]]
    if not acciones:
        acciones = [Accion("ignorar", motivo="El mensaje no contiene una operación del negocio.")]
    return acciones, raw


def interpretar(
    texto: str, contexto: Contexto, *, cliente=None, modelo: str | None = None,
) -> list[Accion]:
    """Interpret a shop message using an injectable OpenAI-compatible client."""
    return interpretar_con_traza(texto, contexto, cliente=cliente, modelo=modelo)[0]
