from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from core.memoria import Memoria
from core.modelos import Resultado, Transaccion


def transaction(store='a', **kwargs):
    return Transaccion(tienda_id=store, id_mensaje='m', tipo=kwargs.pop('tipo','venta'), producto=kwargs.pop('producto','pan'), **kwargs)


def test_persistence_wal_and_context(tmp_path):
    path = tmp_path / 'ledger.db'
    m = Memoria(path)
    assert m._db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    for i in range(12):
        m.agregar(transaction(monto=i))
    m.guardar_alias('a',' GUEVOS ', 'Huevos')
    m.fijar_pendiente('a', {'transaccion_id':12,'pregunta':'¿Cuánto?'})
    other = Memoria(path)
    context = other.contexto('a','lunes')
    assert context.dia == 'lunes'
    assert [t.id for t in context.ultimas] == list(range(3,13))
    assert context.alias == {'guevos':'huevos'}
    assert other.pendiente('a')['transaccion_id'] == 12
    assert other.transacciones('b') == []
    assert other.ultimas('a',0) == []
    other.fijar_pendiente('a',None)
    assert other.pendiente('a') is None


def test_debt_lifecycle_correction_and_isolation():
    m = Memoria()
    t = m.agregar(transaction(tipo='fiado',monto=20,cliente='Rosa',cantidad=2))
    m.registrar_cobro('a',t.id,5)
    assert m.deudas_abiertas('a')[0].pendiente == 15
    m.actualizar(t.id,monto=25,producto='arroz')
    debt = m.deudas_abiertas('a')[0]
    assert (debt.monto_original,debt.pendiente,debt.producto) == (25,20,'arroz')
    with pytest.raises(ValueError):
        m.registrar_cobro('b',t.id,5)
    with pytest.raises(ValueError):
        m.registrar_cobro('a',t.id,-1)
    m.registrar_cobro('a',t.id,None)
    assert m.deudas_abiertas('a') == []
    t2 = m.agregar(transaction(tipo='fiado',cliente='Pepe'))
    m.registrar_cobro('a',t2.id,10)
    assert m.deudas_abiertas('a')[0].pendiente is None
    m.actualizar(t2.id,monto=30)
    assert m.deudas_abiertas('a')[0].pendiente == 30
    m.actualizar(t2.id,tipo='venta')
    assert not m.deudas_abiertas('a')
    m.actualizar(t2.id,tipo='fiado')
    assert m.deudas_abiertas('a')[0].pendiente == 30


@pytest.mark.parametrize('fts',[True,False])
def test_search_and_delete(fts):
    m = Memoria()
    if fts and not m._fts:
        pytest.skip('SQLite sin FTS5')
    m._fts = fts
    t = m.agregar(transaction(tipo='fiado', producto='aceite primor',cliente='Rosa',monto=11))
    other = m.agregar(transaction('b',producto='aceite primor'))
    assert [r.id for r in m.buscar('a','aceite')] == [t.id]
    assert [r.id for r in m.buscar('a','Rosa')] == [t.id]
    assert m.buscar('a','" *') == []
    m.actualizar(t.id,producto='leche')
    assert m.buscar('a','aceite') == []
    m.guardar_alias('a','lech','leche')
    m.fijar_pendiente('a', {'pregunta':'¿Monto?'})
    m.log('a','m','texto','[]',Resultado(transacciones=[t]))
    assert json.loads(m._db.execute('SELECT resultado FROM log').fetchone()[0])['transacciones'][0]['id'] == t.id
    m.borrar_tienda('a')
    assert not m.transacciones('a') and not m.deudas_abiertas('a')
    assert not m.buscar('a','leche') and not m.alias('a') and m.pendiente('a') is None
    assert m._db.execute('SELECT count(*) FROM log').fetchone()[0] == 0
    assert m.obtener(other.id) is not None


def test_threads_and_invalid_updates():
    m = Memoria()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: m.agregar(transaction(monto=i)), range(80)))
    assert len(m.transacciones('a')) == 80
    assert len({t.id for t in m.transacciones('a')}) == 80
    with pytest.raises(ValueError):
        m.actualizar(999,monto=1)
    with pytest.raises(ValueError):
        m.actualizar(1,tienda_id='b')
    assert m.obtener(999) is None


def test_created_metadata_survives_roundtrip():
    m = Memoria()
    t = m.agregar(transaction())
    assert t.created_at
    assert m.obtener(t.id).created_at == t.created_at
    assert m.transacciones('a')[0].created_at == t.created_at
