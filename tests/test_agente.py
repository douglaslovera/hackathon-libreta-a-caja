"""Offline interpreter contract tests: no credentials or network required."""

import json
from dataclasses import asdict, fields
from types import SimpleNamespace

import httpx
import openai
import pytest

from core import agente
from core.modelos import Accion, Contexto, Deuda, Transaccion
from core.prompt import SYSTEM_PROMPT, renderizar_contexto


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(output_text=result)


def respuesta(*acciones):
    return json.dumps({"acciones": [asdict(a) if isinstance(a, Accion) else a for a in acciones]})


def error_api(status, message):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test/responses"))
    clase = openai.BadRequestError if status == 400 else openai.APIStatusError
    return clase(message, response=response, body=None)


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    def prohibido(*args, **kwargs):
        raise AssertionError("No se debe crear un cliente real")
    monkeypatch.setattr(agente.openai, "OpenAI", prohibido)
    monkeypatch.setattr(agente.time, "sleep", lambda _: None)


def test_schema_request_shape():
    cliente = FakeClient(respuesta(Accion("ignorar", motivo="Saludo")))
    agente.interpretar("Hola", Contexto(dia="lunes"), cliente=cliente)
    request = cliente.calls[0]
    assert request["model"] == "gpt-6-luna"
    assert request["temperature"] == 0
    assert request["input"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert request["input"][1]["role"] == "user"
    assert '"Hola"' in request["input"][1]["content"]
    formato = request["text"]["format"]
    assert formato["type"] == "json_schema"
    assert formato["strict"] is True
    schema = formato["schema"]
    assert schema["required"] == ["acciones"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["acciones"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {f.name for f in fields(Accion)}
    assert set(item["properties"]) == set(item["required"])
    for nombre in ("monto", "tipo", "ref_transaccion", "nuevo_valor"):
        assert "null" in item["properties"][nombre]["type"]


def test_multiple_actions_and_exact_raw_trace():
    raw = respuesta(Accion("registrar", tipo="venta", producto="yogur", cantidad=4, monto=8),
                    Accion("registrar", tipo="venta", producto="vela", cantidad=2, monto=2))
    acciones, traza = agente.interpretar_con_traza("Vendí yogures y velas", Contexto(), cliente=FakeClient(raw))
    assert traza == raw
    assert len(acciones) == 2
    assert [a.producto for a in acciones] == ["yogur", "vela"]
    assert [a.monto for a in acciones] == [8, 2]
    assert all(isinstance(a, Accion) for a in acciones)


def test_coercion_unknown_fields_and_correction():
    cliente = FakeClient(respuesta(
        {"accion": "registrar", "tipo": " VENTA ", "producto": " LECHE Gloria ",
         "cantidad": "2", "precio_unitario": "4,50", "monto": "9.00", "descuento": "0",
         "total": "false", "campo_inventado": "se descarta"},
        {"accion": "corregir", "ref_transaccion": "17", "campo": "monto", "nuevo_valor": "8,50"}))
    venta, correccion = agente.interpretar("datos", Contexto(), cliente=cliente)
    assert venta.producto == "leche gloria"
    assert venta.tipo == "venta"
    assert (venta.cantidad, venta.precio_unitario, venta.monto, venta.descuento) == (2, 4.5, 9, 0)
    assert venta.total is False
    assert correccion.ref_transaccion == 17
    assert correccion.nuevo_valor == 8.5


@pytest.mark.parametrize("tipo", ["ingreso", 123, ["venta"]])
def test_invalid_tipo_becomes_question(tipo):
    cliente = FakeClient(respuesta({"accion": "registrar", "tipo": tipo, "producto": "PAN", "monto": 5}))
    accion, = agente.interpretar("datos", Contexto(), cliente=cliente)
    assert accion.accion == "preguntar"
    assert accion.tipo is None
    assert accion.producto == "pan"
    assert accion.pregunta
    assert accion.monto is None


@pytest.mark.parametrize("monto", ["desconocido", "NaN", "Infinity", True, {}])
def test_invalid_number_becomes_question(monto):
    cliente = FakeClient(respuesta({"accion": "registrar", "tipo": "venta", "monto": monto}))
    accion, = agente.interpretar("datos", Contexto(), cliente=cliente)
    assert accion.accion == "preguntar"
    assert accion.monto is None


def test_retry_transient_exception(monkeypatch):
    pausas = []
    monkeypatch.setattr(agente.time, "sleep", pausas.append)
    cliente = FakeClient(error_api(429, "rate limit"), TimeoutError("timeout"),
                         respuesta(Accion("ignorar")))
    assert agente.interpretar("Hola", Contexto(), cliente=cliente)[0].accion == "ignorar"
    assert len(cliente.calls) == 3
    assert pausas == [0.1, 0.2]


def test_retry_limit():
    cliente = FakeClient(*(error_api(503, "unavailable") for _ in range(4)))
    with pytest.raises(openai.APIStatusError):
        agente.interpretar("datos", Contexto(), cliente=cliente)
    assert len(cliente.calls) == 3


def test_temperature_rejected_retries_without_parameter():
    cliente = FakeClient(error_api(400, "Unsupported parameter: 'temperature'"), respuesta(Accion("ignorar")))
    agente.interpretar("Hola", Contexto(), cliente=cliente)
    assert cliente.calls[0]["temperature"] == 0
    assert "temperature" not in cliente.calls[1]
    assert cliente.calls[0]["text"] == cliente.calls[1]["text"]


def test_other_errors_not_retried():
    cliente = FakeClient(error_api(400, "Invalid schema"))
    with pytest.raises(openai.BadRequestError):
        agente.interpretar("datos", Contexto(), cliente=cliente)
    assert len(cliente.calls) == 1


def test_context_contains_debt_and_transaction_ids():
    contexto = Contexto(
        dia="miércoles", deudas_abiertas=[Deuda(41, "Elena", "champú", 20, 7), Deuda(42, "Luis", "vela", None, None)],
        ultimas=[Transaccion("tienda", "mensaje", "fiado", "champú", cantidad=2, monto=20, cliente="Elena", id=41)],
        alias={"elenita": "Elena"})
    bloque = renderizar_contexto(contexto)
    datos = json.loads(bloque.split("\n", 1)[1])
    assert datos["dia"] == "miércoles"
    assert datos["deudas_abiertas"][0] == {"transaccion_id": 41, "cliente": "Elena", "producto": "champú", "pendiente": 7}
    assert datos["deudas_abiertas"][1]["pendiente"] is None
    assert datos["ultimas"][0]["id"] == 41
    assert datos["ultimas"][0]["cantidad"] == 2
    assert datos["alias"] == {"elenita": "Elena"}
    cliente = FakeClient(respuesta(Accion("cobrar_fiado", tipo="cobro_fiado", ref_transaccion=41)))
    agente.interpretar("Elena pagó", contexto, cliente=cliente)
    assert bloque in cliente.calls[0]["input"][1]["content"]


def test_model_environment_and_explicit_override(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "modelo-entorno")
    cliente = FakeClient(respuesta(Accion("ignorar")), respuesta(Accion("ignorar")))
    agente.interpretar("Hola", Contexto(), cliente=cliente)
    agente.interpretar("Hola", Contexto(), cliente=cliente, modelo="modelo-explícito")
    assert [c["model"] for c in cliente.calls] == ["modelo-entorno", "modelo-explícito"]


def test_empty_message_does_not_create_client():
    assert agente.interpretar(" \n", Contexto()) == []


def test_missing_amount_preserved_without_inference():
    cliente = FakeClient(respuesta(Accion("preguntar", tipo="fiado", producto="champú", cantidad=2,
                                         cliente="Elena", pregunta="¿Por cuánto lo fiaste?")))
    accion, = agente.interpretar("Le fié champú a Elena", Contexto(), cliente=cliente)
    assert accion.monto is None
    assert accion.precio_unitario is None
    assert accion.cantidad == 2
    assert accion.accion == "preguntar"


@pytest.mark.parametrize("raw", ["no JSON", '{"acciones":{}}', '[]', 'null'])
def test_malformed_response_is_not_silently_ignored(raw):
    with pytest.raises(ValueError):
        agente.interpretar("datos", Contexto(), cliente=FakeClient(raw))


def test_empty_llm_actions_for_nonempty_message_returns_explicit_ignore():
    accion, = agente.interpretar("Hola", Contexto(), cliente=FakeClient(respuesta()))
    assert accion.accion == "ignorar"
