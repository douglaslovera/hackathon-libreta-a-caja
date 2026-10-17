"""Score a submission CSV against the expected answers.

    .venv/bin/python -m eval.evaluar                        # run the kit through the pipeline, then score
    .venv/bin/python -m eval.evaluar --salida mi.csv        # only score an existing CSV
    .venv/bin/python -m eval.evaluar --esperado otro.csv    # different answer key

Rows are paired per id_mensaje (best tipo+producto match). The official rubric is not public, so
this reports the things it rewards: exact rows, extra/missing rows, invented amounts, SI detection
and weekly cash flow.
"""

from __future__ import annotations

import argparse
import csv
import time
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / "Kit_Reto_Mibanco"
MENSAJES = KIT / "ejemplos_mensajes.csv"
ESPERADO = KIT / "ejemplos_respuestas.csv"


def leer(path: str | Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "").strip() for k, v in fila.items()} for fila in csv.DictReader(f)]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()
    return " ".join(s.split())


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _monto(v: str) -> float | None:
    try:
        return round(float(v), 2) if v else None
    except ValueError:
        return None


def _cantidad(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def _emparejar(esp: list[dict], obt: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    pares, libres = [], list(obt)
    for e in esp:
        if not libres:
            break
        mejor = max(libres, key=lambda o: (o["tipo"] == e["tipo"]) + _sim(o["producto"], e["producto"]))
        pares.append((e, mejor))
        libres.remove(mejor)
    emparejados = {id(e) for e, _ in pares}
    faltantes = [e for e in esp if id(e) not in emparejados]
    return pares, faltantes, libres


def flujo(filas: list[dict]) -> float:
    total = 0.0
    for f in filas:
        m = _monto(f["monto"]) or 0.0
        if f["tipo"] in ("venta", "cobro_fiado"):
            total += m
        elif f["tipo"] == "gasto":
            total -= m
    return round(total, 2)


def evaluar(esperado: list[dict], obtenido: list[dict], *, detalle: bool = True) -> dict:
    por_id_e, por_id_o = defaultdict(list), defaultdict(list)
    for f in esperado:
        por_id_e[f["id_mensaje"]].append(f)
    for f in obtenido:
        por_id_o[f["id_mensaje"]].append(f)
    ids = sorted(set(por_id_e) | set(por_id_o))

    c = defaultdict(int)
    problemas: list[str] = []
    for i in ids:
        pares, faltantes, extras = _emparejar(por_id_e[i], por_id_o[i])
        c["esperadas"] += len(por_id_e[i])
        c["faltantes"] += len(faltantes)
        c["extras"] += len(extras)
        for f in faltantes:
            problemas.append(f"{i}  FALTA   {f['tipo']} · {f['producto']} · {f['cantidad']} · {f['monto'] or '—'}")
        for f in extras:
            problemas.append(f"{i}  SOBRA   {f['tipo']} · {f['producto']} · {f['cantidad']} · {f['monto'] or '—'}")
        for e, o in pares:
            ok = {
                "tipo": e["tipo"] == o["tipo"],
                "producto": _sim(e["producto"], o["producto"]) >= 0.8,
                "cantidad": _cantidad(e["cantidad"]) == _cantidad(o["cantidad"]),
                "monto": _monto(e["monto"]) == _monto(o["monto"]),
                "confirmacion": e["requiere_confirmacion"].upper() == o["requiere_confirmacion"].upper(),
            }
            for campo, bien in ok.items():
                c[f"ok_{campo}"] += bien
            c["exactas"] += all(ok.values())
            if not e["monto"] and o["monto"]:
                c["inventados"] += 1
            if e["requiere_confirmacion"] == "SI":
                c["si_esperados"] += 1
                c["si_acertados"] += o["requiere_confirmacion"].upper() == "SI"
            if not all(ok.values()):
                malos = ", ".join(k for k, v in ok.items() if not v)
                problemas.append(
                    f"{i}  [{malos}]  esperado {e['tipo']} · {e['producto']} · {e['cantidad']} · {e['monto'] or '—'} · {e['requiere_confirmacion']}"
                    f"  |  obtenido {o['tipo']} · {o['producto']} · {o['cantidad']} · {o['monto'] or '—'} · {o['requiere_confirmacion']}"
                )
        c["si_falsos"] += sum(
            1 for _, o in pares if o["requiere_confirmacion"].upper() == "SI"
        ) - sum(1 for e, o in pares if e["requiere_confirmacion"] == "SI" and o["requiere_confirmacion"].upper() == "SI")

    n = max(c["esperadas"], 1)
    resumen = {
        "filas_exactas": f"{c['exactas']}/{c['esperadas']}",
        "faltantes": c["faltantes"],
        "extras": c["extras"],
        "montos_inventados": c["inventados"],
        "preguntas_acertadas": f"{c['si_acertados']}/{c['si_esperados']}",
        "preguntas_innecesarias": c["si_falsos"],
        **{f"campo_{k}": f"{c[f'ok_{k}'] / n:.0%}" for k in ("tipo", "producto", "cantidad", "monto", "confirmacion")},
        "flujo_esperado": flujo(esperado),
        "flujo_obtenido": flujo(obtenido),
    }
    if detalle:
        for p in problemas:
            print(p)
        if problemas:
            print()
        for k, v in resumen.items():
            print(f"{k:24} {v}")
    return resumen


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--salida", help="CSV to score; if omitted, run the pipeline on --mensajes first")
    p.add_argument("--mensajes", default=str(MENSAJES))
    p.add_argument("--esperado", default=str(ESPERADO))
    args = p.parse_args(argv)

    salida = args.salida
    if salida is None:
        import logging

        from canales.lote import procesar_csv
        from core.memoria import Memoria

        logging.basicConfig(level=logging.INFO, format="%(message)s")
        Path("salidas").mkdir(exist_ok=True)
        salida = f"salidas/eval_{time.strftime('%H%M%S')}.csv"
        inicio = time.monotonic()
        procesar_csv(args.mensajes, salida, memoria=Memoria(), tienda_id="eval")
        print(f"\n{salida} en {time.monotonic() - inicio:.1f}s\n")

    evaluar(leer(args.esperado), leer(salida))


if __name__ == "__main__":
    main()
