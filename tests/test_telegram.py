import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from canales import telegram as tg
from core.modelos import Deuda, Resultado, Transaccion


@pytest.fixture
def entorno():
    libreta = SimpleNamespace(
        registrar=Mock(return_value=Resultado(respuesta="Anotado 📒")),
        registrar_audio=Mock(return_value=("Vendí una leche", Resultado(respuesta="Anotado"))),
        pendiente=Mock(return_value=None), omitir_pendiente=Mock(return_value=True),
        reiniciar=Mock(), transacciones=Mock(return_value=[]),
        flujo=Mock(return_value=dict(ventas=10, cobros=2, gastos=3, flujo=9,
                                    fiado_pendiente=4, sin_monto=1)),
        cuota=Mock(return_value=dict(cuota=25.5, explicacion="No es aprobación de crédito.")),
        deudas=Mock(return_value=[]), lote=Mock(),
    )
    mensaje = SimpleNamespace(
        message_id=42, text="Vendí pan por 3 soles", reply_text=AsyncMock(),
        reply_document=AsyncMock(), voice=None, audio=None, document=None,
        reply_to_message=None, caption=None,
    )
    update = SimpleNamespace(effective_message=mensaje, effective_chat=SimpleNamespace(id=123))
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"libreta": libreta}), chat_data={},
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    return update, context, libreta


def archivo(nombre="input.csv", contenido=b"id_mensaje,dia,mensaje\n1,lunes,hola"):
    descarga = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(contenido)))
    return SimpleNamespace(file_name=nombre, get_file=AsyncMock(return_value=descarga))


def test_texto_delega_sin_modificar_respuesta_pendiente(entorno):
    update, context, libreta = entorno
    update.effective_message.text = "5 soles"
    asyncio.run(tg.texto(update, context))
    libreta.registrar.assert_called_once_with("5 soles", tienda_id="tg:123", id_mensaje="tg-42")
    libreta.pendiente.assert_called_once_with("tg:123")
    update.effective_message.reply_text.assert_awaited_once_with("Anotado 📒", reply_markup=None)


@pytest.mark.parametrize("clave", ["transaccion_id", "id", "ref_transaccion"])
def test_ofrece_omitir_si_hay_pendiente(entorno, clave):
    update, context, libreta = entorno
    libreta.pendiente.return_value = {clave: 9, "pregunta": "¿Monto?"}
    asyncio.run(tg.texto(update, context))
    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "Omitir"
    assert markup.inline_keyboard[0][0].callback_data == "omitir:9"


def test_respuesta_vacia_y_fragmentos_de_4000(entorno):
    update, context, libreta = entorno
    libreta.registrar.return_value = Resultado()
    asyncio.run(tg.texto(update, context))
    assert update.effective_message.reply_text.call_args.args[0] == "Listo, casera 📒"
    update.effective_message.reply_text.reset_mock()
    libreta.registrar.return_value = Resultado(respuesta="x" * 8001)
    asyncio.run(tg.texto(update, context))
    assert [len(c.args[0]) for c in update.effective_message.reply_text.await_args_list] == [4000, 4000, 1]


@pytest.mark.parametrize("tipo,nombre", [("voice", None), ("audio", "venta.mp3")])
def test_audio_descarga_delega_y_responde_en_orden(entorno, tipo, nombre):
    update, context, libreta = entorno
    setattr(update.effective_message, tipo, archivo(nombre, b"audio"))
    libreta.pendiente.return_value = {"transaccion_id": 7}
    asyncio.run(tg.audio(update, context))
    libreta.registrar_audio.assert_called_once_with(
        b"audio", nombre or "nota.ogg", tienda_id="tg:123", id_mensaje="tg-42")
    respuestas = update.effective_message.reply_text.await_args_list
    assert [c.args[0] for c in respuestas] == ["🎤 Escuché: Vendí una leche", "Anotado"]
    assert respuestas[1].kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "omitir:7"
    libreta.registrar.assert_not_called()


def test_audio_sin_voz(entorno):
    update, context, libreta = entorno
    update.effective_message.voice = archivo(None, b"ogg")
    libreta.registrar_audio.return_value = ("", None)
    asyncio.run(tg.audio(update, context))
    update.effective_message.reply_text.assert_awaited_once_with(
        "No alcancé a escucharte. ¿Me mandas la nota otra vez?")
    libreta.pendiente.assert_not_called()


@pytest.mark.parametrize("omitido,respuesta", [
    (True, "Listo, dejamos ese dato por confirmar 📒"),
    (False, "Esa pregunta ya fue atendida 📒"),
])
def test_boton_omitir(entorno, omitido, respuesta):
    update, context, libreta = entorno
    libreta.omitir_pendiente.return_value = omitido
    update.callback_query = SimpleNamespace(data="omitir:7", answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(tg.boton(update, context))
    libreta.omitir_pendiente.assert_called_once_with("tg:123", "7")
    update.callback_query.answer.assert_awaited_once_with()
    update.callback_query.edit_message_text.assert_awaited_once_with(respuesta)


@pytest.mark.parametrize("confirmado", [True, False])
def test_reiniciar_requiere_confirmacion(entorno, confirmado):
    update, context, libreta = entorno
    asyncio.run(tg.reiniciar(update, context))
    libreta.reiniciar.assert_not_called()
    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["reiniciar:si", "reiniciar:no"]
    context.chat_data["esperando_csv"] = True
    update.callback_query = SimpleNamespace(
        data="reiniciar:si" if confirmado else "reiniciar:no", answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(tg.boton(update, context))
    if confirmado:
        libreta.reiniciar.assert_called_once_with("tg:123")
        assert "esperando_csv" not in context.chat_data
        respuesta = "Libreta en blanco. ¡Empezamos de nuevo! 📒"
    else:
        libreta.reiniciar.assert_not_called()
        respuesta = "Seguimos con tu libreta 📒"
    update.callback_query.edit_message_text.assert_awaited_once_with(respuesta)


def test_flujo_formato(entorno):
    update, context, libreta = entorno
    asyncio.run(tg.flujo(update, context))
    libreta.flujo.assert_called_once_with("tg:123")
    update.effective_message.reply_text.assert_awaited_once_with(
        "📒 Esta semana\nVentas: S/ 10.00\nCobros: S/ 2.00\nGastos: S/ 3.00\n"
        "💰 Flujo: S/ 9.00\nFiado pendiente: S/ 4.00\nSin monto: 1")


def test_cuota_respuesta(entorno):
    update, context, libreta = entorno
    asyncio.run(tg.cuota(update, context))
    libreta.cuota.assert_called_once_with("tg:123")
    update.effective_message.reply_text.assert_awaited_once_with(
        "🏦 Cuota mensual orientativa: S/ 25.50\nNo es aprobación de crédito.")


@pytest.mark.parametrize("vacio", [True, False])
def test_deudas_respuesta(entorno, vacio):
    update, context, libreta = entorno
    if not vacio:
        libreta.deudas.return_value = [Deuda(1, "Rosa", "pan", 5, 5), Deuda(2, "Ana", "leche", None, None)]
    asyncio.run(tg.deudas(update, context))
    libreta.deudas.assert_called_once_with("tg:123")
    esperado = "¡No tienes fiados pendientes! 🙌" if vacio else (
        "📝 Fiados pendientes\nRosa · pan: S/ 5.00\nAna · leche: monto por confirmar")
    update.effective_message.reply_text.assert_awaited_once_with(esperado)


@pytest.mark.parametrize("vacio", [True, False])
def test_resumen_pide_semana_actual_al_servicio(entorno, vacio):
    update, context, libreta = entorno
    if not vacio:
        libreta.transacciones.return_value = [Transaccion("tg:123", "m", "venta", "pan", monto=None, id=3, dia="lunes")]
    asyncio.run(tg.resumen(update, context))
    libreta.transacciones.assert_called_once_with("tg:123")
    esperado = "Aún no hay movimientos esta semana 📒" if vacio else (
        "📒 Movimientos de esta semana\n#3 lunes · venta · pan · por confirmar")
    update.effective_message.reply_text.assert_awaited_once_with(esperado)


@pytest.mark.parametrize("modo", ["comando", "leyenda", "respuesta", "esperando"])
def test_lote_envia_documento_y_caption(entorno, modo):
    update, context, libreta = entorno
    doc = archivo()
    filas = [Transaccion("lote", "1", "venta", "pan", requiere_confirmacion=True)]
    libreta.lote.return_value = (b"id_mensaje,tipo\n1,venta\n", filas, {"flujo": 12.5})
    update.effective_message.document = doc
    handler = tg.lote
    if modo == "leyenda":
        update.effective_message.caption = "/lote@mi_bot"
        handler = tg.documento
    elif modo == "respuesta":
        update.effective_message.document = None
        update.effective_message.reply_to_message = SimpleNamespace(document=doc)
    elif modo == "esperando":
        context.chat_data["esperando_csv"] = True
        handler = tg.documento
    asyncio.run(handler(update, context))
    libreta.lote.assert_called_once_with(b"id_mensaje,dia,mensaje\n1,lunes,hola", prefijo="123")
    datos = update.effective_message.reply_document.call_args.kwargs
    assert datos["filename"] == "entrega.csv"
    assert datos["caption"] == "1 filas · 1 con SI · Flujo: S/ 12.50"
    assert datos["document"].getvalue() == b"id_mensaje,tipo\n1,venta\n"
    assert "esperando_csv" not in context.chat_data


def test_lote_solicita_csv_y_rechaza_otro_formato(entorno):
    update, context, libreta = entorno
    asyncio.run(tg.lote(update, context))
    assert context.chat_data["esperando_csv"]
    update.effective_message.reply_text.assert_awaited_with("Mándame tu CSV con columnas id_mensaje,dia,mensaje 📎")
    update.effective_message.document = archivo("foto.png")
    asyncio.run(tg.documento(update, context))
    update.effective_message.reply_text.assert_awaited_with("Necesito un archivo .csv, casera 📎")
    libreta.lote.assert_not_called()


def test_registro_solo_persiste_chats(tmp_path):
    path = str(tmp_path / "libreta.db")
    registro = tg.RegistroTelegram(path)
    registro.recordar(123)
    registro.recordar(123)
    registro.close()
    registro = tg.RegistroTelegram(path)
    try:
        assert registro.chats() == [123]
        assert registro.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("telegram_chats",)]
    finally:
        registro.close()


def test_resumen_programado_usa_chats_y_continua_tras_error(entorno, tmp_path):
    update, context, libreta = entorno
    registro = tg.RegistroTelegram(str(tmp_path / "libreta.db"))
    context.application.bot_data["registro_telegram"] = registro
    try:
        asyncio.run(tg.inicio(update, context))
        registro.recordar(456)
        context.bot.send_message.side_effect = [RuntimeError("chat bloqueado"), None]
        asyncio.run(tg.resumen_semanal(context))
        assert libreta.flujo.call_args_list == [call("tg:123"), call("tg:456")]
        assert context.bot.send_message.await_args_list == [
            call(chat_id=123, text=tg._formato_flujo(libreta.flujo.return_value)),
            call(chat_id=456, text=tg._formato_flujo(libreta.flujo.return_value)),
        ]
    finally:
        registro.close()


def test_aplicacion_guarda_libreta_y_programa_domingo_lima(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "libreta.db"))
    libreta = object()
    app = tg.crear_aplicacion("123:ABC", libreta)
    try:
        assert app.bot_data["libreta"] is libreta
        trigger = app.job_queue.get_jobs_by_name("resumen-domingo")[0].job.trigger
        assert str(trigger.timezone) == "America/Lima"
        assert "day_of_week='sun'" in str(trigger)
        assert "hour='20'" in str(trigger)
        asyncio.run(app.post_shutdown(app))
    except BaseException:
        app.bot_data["registro_telegram"].close()
        raise


def test_error_global_avisa(entorno):
    update, context, _ = entorno
    context.error = RuntimeError("sin red")
    asyncio.run(tg.error(update, context))
    update.effective_message.reply_text.assert_awaited_once_with("Uy, algo falló, ¿me lo repites?")
