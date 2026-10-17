"""Canal Telegram: long polling, audio y reportes semanales en hora de Lima."""
from __future__ import annotations

import asyncio
from datetime import time
from io import BytesIO
import logging
import os
import sqlite3
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

LOG = logging.getLogger(__name__)
LIMA = ZoneInfo("America/Lima")


class RegistroTelegram:
    """Chats que reciben el resumen dominical; el negocio vive en Libreta."""

    def __init__(self, path):
        # A persistent connection also supports :memory: in tests. Serialize access
        # with short synchronous queries in the event loop; no network work here.
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS telegram_chats (chat_id INTEGER PRIMARY KEY)")
        self.db.commit()

    def recordar(self, chat_id):
        self.db.execute("INSERT OR IGNORE INTO telegram_chats VALUES (?)", (chat_id,))
        self.db.commit()

    def chats(self):
        return [fila[0] for fila in self.db.execute("SELECT chat_id FROM telegram_chats")]

    def close(self):
        self.db.close()


def _libreta(context):
    return context.application.bot_data["libreta"]


def _registro(context):
    return context.application.bot_data.get("registro_telegram")


def _tienda(update, context):
    chat_id = update.effective_chat.id
    registro = _registro(context)
    if registro:
        registro.recordar(chat_id)
    return f"tg:{chat_id}"


def _formato_flujo(flujo):
    return (f"📒 Esta semana\nVentas: S/ {flujo['ventas']:.2f}\n"
            f"Cobros: S/ {flujo['cobros']:.2f}\nGastos: S/ {flujo['gastos']:.2f}\n"
            f"💰 Flujo: S/ {flujo['flujo']:.2f}\n"
            f"Fiado pendiente: S/ {flujo['fiado_pendiente']:.2f}\n"
            f"Sin monto: {flujo['sin_monto']}")


async def _responder(update, texto, **kwargs):
    # Telegram allows at most 4096 characters per message.
    for inicio in range(0, len(texto), 4000):
        await update.effective_message.reply_text(texto[inicio:inicio + 4000], **kwargs)


async def _responder_resultado(update, context, tienda_id, resultado):
    pendiente = await asyncio.to_thread(_libreta(context).pendiente, tienda_id)
    botones = None
    if pendiente:
        ref = pendiente.get("transaccion_id", pendiente.get("id", pendiente.get("ref_transaccion", "")))
        botones = InlineKeyboardMarkup([[InlineKeyboardButton("Omitir", callback_data=f"omitir:{ref}")]])
    await _responder(update, resultado.respuesta or "Listo, casera 📒", reply_markup=botones)


async def texto(update: Update, context):
    tienda_id = _tienda(update, context)
    resultado = await asyncio.to_thread(
        _libreta(context).registrar, update.effective_message.text,
        tienda_id=tienda_id, id_mensaje=f"tg-{update.effective_message.message_id}",
    )
    await _responder_resultado(update, context, tienda_id, resultado)


async def audio(update: Update, context):
    tienda_id = _tienda(update, context)
    mensaje = update.effective_message
    nota = mensaje.voice or mensaje.audio
    archivo = await nota.get_file()
    contenido = bytes(await archivo.download_as_bytearray())
    nombre = getattr(nota, "file_name", None) or "nota.ogg"
    escuchado, resultado = await asyncio.to_thread(
        _libreta(context).registrar_audio, contenido, nombre,
        tienda_id=tienda_id, id_mensaje=f"tg-{mensaje.message_id}",
    )
    if not escuchado:
        await mensaje.reply_text("No alcancé a escucharte. ¿Me mandas la nota otra vez?")
        return
    await _responder(update, f"🎤 Escuché: {escuchado}")
    await _responder_resultado(update, context, tienda_id, resultado)


async def inicio(update: Update, context):
    _tienda(update, context)
    await update.effective_message.reply_text(
        "¡Hola, casera! Soy tu libreta 📒 Mándame texto o una nota de voz.\n"
        "Por ejemplo: ‘Vendí 3 leches a 4 soles’ o ‘Le fié 2 panes a Rosa por 1 sol’.\n"
        "/flujo · /cuota · /resumen · /deudas\n/lote para tu CSV · /reiniciar para borrar tu libreta")


async def flujo(update: Update, context):
    tienda_id = _tienda(update, context)
    datos = await asyncio.to_thread(_libreta(context).flujo, tienda_id)
    await update.effective_message.reply_text(_formato_flujo(datos))


async def cuota(update: Update, context):
    datos = await asyncio.to_thread(_libreta(context).cuota, _tienda(update, context))
    await _responder(update, f"🏦 Cuota mensual orientativa: S/ {datos['cuota']:.2f}\n{datos['explicacion']}")


async def resumen(update: Update, context):
    filas = await asyncio.to_thread(_libreta(context).transacciones, _tienda(update, context))
    lineas = ["📒 Movimientos de esta semana"]
    for t in filas:
        monto = "por confirmar" if t.monto is None else f"S/ {t.monto:.2f}"
        lineas.append(f"#{t.id} {t.dia or ''} · {t.tipo} · {t.producto} · {monto}")
    await _responder(update, "\n".join(lineas) if filas else "Aún no hay movimientos esta semana 📒")


async def deudas(update: Update, context):
    filas = await asyncio.to_thread(_libreta(context).deudas, _tienda(update, context))
    lineas = ["📝 Fiados pendientes"]
    for d in filas:
        monto = "monto por confirmar" if d.pendiente is None else f"S/ {d.pendiente:.2f}"
        lineas.append(f"{d.cliente} · {d.producto}: {monto}")
    await _responder(update, "\n".join(lineas) if filas else "¡No tienes fiados pendientes! 🙌")


async def reiniciar(update: Update, context):
    _tienda(update, context)
    await update.effective_message.reply_text(
        "¿Borramos todos los movimientos y fiados de esta libreta? No se puede deshacer.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Sí, borrar", callback_data="reiniciar:si"),
            InlineKeyboardButton("Cancelar", callback_data="reiniciar:no"),
        ]]),
    )


async def boton(update: Update, context):
    tienda_id = _tienda(update, context)
    query = update.callback_query
    await query.answer()
    if query.data.startswith("omitir:"):
        omitido = await asyncio.to_thread(
            _libreta(context).omitir_pendiente, tienda_id, query.data.partition(":")[2],
        )
        if omitido:
            respuesta = "Listo, dejamos ese dato por confirmar 📒"
        else:
            respuesta = "Esa pregunta ya fue atendida 📒"
    elif query.data == "reiniciar:si":
        await asyncio.to_thread(_libreta(context).reiniciar, tienda_id)
        context.chat_data.pop("esperando_csv", None)
        respuesta = "Libreta en blanco. ¡Empezamos de nuevo! 📒"
    else:
        respuesta = "Seguimos con tu libreta 📒"
    await query.edit_message_text(respuesta)


async def lote(update: Update, context):
    _tienda(update, context)
    mensaje = update.effective_message
    documento = mensaje.document
    if documento is None and mensaje.reply_to_message:
        documento = mensaje.reply_to_message.document
    if documento is None:
        context.chat_data["esperando_csv"] = True
        await mensaje.reply_text("Mándame tu CSV con columnas id_mensaje,dia,mensaje 📎")
        return
    await _ejecutar_lote(update, context, documento)


async def documento(update: Update, context):
    _tienda(update, context)
    caption = update.effective_message.caption or ""
    comando = caption.split(maxsplit=1)[0].split("@")[0] if caption else ""
    if context.chat_data.get("esperando_csv") or comando == "/lote":
        await _ejecutar_lote(update, context, update.effective_message.document)
    else:
        await update.effective_message.reply_text("Para procesar un CSV, mándalo con la leyenda /lote 📎")


async def _ejecutar_lote(update, context, documento):
    if not (documento.file_name or "").lower().endswith(".csv"):
        await update.effective_message.reply_text("Necesito un archivo .csv, casera 📎")
        return
    archivo = await documento.get_file()
    contenido = bytes(await archivo.download_as_bytearray())
    salida, filas, datos = await asyncio.to_thread(
        _libreta(context).lote, contenido, prefijo=str(update.effective_chat.id),
    )
    await update.effective_message.reply_document(
        document=BytesIO(salida), filename="entrega.csv",
        caption=f"{len(filas)} filas · {sum(t.requiere_confirmacion for t in filas)} con SI · Flujo: S/ {datos['flujo']:.2f}")
    context.chat_data.pop("esperando_csv", None)


async def resumen_semanal(context):
    registro = _registro(context)
    for chat_id in registro.chats() if registro else []:
        try:
            datos = await asyncio.to_thread(_libreta(context).flujo, f"tg:{chat_id}")
            await context.bot.send_message(chat_id=chat_id, text=_formato_flujo(datos))
        except Exception:
            LOG.exception("No se pudo enviar resumen al chat %s", chat_id)


async def error(update, context):
    LOG.error("Error en Telegram", exc_info=(type(context.error), context.error, context.error.__traceback__))
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("Uy, algo falló, ¿me lo repites?")
        except Exception:
            LOG.exception("No se pudo enviar el aviso de error")


def crear_aplicacion(token, libreta, *, db_path=None) -> Application:
    registro = RegistroTelegram(db_path or os.getenv("DB_PATH", "./libreta.db"))

    async def cerrar(application):
        registro.close()

    app = Application.builder().token(token).post_shutdown(cerrar).build()
    app.bot_data.update(libreta=libreta, registro_telegram=registro)
    for nombre, handler in {"start": inicio, "flujo": flujo, "cuota": cuota, "resumen": resumen,
                            "deudas": deudas, "reiniciar": reiniciar, "lote": lote}.items():
        app.add_handler(CommandHandler(nombre, handler))
    app.add_handler(CallbackQueryHandler(boton, pattern=r"^(omitir:|reiniciar:)"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, audio))
    app.add_handler(MessageHandler(filters.Document.ALL, documento))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto))
    app.add_error_handler(error)
    if app.job_queue is None:
        registro.close()
        raise RuntimeError("Instala python-telegram-bot[job-queue] para el resumen semanal")
    # PTB uses Sunday=0, Monday=1, ... Saturday=6.
    app.job_queue.run_daily(resumen_semanal, time=time(20, 0, tzinfo=LIMA), days=(0,), name="resumen-domingo")
    return app
