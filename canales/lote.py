"""Batch channel: messages CSV (id_mensaje,dia,mensaje) → submission CSV.

    .venv/bin/python -m canales.lote entrada.csv salida.csv [--db lote.db] [--tienda lote]

Messages are processed in order (debts and corrections depend on earlier ones). The output is
rewritten from the database after every message, so a crash mid-run still leaves a valid CSV.
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from pathlib import Path

from core.modelos import CSV_COLUMNAS, Transaccion
from core.procesar import Interprete, procesar_mensaje

log = logging.getLogger(__name__)


def leer_mensajes(path: str | Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    return [
        {
            "id_mensaje": (fila.get("id_mensaje") or "").strip(),
            "dia": (fila.get("dia") or "").strip().lower() or None,
            "mensaje": (fila.get("mensaje") or "").strip(),
        }
        for fila in filas
        if (fila.get("id_mensaje") or "").strip()
    ]


def escribir_csv(transacciones: list[Transaccion], path: str | Path) -> None:
    tmp = Path(f"{path}.tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNAS)
        w.writeheader()
        for t in transacciones:
            w.writerow(t.fila_csv())
    tmp.replace(path)


def procesar_csv(
    entrada_path: str | Path,
    salida_path: str | Path,
    *,
    memoria,
    tienda_id: str,
    interpretar: Interprete | None = None,
) -> list[Transaccion]:
    mensajes = leer_mensajes(entrada_path)
    escribir_csv([], salida_path)
    for m in mensajes:
        inicio = time.monotonic()
        try:
            resultado = procesar_mensaje(
                m["mensaje"],
                tienda_id=tienda_id,
                id_mensaje=m["id_mensaje"],
                dia=m["dia"],
                memoria=memoria,
                interpretar=interpretar,
            )
            log.info("%s (%.1fs) %s", m["id_mensaje"], time.monotonic() - inicio,
                     resultado.respuesta.replace("\n", " | "))
        except Exception:
            # No guessed row: a wrong row costs more than a missing one.
            log.exception("%s falló; se omite", m["id_mensaje"])
        escribir_csv(memoria.transacciones(tienda_id), salida_path)
    return memoria.transacciones(tienda_id)


def main(argv: list[str] | None = None) -> None:
    from core.config import cargar_env

    cargar_env()
    from core.memoria import Memoria

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("entrada")
    p.add_argument("salida")
    p.add_argument("--db", default=":memory:", help="SQLite path (default: in memory)")
    p.add_argument("--tienda", default=f"lote:{int(time.time())}")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    inicio = time.monotonic()
    filas = procesar_csv(args.entrada, args.salida, memoria=Memoria(args.db), tienda_id=args.tienda)
    print(f"{len(filas)} filas → {args.salida} en {time.monotonic() - inicio:.1f}s")


if __name__ == "__main__":
    main()
