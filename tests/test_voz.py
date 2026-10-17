from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from openai import APIConnectionError, APIStatusError
import pytest

from core import voz


def cliente_falso(**kwargs):
    crear = Mock(**kwargs)
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=crear))), crear


def test_transcripcion_directa_con_vocabulario(monkeypatch):
    monkeypatch.delenv("OPENAI_TRANSCRIBE_MODEL", raising=False)
    cliente, crear = cliente_falso(return_value=SimpleNamespace(text="  Vendí una leche Gloria.  "))
    assert voz.transcribir(b"ogg", vocabulario=["Doña Nelly", "chocolate Cusco"], cliente=cliente) == "Vendí una leche Gloria."
    datos = crear.call_args.kwargs
    assert datos["model"] == "gpt-4o-mini-transcribe"
    assert datos["language"] == "es"
    assert datos["file"] == ("nota.ogg", b"ogg")
    for palabra in ["fiado", "soles", "Gloria", "Primor", "balón de gas", "Doña Nelly", "chocolate Cusco"]:
        assert palabra in datos["prompt"]


def test_modelo_configurable(monkeypatch):
    monkeypatch.setenv("OPENAI_TRANSCRIBE_MODEL", "modelo-prueba")
    cliente, crear = cliente_falso(return_value=SimpleNamespace(text="hola"))
    voz.transcribir(b"audio", "nota.mp3", cliente=cliente)
    assert crear.call_args.kwargs["model"] == "modelo-prueba"
    assert crear.call_args.kwargs["file"] == ("nota.mp3", b"audio")


def test_reintenta_errores_transitorios(monkeypatch):
    monkeypatch.setattr(voz.time, "sleep", Mock())
    request = httpx.Request("POST", "https://example.test")
    error = APIConnectionError(request=request)
    cliente, crear = cliente_falso(side_effect=[error, SimpleNamespace(text="listo")])
    assert voz.transcribir(b"ogg", cliente=cliente) == "listo"
    assert crear.call_count == 2


@pytest.mark.parametrize("status, intentos", [(429, 3), (503, 3), (401, 1)])
def test_reintentos_limitados(monkeypatch, status, intentos):
    monkeypatch.setattr(voz.time, "sleep", Mock())
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    cliente, crear = cliente_falso(side_effect=APIStatusError("error", response=response, body=None))
    with pytest.raises(APIStatusError):
        voz.transcribir(b"ogg", cliente=cliente)
    assert crear.call_count == intentos
