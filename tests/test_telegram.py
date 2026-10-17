import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from canales import telegram as tg
from core.modelos import Contexto, Deuda, Resultado, Transaccion


@pytest.fixture
def entorno(monkeypatch):
    monkeypatch.setattr(tg, "ahora", lambda: datetime(2026, 10, 17, 12, tzinfo=tg.LIMA))
    memoria = SimpleNamespace(
        pendiente=Mock(return_value=None), fijar_pendiente=Mock(),
        transacciones=Mock(return_value=[]), contexto=Mock(return_value=Contexto()),
        borrar_tienda=Mock(), deudas_abiertas=Mock(return_value=[]),
    )
    mensaje = SimpleNamespace(
        message_id=42, text="Vendí pan por 3 soles", reply_text=AsyncMock(),
        reply_document=AsyncMock(), voice=None, audio=None, document=None,
        reply_to_message=None, caption=None,
    )
    update = SimpleNamespace(effective_message=mensaje, effective_chat=SimpleNamespace(id=123))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"memoria": memoria}), chat_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
    return update, context, memoria


def test_texto_procesa_y_responde(entorno, monkeypatch):
    update, context, memoria = entorno
    procesar = Mock(return_value=Resultado(respuesta="Anotado 📒"))
    monkeypatch.setattr(tg, "procesar_mensaje", procesar)
    asyncio.run(tg.texto(update, context))
    procesar.assert_called_once_with("Vendí pan por 3 soles", tienda_id="tg:123", id_mensaje="tg-42", dia="sábado", memoria=memoria)
    update.effective_message.reply_text.assert_awaited_once_with("Anotado 📒", reply_markup=None)


def test_respuesta_pendiente_incluye_pregunta_y_se_limpia(entorno, monkeypatch):
    update, context, memoria = entorno
    memoria.pendiente.return_value = {"pregunta": "¿Cuánto costó?", "transaccion_id": 7}
    update.effective_message.text = "5 soles"
    procesar = Mock(return_value=Resultado(respuesta="Corregido"))
    monkeypatch.setattr(tg, "procesar_mensaje", procesar)
    asyncio.run(tg.texto(update, context))
    assert procesar.call_args.args[0] == "Respuesta a la pregunta '¿Cuánto costó?' sobre la transacción #7: 5 soles"
    memoria.fijar_pendiente.assert_called_once_with("tg:123", None)


def test_conserva_nueva_pregunta_y_ofrece_omitir(entorno, monkeypatch):
    update, context, memoria = entorno
    memoria.pendiente.side_effect = [None, {"pregunta": "¿Monto?", "transaccion_id": 9}]
    monkeypatch.setattr(tg, "procesar_mensaje", Mock(return_value=Resultado(respuesta="¿Monto?")))
    asyncio.run(tg.texto(update, context))
    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "omitir:9"
    memoria.fijar_pendiente.assert_not_called()



def test_conserva_pregunta_reiterada_si_no_se_resolvio(entorno, monkeypatch):
    update, context, memoria = entorno
    memoria.pendiente.return_value = {"pregunta": "¿Monto?", "transaccion_id": 9}
    monkeypatch.setattr(tg, "procesar_mensaje", Mock(return_value=Resultado(
        respuesta="¿Monto?", preguntas=["¿Monto?"])))
    asyncio.run(tg.texto(update, context))
    memoria.fijar_pendiente.assert_not_called()
    assert update.effective_message.reply_text.call_args.kwargs["reply_markup"] is not None


def test_fallo_no_pierde_pregunta(entorno, monkeypatch):
    update, context, memoria = entorno
    memoria.pendiente.return_value = {"pregunta": "¿Monto?", "transaccion_id": 9}
    monkeypatch.setattr(tg, "procesar_mensaje", Mock(side_effect=RuntimeError("sin red")))
    with pytest.raises(RuntimeError):
        asyncio.run(tg.texto(update, context))
    memoria.fijar_pendiente.assert_not_called()


def test_flujo_formato_y_semana(entorno, monkeypatch, tmp_path):
    update, context, memoria = entorno
    registro = tg.RegistroTelegram(str(tmp_path / "libreta.db"))
    context.application.bot_data["registro_telegram"] = registro
    registro.db.execute("INSERT INTO telegram_semanas VALUES (?, ?, ?)", ("tg:123", "anterior", "2026-10-05"))
    registro.db.commit()
    actual = Transaccion("tg:123", "actual", "venta", "pan", monto=10)
    anterior = Transaccion("tg:123", "anterior", "venta", "leche", monto=50)
    memoria.transacciones.return_value = [anterior, actual]
    calcular = Mock(return_value=dict(ventas=10, cobros=2, gastos=3, flujo=9, fiado_pendiente=4, sin_monto=1))
    monkeypatch.setattr(tg, "flujo_semanal", calcular)
    try:
        asyncio.run(tg.flujo(update, context))
        calcular.assert_called_once_with([actual])
        respuesta = update.effective_message.reply_text.call_args.args[0]
        assert "Ventas: S/ 10.00" in respuesta
        assert "Flujo: S/ 9.00" in respuesta
        assert "Sin monto: 1" in respuesta
    finally:
        registro.close()


def test_audio_transcribe_con_vocabulario_y_procesa(entorno, monkeypatch):
    update, context, memoria = entorno
    archivo = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg")))
    update.effective_message.voice = SimpleNamespace(get_file=AsyncMock(return_value=archivo))
    memoria.contexto.return_value = Contexto(
        deudas_abiertas=[Deuda(1, "Nelly", "pan", 2, 2)],
        ultimas=[Transaccion("tg:123", "a", "venta", "leche gloria")],
    )
    transcribir = Mock(return_value="Vendí una leche")
    procesar = Mock(return_value=Resultado(respuesta="Anotado"))
    monkeypatch.setattr(tg, "transcribir", transcribir)
    monkeypatch.setattr(tg, "procesar_mensaje", procesar)
    asyncio.run(tg.audio(update, context))
    transcribir.assert_called_once_with(b"ogg", "nota.ogg", ["Nelly", "leche gloria"])
    assert update.effective_message.reply_text.await_args_list[0].args[0] == "🎤 Escuché: Vendí una leche"
    assert procesar.call_args.args[0] == "Vendí una leche"


def test_boton_omitir_y_reset_confirmado(entorno):
    update, context, memoria = entorno
    memoria.pendiente.return_value = {"transaccion_id": 7, "pregunta": "¿Monto?"}
    update.callback_query = SimpleNamespace(data="omitir:7", answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(tg.boton(update, context))
    memoria.fijar_pendiente.assert_called_once_with("tg:123", None)
    asyncio.run(tg.reiniciar(update, context))
    memoria.borrar_tienda.assert_not_called()
    update.callback_query.data = "reiniciar:si"
    asyncio.run(tg.boton(update, context))
    memoria.borrar_tienda.assert_called_once_with("tg:123")


def test_lote_aislado_devuelve_documento(entorno, monkeypatch):
    update, context, memoria = entorno
    archivo = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b"id_mensaje,dia,mensaje\n1,lunes,hola"))
    update.effective_message.document = SimpleNamespace(file_name="input.csv", get_file=AsyncMock(return_value=archivo))
    update.effective_message.caption = "/lote"

    def csv(entrada, salida, **kwargs):
        assert Path(entrada).read_bytes().startswith(b"id_mensaje")
        assert kwargs["tienda_id"].startswith("lote:123:")
        assert kwargs["memoria"] is memoria
        Path(salida).write_text("id_mensaje,tipo\n1,venta\n")
        return [Transaccion(kwargs["tienda_id"], "1", "venta", "pan", requiere_confirmacion=True)]

    monkeypatch.setattr(tg, "procesar_csv", csv)
    monkeypatch.setattr(tg, "flujo_semanal", lambda filas: {"flujo": 0})
    asyncio.run(tg.documento(update, context))
    datos = update.effective_message.reply_document.call_args.kwargs
    assert datos["filename"] == "entrega.csv"
    assert "1 filas · 1 con SI" in datos["caption"]
    assert datos["document"].getvalue().startswith(b"id_mensaje")


def test_registro_persiste_chats_y_fechas(tmp_path):
    path = str(tmp_path / "libreta.db")
    registro = tg.RegistroTelegram(path)
    registro.recordar(123)
    registro.fechar("tg:123", "tg-42")
    registro.close()
    registro = tg.RegistroTelegram(path)
    assert registro.chats() == [123]
    assert registro.semanas("tg:123")["tg-42"] == tg.semana_actual()
    registro.close()


def test_resumen_programado_usa_chats_guardados(entorno, monkeypatch, tmp_path):
    _, context, _ = entorno
    registro = tg.RegistroTelegram(str(tmp_path / "libreta.db"))
    registro.recordar(123)
    context.application.bot_data["registro_telegram"] = registro
    monkeypatch.setattr(tg, "flujo_semanal", lambda filas: dict(ventas=0, cobros=0, gastos=0, flujo=0, fiado_pendiente=0, sin_monto=0))
    try:
        asyncio.run(tg.resumen_semanal(context))
        assert context.bot.send_message.call_args.kwargs["chat_id"] == 123
    finally:
        registro.close()


def test_aplicacion_tiene_job_domingo_lima(tmp_path):
    # Building an application does not contact Telegram.
    app = tg.crear_aplicacion("123:ABC", object(), db_path=str(tmp_path / "libreta.db"))
    try:
        job = app.job_queue.get_jobs_by_name("resumen-domingo")[0]
        trigger = job.job.trigger
        assert str(trigger.timezone) == "America/Lima"
        assert "day_of_week='sun'" in str(trigger)
        assert "hour='20'" in str(trigger)
    finally:
        app.bot_data["registro_telegram"].close()
