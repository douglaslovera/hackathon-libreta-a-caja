"""Channel-agnostic application service: everything Telegram and the HTTP API both need.

Channels only translate their transport (Telegram updates, HTTP requests) into calls here.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

from core.modelos import DIAS, Deuda, Resultado, Transaccion

LIMA = ZoneInfo("America/Lima")


def ahora() -> datetime:
    return datetime.now(LIMA)


def semana_actual() -> str:
    """ISO date of this week's Monday (Lima time)."""
    hoy = ahora().date()
    return (hoy - timedelta(days=hoy.weekday())).isoformat()


def dia_actual() -> str:
    return DIAS[ahora().weekday()]


def _ref(pendiente: dict | None):
    if not pendiente:
        return None
    return pendiente.get("transaccion_id", pendiente.get("id", pendiente.get("ref_transaccion")))


class Libreta:
    """One shop's notebook operations on top of Memoria, plus week dating of messages."""

    def __init__(self, memoria, db_path: str | None = None, *, interpretar=None):
        self.memoria = memoria
        self.interpretar = interpretar
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS semanas_mensaje (
                   tienda_id TEXT, id_mensaje TEXT, semana TEXT NOT NULL,
                   PRIMARY KEY (tienda_id, id_mensaje))"""
        )
        self._db.commit()

    # ---- recording -------------------------------------------------------------------------

    def registrar(self, texto: str, *, tienda_id: str, id_mensaje: str | None = None,
                  dia: str | None = None) -> Resultado:
        """Process one message. If a follow-up question is open, the text is treated as its answer."""
        from core.procesar import procesar_mensaje

        id_mensaje = id_mensaje or f"m-{uuid4().hex[:12]}"
        pendiente = self.memoria.pendiente(tienda_id)
        if pendiente:
            pregunta = pendiente.get("pregunta", "¿Cuál es el monto?")
            texto = f"Respuesta a la pregunta '{pregunta}' sobre la transacción #{_ref(pendiente)}: {texto}"
        self._fechar(tienda_id, id_mensaje)
        resultado = procesar_mensaje(
            texto, tienda_id=tienda_id, id_mensaje=id_mensaje, dia=dia or dia_actual(),
            memoria=self.memoria, interpretar=self.interpretar,
        )
        # The answer was consumed unless processing opened a new question.
        if pendiente and self.memoria.pendiente(tienda_id) == pendiente and not resultado.preguntas:
            self.memoria.fijar_pendiente(tienda_id, None)
        return resultado

    def vocabulario(self, tienda_id: str) -> list[str]:
        contexto = self.memoria.contexto(tienda_id)
        palabras = [d.cliente for d in contexto.deudas_abiertas]
        palabras += [t.producto for t in contexto.ultimas]
        return list(dict.fromkeys(p for p in palabras if p))

    def transcribir(self, audio: bytes, nombre_archivo: str, *, tienda_id: str) -> str:
        from core.voz import transcribir

        return transcribir(audio, nombre_archivo, self.vocabulario(tienda_id)).strip()

    def registrar_audio(self, audio: bytes, nombre_archivo: str, *, tienda_id: str,
                        id_mensaje: str | None = None) -> tuple[str, Resultado | None]:
        """Transcribe then record. Returns ("", None) when nothing intelligible was heard."""
        texto = self.transcribir(audio, nombre_archivo, tienda_id=tienda_id)
        if not texto:
            return "", None
        return texto, self.registrar(texto, tienda_id=tienda_id, id_mensaje=id_mensaje)

    # ---- follow-up questions ---------------------------------------------------------------

    def pendiente(self, tienda_id: str) -> dict | None:
        return self.memoria.pendiente(tienda_id)

    def omitir_pendiente(self, tienda_id: str, transaccion_id=None) -> bool:
        """Leave the open question unanswered (row stays requiere_confirmacion=SI)."""
        pendiente = self.memoria.pendiente(tienda_id)
        if not pendiente or (transaccion_id is not None and str(_ref(pendiente)) != str(transaccion_id)):
            return False
        self.memoria.fijar_pendiente(tienda_id, None)
        return True

    # ---- reports ---------------------------------------------------------------------------

    def semanas(self, tienda_id: str) -> dict[str, list[Transaccion]]:
        """Transactions grouped by week (Monday ISO date). Undated rows count as this week."""
        with self._lock:
            fechas = dict(self._db.execute(
                "SELECT id_mensaje, semana FROM semanas_mensaje WHERE tienda_id=?", (tienda_id,)))
        grupos: dict[str, list[Transaccion]] = {}
        for t in self.memoria.transacciones(tienda_id):
            grupos.setdefault(fechas.get(t.id_mensaje, semana_actual()), []).append(t)
        return grupos

    def transacciones(self, tienda_id: str, semana: str | None = None) -> list[Transaccion]:
        if semana == "all":
            return self.memoria.transacciones(tienda_id)
        return self.semanas(tienda_id).get(semana or semana_actual(), [])

    def flujo(self, tienda_id: str, semana: str | None = None) -> dict:
        from core.finanzas import flujo_semanal

        return {"semana": semana or semana_actual(),
                **flujo_semanal(self.transacciones(tienda_id, semana))}

    def flujos_semanales(self, tienda_id: str) -> list[tuple[str, float]]:
        """Weekly net flow from the first recorded week to this one, empty weeks included."""
        from core.finanzas import flujo_semanal

        grupos = self.semanas(tienda_id)
        fecha = date.fromisoformat(min(grupos, default=semana_actual()))
        fin = date.fromisoformat(semana_actual())
        salida = []
        while fecha <= fin:
            salida.append((fecha.isoformat(), flujo_semanal(grupos.get(fecha.isoformat(), []))["flujo"]))
            fecha += timedelta(days=7)
        return salida

    def cuota(self, tienda_id: str) -> dict:
        from core.finanzas import cuota_sostenible

        semanas = self.flujos_semanales(tienda_id)
        return {**cuota_sostenible([f for _, f in semanas]),
                "semanas": [{"semana": s, "flujo": f} for s, f in semanas]}

    def deudas(self, tienda_id: str) -> list[Deuda]:
        return self.memoria.deudas_abiertas(tienda_id)

    # ---- admin -----------------------------------------------------------------------------

    def reiniciar(self, tienda_id: str) -> None:
        self.memoria.borrar_tienda(tienda_id)
        with self._lock:
            self._db.execute("DELETE FROM semanas_mensaje WHERE tienda_id=?", (tienda_id,))
            self._db.commit()

    def lote(self, contenido_csv: bytes, *, prefijo: str) -> tuple[bytes, list[Transaccion], dict]:
        """Run a messages CSV in a fresh shop; returns (submission CSV bytes, rows, flow)."""
        from canales.lote import procesar_csv
        from core.finanzas import flujo_semanal

        tienda_id = f"lote:{prefijo}:{ahora():%Y%m%d%H%M%S}-{uuid4().hex[:8]}"
        with TemporaryDirectory(prefix="libreta-lote-") as tmp:
            entrada, salida = Path(tmp) / "entrada.csv", Path(tmp) / "entrega.csv"
            entrada.write_bytes(contenido_csv)
            filas = procesar_csv(entrada, salida, memoria=self.memoria, tienda_id=tienda_id,
                                 interpretar=self.interpretar)
            return salida.read_bytes(), filas, flujo_semanal(filas)

    def _fechar(self, tienda_id: str, id_mensaje: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO semanas_mensaje VALUES (?, ?, ?)",
                             (tienda_id, id_mensaje, semana_actual()))
            self._db.commit()

    def close(self) -> None:
        self._db.close()
