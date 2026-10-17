import csv

from canales.lote import procesar_csv
from core.memoria import Memoria
from core.modelos import Accion

MENSAJES = [
    ("E01", "lunes", "vendí 10 panes a 0.30 y 3 leches gloria a 4.20"),
    ("E03", "lunes", "la señora Rosa María se llevó 1 aceite primor fiado, está 11 soles"),
    ("E09", "martes", "vendí 3 atunes"),
    ("E12", "miércoles", "la señora Rosa María me pagó lo del aceite"),
    ("E26", "sábado", "compré 2 paquetes de fideos a 2.80 para la casa, no es del negocio"),
    ("E99", "sábado", "esto revienta"),
]

ACCIONES = {
    "E01": [
        Accion("registrar", tipo="venta", producto="pan", cantidad=10, precio_unitario=0.30),
        Accion("registrar", tipo="venta", producto="leche gloria", cantidad=3, precio_unitario=4.20),
    ],
    "E03": [Accion("registrar", tipo="fiado", producto="aceite primor", cantidad=1, monto=11, cliente="Rosa María")],
    "E09": [Accion("preguntar", tipo="venta", producto="atún", cantidad=3, pregunta="¿A cuánto?")],
    "E12": [Accion("cobrar_fiado", cliente="Rosa María", producto="aceite primor", total=True)],
    "E26": [Accion("ignorar", motivo="compra personal")],
}


def _fake(texto, contexto):
    for id_m, _, msg in MENSAJES:
        if msg == texto:
            if id_m == "E99":
                raise RuntimeError("LLM caído")
            return ACCIONES[id_m]
    raise AssertionError(texto)


def test_procesar_csv(tmp_path):
    entrada, salida = tmp_path / "in.csv", tmp_path / "out.csv"
    with open(entrada, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id_mensaje", "dia", "mensaje"])
        w.writerows(MENSAJES)

    procesar_csv(entrada, salida, memoria=Memoria(), tienda_id="t", interpretar=_fake)

    with open(salida, encoding="utf-8-sig", newline="") as f:
        filas = [list(r.values()) for r in csv.DictReader(f)]
    assert filas == [
        ["E01", "venta", "pan", "10", "3.00", "NO"],
        ["E01", "venta", "leche gloria", "3", "12.60", "NO"],
        ["E03", "fiado", "aceite primor", "1", "11.00", "NO"],
        ["E09", "venta", "atún", "3", "", "SI"],
        ["E12", "cobro_fiado", "aceite primor", "1", "11.00", "NO"],
    ]
