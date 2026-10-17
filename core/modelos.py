"""Shared data contracts. Every module imports from here; change only by agreement."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

TIPOS = ("venta", "gasto", "fiado", "cobro_fiado")
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

NombreAccion = Literal["registrar", "corregir", "cobrar_fiado", "preguntar", "ignorar"]


@dataclass
class Accion:
    """One step the LLM proposes. Python validates it before anything is stored.

    registrar     tipo, producto, cantidad, and either precio_unitario or monto (total);
                  optional descuento (absolute soles off the total) and cliente (for fiado).
                  Leave the amount fields None when the message does not state them.
    corregir      ref_transaccion (id of an earlier Transaccion) + campo + nuevo_valor.
    cobrar_fiado  cliente, and monto if stated; total=True when the customer paid all they owe.
                  producto optional (what the debt was for).
    preguntar     a field is missing; producto/tipo/cantidad filled when known, pregunta is the
                  Spanish question for Doña Rosa. Produces a row with requiere_confirmacion=SI.
    ignorar       not a business transaction (e.g. personal purchase); motivo explains why.
    """

    accion: NombreAccion
    tipo: str | None = None
    producto: str | None = None
    cantidad: float | None = None
    precio_unitario: float | None = None
    monto: float | None = None
    descuento: float | None = None
    cliente: str | None = None
    total: bool = False
    ref_transaccion: int | None = None
    campo: str | None = None
    nuevo_valor: str | float | None = None
    pregunta: str | None = None
    motivo: str | None = None


@dataclass
class Transaccion:
    tienda_id: str
    id_mensaje: str
    tipo: str
    producto: str
    cantidad: float = 1
    monto: float | None = None
    requiere_confirmacion: bool = False
    dia: str | None = None
    cliente: str | None = None
    pregunta: str | None = None
    id: int | None = None

    def fila_csv(self) -> dict[str, str]:
        cantidad = int(self.cantidad) if float(self.cantidad).is_integer() else self.cantidad
        return {
            "id_mensaje": self.id_mensaje,
            "tipo": self.tipo,
            "producto": self.producto,
            "cantidad": str(cantidad),
            "monto": "" if self.monto is None else f"{self.monto:.2f}",
            "requiere_confirmacion": "SI" if self.requiere_confirmacion else "NO",
        }


@dataclass
class Deuda:
    """Open credit (fiado) still owed. pendiente is None when the fiado had no amount."""

    transaccion_id: int
    cliente: str
    producto: str
    monto_original: float | None
    pendiente: float | None


@dataclass
class Contexto:
    """What the LLM gets to see besides the message itself."""

    dia: str | None = None
    deudas_abiertas: list[Deuda] = field(default_factory=list)
    ultimas: list[Transaccion] = field(default_factory=list)
    alias: dict[str, str] = field(default_factory=dict)


@dataclass
class Resultado:
    """Outcome of processing one message."""

    transacciones: list[Transaccion] = field(default_factory=list)
    corregidas: list[Transaccion] = field(default_factory=list)
    preguntas: list[str] = field(default_factory=list)
    rechazos: list[str] = field(default_factory=list)
    ignorados: list[str] = field(default_factory=list)
    respuesta: str = ""


CSV_COLUMNAS = ["id_mensaje", "tipo", "producto", "cantidad", "monto", "requiere_confirmacion"]

__all__ = [
    "TIPOS", "DIAS", "Accion", "Transaccion", "Deuda", "Contexto", "Resultado", "CSV_COLUMNAS", "asdict",
]
