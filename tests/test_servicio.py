from datetime import datetime

import pytest

from core import servicio
from core.memoria import Memoria
from core.modelos import Accion


@pytest.fixture
def libreta(monkeypatch):
    monkeypatch.setattr(servicio, "ahora", lambda: datetime(2026, 10, 17, 12, tzinfo=servicio.LIMA))
    vistos = []

    def interpretar(texto, contexto):
        vistos.append(texto)
        if texto.startswith("Respuesta a la pregunta"):
            ref = contexto.ultimas[-1].id
            return [Accion("corregir", ref_transaccion=ref, campo="monto", nuevo_valor=16.5)]
        if "atunes" in texto:
            return [Accion("preguntar", tipo="venta", producto="atún", cantidad=3, pregunta="¿A cuánto?")]
        if "fié" in texto:
            return [Accion("registrar", tipo="fiado", producto="aceite", cantidad=1, monto=11, cliente="Rosa")]
        return [Accion("registrar", tipo="venta", producto="pan", cantidad=10, precio_unitario=0.3)]

    lib = servicio.Libreta(Memoria(), interpretar=interpretar)
    lib.vistos = vistos
    yield lib
    lib.close()


def test_registrar_y_flujo(libreta):
    r = libreta.registrar("vendí 10 panes a 0.30", tienda_id="t")
    assert [t.monto for t in r.transacciones] == [3.0]
    flujo = libreta.flujo("t")
    assert (flujo["semana"], flujo["flujo"]) == ("2026-10-12", 3.0)
    assert libreta.cuota("t")["semanas"] == [{"semana": "2026-10-12", "flujo": 3.0}]


def test_respuesta_a_pendiente_corrige_y_limpia(libreta):
    libreta.registrar("vendí 3 atunes", tienda_id="t")
    assert libreta.pendiente("t")["pregunta"] == "¿A cuánto?"
    libreta.registrar("a 5.50 cada uno", tienda_id="t")
    assert libreta.vistos[-1].startswith("Respuesta a la pregunta '¿A cuánto?'")
    assert libreta.pendiente("t") is None


def test_omitir_pendiente(libreta):
    libreta.registrar("vendí 3 atunes", tienda_id="t")
    ref = libreta.pendiente("t")["transaccion_id"]
    assert not libreta.omitir_pendiente("t", ref + 1)
    assert libreta.omitir_pendiente("t", ref)
    assert libreta.pendiente("t") is None
    assert libreta.transacciones("t")[0].requiere_confirmacion


def test_deudas_vocabulario_y_reiniciar(libreta):
    libreta.registrar("le fié aceite a Rosa, 11", tienda_id="t")
    assert [d.cliente for d in libreta.deudas("t")] == ["Rosa"]
    assert libreta.vocabulario("t") == ["Rosa", "aceite"]
    libreta.reiniciar("t")
    assert libreta.transacciones("t", "all") == [] and libreta.deudas("t") == []


def test_lote_usa_tienda_nueva(libreta):
    csv_in = "id_mensaje,dia,mensaje\nX1,lunes,vendí 10 panes a 0.30\n".encode("utf-8-sig")
    salida, filas, flujo = libreta.lote(csv_in, prefijo="web")
    assert salida.decode("utf-8-sig").splitlines()[1] == "X1,venta,pan,10,3.00,NO"
    assert flujo["flujo"] == 3.0
    assert libreta.transacciones("t", "all") == []


def test_legacy_path_ignored_and_week_persists(tmp_path):
    path = tmp_path / "ledger.db"
    memoria = Memoria(path)
    lib = servicio.Libreta(memoria, str(tmp_path / "ignored.db"))
    lib._fechar("t", "m")
    assert not (tmp_path / "ignored.db").exists()
    lib.close()
    memoria = Memoria(path)
    assert memoria.semanas_mensaje("t") == {"m": servicio.semana_actual()}
    memoria.borrar_tienda("t")
    assert memoria.semanas_mensaje("t") == {}
    memoria.close()
