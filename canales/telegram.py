"""Canal Telegram: long polling, audio y reportes semanales en hora de Lima."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from io import BytesIO
import logging
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from core.modelos import DIAS
from core.voz import transcribir

LOG = logging.getLogger(__name__)
LIMA = ZoneInfo("America/Lima")


# Lazy imports allow the channel to load independently of the other implementations.
def procesar_mensaje(*args, **kwargs):
    from core.procesar import procesar_mensaje as procesar
    return procesar(*args, **kwargs)


def flujo_semanal(transacciones):
    from core.finanzas import flujo_semanal as calcular
    return calcular(transacciones)


def cuota_sostenible(flujos):
    from core.finanzas import cuota_sostenible as calcular
    return calcular(flujos)


def procesar_csv(*args, **kwargs):
    from canales.lote import procesar_csv as procesar
    return procesar(*args, **kwargs)


def ahora():
    return datetime.now(LIMA)


def semana_actual():
    fecha = ahora().date()
    return (fecha - timedelta(days=fecha.weekday())).isoformat()


class RegistroTelegram:
    """Fechas de mensajes y chats, aparte de las tablas de negocio de Memoria."""

    def __init__(self, path):
        self.path = path
        # A persistent connection also supports :memory: in tests. Serialize access
        # with short synchronous queries in the event loop; no network work here.
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS telegram_chats (chat_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS telegram_semanas (
                tienda_id TEXT, id_mensaje TEXT, semana TEXT NOT NULL,
                PRIMARY KEY (tienda_id, id_mensaje)
            );
        """)
        self.db.commit()

    def recordar(self, chat_id):
        self.db.execute("INSERT OR IGNORE INTO telegram_chats VALUES (?)", (chat_id,))
        self.db.commit()

    def fechar(self, tienda_id, id_mensaje):
        self.db.execute("INSERT OR IGNORE INTO telegram_semanas VALUES (?, ?, ?)",
                        (tienda_id, id_mensaje, semana_actual()))
        self.db.commit()

    def semanas(self, tienda_id):
        return dict(self.db.execute("SELECT id_mensaje, semana FROM telegram_semanas WHERE tienda_id=?", (tienda_id,)))

    def chats(self):
        return [fila[0] for fila in self.db.execute("SELECT chat_id FROM telegram_chats")]

    def borrar(self, tienda_id):
        self.db.execute("DELETE FROM telegram_semanas WHERE tienda_id=?", (tienda_id,))
        self.db.commit()

    def close(self):
        self.db.close()


def _memoria(context):
    return context.application.bot_data["memoria"]


def _registro(context):
    return context.application.bot_data.get("registro_telegram")


def _tienda(update, context):
    chat_id = update.effective_chat.id
    registro = _registro(context)
    if registro:
        registro.recordar(chat_id)
    return f"tg:{chat_id}"


def _grupos(context, tienda_id, transacciones):
    registro = _registro(context)
    fechas = registro.semanas(tienda_id) if registro else {}
    grupos = {}
    for t in transacciones:
        # Legacy/imported records have no dates in the frozen domain contract.
        semana = fechas.get(t.id_mensaje, semana_actual())
        grupos.setdefault(semana, []).append(t)
    return grupos


async def _semanales(context, tienda_id):
    transacciones = await asyncio.to_thread(_memoria(context).transacciones, tienda_id)
    return _grupos(context, tienda_id, transacciones)


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


async def _procesar(update, context, texto):
    tienda_id = _tienda(update, context)
    memoria = _memoria(context)
    pendiente = await asyncio.to_thread(memoria.pendiente, tienda_id)
    if pendiente:
        pregunta = pendiente.get("pregunta", "¿Cuál es el monto?")
        ref = pendiente.get("transaccion_id", pendiente.get("id", pendiente.get("ref_transaccion")))
        texto = f"Respuesta a la pregunta '{pregunta}' sobre la transacción #{ref}: {texto}"
    registro = _registro(context)
    if registro:
        registro.fechar(tienda_id, f"tg-{update.effective_message.message_id}")
    # Clear only once processing succeeded, and preserve any new follow-up it emits.
    resultado = await asyncio.to_thread(
        procesar_mensaje, texto, tienda_id=tienda_id,
        id_mensaje=f"tg-{update.effective_message.message_id}",
        dia=DIAS[ahora().weekday()], memoria=memoria,
    )
    actual = await asyncio.to_thread(memoria.pendiente, tienda_id)
    if pendiente and actual == pendiente and not resultado.preguntas:
        await asyncio.to_thread(memoria.fijar_pendiente, tienda_id, None)
        actual = None
    botones = None
    if actual:
        ref = actual.get("transaccion_id", actual.get("id", actual.get("ref_transaccion", "")))
        botones = InlineKeyboardMarkup([[InlineKeyboardButton("Omitir", callback_data=f"omitir:{ref}")]])
    await _responder(update, resultado.respuesta or "Listo, casera 📒", reply_markup=botones)


async def texto(update: Update, context):
    await _procesar(update, context, update.effective_message.text)


async def audio(update: Update, context):
    tienda_id = _tienda(update, context)
    mensaje = update.effective_message
    nota = mensaje.voice or mensaje.audio
    archivo = await nota.get_file()
    contenido = bytes(await archivo.download_as_bytearray())
    contexto = await asyncio.to_thread(_memoria(context).contexto, tienda_id)
    vocabulario = [d.cliente for d in contexto.deudas_abiertas]
    vocabulario.extend(t.producto for t in contexto.ultimas)
    nombre = getattr(nota, "file_name", None) or "nota.ogg"
    escuchado = await asyncio.to_thread(transcribir, contenido, nombre, vocabulario)
    if not escuchado:
        await mensaje.reply_text("No alcancé a escucharte. ¿Me mandas la nota otra vez?")
        return
    await _responder(update, f"🎤 Escuché: {escuchado}")
    await _procesar(update, context, escuchado)


async def inicio(update: Update, context):
    _tienda(update, context)
    await update.effective_message.reply_text(
        "¡Hola, casera! Soy tu libreta 📒 Mándame texto o una nota de voz.\n"
        "Por ejemplo: ‘Vendí 3 leches a 4 soles’ o ‘Le fié 2 panes a Rosa por 1 sol’.\n"
        "/flujo · /cuota · /resumen · /deudas\n/lote para tu CSV · /reiniciar para borrar tu libreta")


async def flujo(update: Update, context):
    tienda_id = _tienda(update, context)
    grupos = await _semanales(context, tienda_id)
    datos = await asyncio.to_thread(flujo_semanal, grupos.get(semana_actual(), []))
    await update.effective_message.reply_text(_formato_flujo(datos))


async def cuota(update: Update, context):
    grupos = await _semanales(context, _tienda(update, context))
    # Include empty weeks between the first recorded week and today.
    flujos = []
    fecha = datetime.fromisoformat(min(grupos, default=semana_actual())).date()
    fin = datetime.fromisoformat(semana_actual()).date()
    while fecha <= fin:
        datos = await asyncio.to_thread(flujo_semanal, grupos.get(fecha.isoformat(), []))
        flujos.append(datos["flujo"])
        fecha += timedelta(days=7)
    datos = await asyncio.to_thread(cuota_sostenible, flujos)
    await _responder(update, f"🏦 Cuota mensual orientativa: S/ {datos['cuota']:.2f}\n{datos['explicacion']}")


async def resumen(update: Update, context):
    grupos = await _semanales(context, _tienda(update, context))
    filas = grupos.get(semana_actual(), [])
    lineas = ["📒 Movimientos de esta semana"]
    for t in filas:
        monto = "por confirmar" if t.monto is None else f"S/ {t.monto:.2f}"
        lineas.append(f"#{t.id} {t.dia or ''} · {t.tipo} · {t.producto} · {monto}")
    await _responder(update, "\n".join(lineas) if filas else "Aún no hay movimientos esta semana 📒")


async def deudas(update: Update, context):
    filas = await asyncio.to_thread(_memoria(context).deudas_abiertas, _tienda(update, context))
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
        pendiente = await asyncio.to_thread(_memoria(context).pendiente, tienda_id)
        ref = (pendiente or {}).get("transaccion_id", (pendiente or {}).get("id", (pendiente or {}).get("ref_transaccion", "")))
        if pendiente and str(ref) == query.data.partition(":")[2]:
            await asyncio.to_thread(_memoria(context).fijar_pendiente, tienda_id, None)
            respuesta = "Listo, dejamos ese dato por confirmar 📒"
        else:
            respuesta = "Esa pregunta ya fue atendida 📒"
    elif query.data == "reiniciar:si":
        await asyncio.to_thread(_memoria(context).borrar_tienda, tienda_id)
        if _registro(context):
            _registro(context).borrar(tienda_id)
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
    tienda_id = f"lote:{update.effective_chat.id}:{ahora().strftime('%Y%m%d%H%M%S%f')}-{uuid4().hex[:8]}"
    with TemporaryDirectory(prefix="libreta-lote-") as temporal:
        entrada, salida = Path(temporal) / "entrada.csv", Path(temporal) / "entrega.csv"
        entrada.write_bytes(contenido)
        filas = await asyncio.to_thread(procesar_csv, str(entrada), str(salida), memoria=_memoria(context), tienda_id=tienda_id)
        datos = await asyncio.to_thread(flujo_semanal, filas)
        await update.effective_message.reply_document(
            document=BytesIO(salida.read_bytes()), filename="entrega.csv",
            caption=f"{len(filas)} filas · {sum(t.requiere_confirmacion for t in filas)} con SI · Flujo: S/ {datos['flujo']:.2f}")
    context.chat_data.pop("esperando_csv", None)


async def resumen_semanal(context):
    registro = _registro(context)
    for chat_id in registro.chats() if registro else []:
        try:
            grupos = await _semanales(context, f"tg:{chat_id}")
            datos = await asyncio.to_thread(flujo_semanal, grupos.get(semana_actual(), []))
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


def crear_aplicacion(token, memoria, *, db_path=None):
    registro = RegistroTelegram(db_path or os.getenv("DB_PATH", "./libreta.db"))

    async def cerrar(application):
        registro.close()

    app = Application.builder().token(token).post_shutdown(cerrar).build()
    app.bot_data.update(memoria=memoria, registro_telegram=registro)
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
