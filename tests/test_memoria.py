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
    other.close()
    m.close()




from tests.memoria_contract import ComportamientoMemoria


class TestMemoriaSQLite(ComportamientoMemoria):
    @pytest.fixture(params=[True, False], ids=['fts', 'like'])
    def m(self, request, tiendas):
        memoria = Memoria()
        if request.param and not memoria._fts:
            memoria.close()
            pytest.skip('SQLite sin FTS5')
        memoria._fts = request.param
        yield memoria
        memoria.close()


def test_log_serialization_and_delete():
    m = Memoria()
    t = m.agregar(transaction())
    m.log('a', 'm', 'texto', '[]', Resultado(transacciones=[t]))
    assert json.loads(m._db.execute('SELECT resultado FROM log').fetchone()[0])['transacciones'][0]['id'] == t.id
    m.borrar_tienda('a')
    assert m._db.execute('SELECT count(*) FROM log').fetchone()[0] == 0
    m.close()


def test_old_telegram_weeks_migrate_once(tmp_path):
    import sqlite3
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE telegram_semanas (tienda_id TEXT, id_mensaje TEXT, semana TEXT)')
        db.execute("INSERT INTO telegram_semanas VALUES ('a', 'm', '2026-10-05')")
    m = Memoria(path)
    assert m.semanas_mensaje('a') == {'m': '2026-10-05'}
    m.borrar_tienda('a')
    m.close()
    m = Memoria(path)
    assert m.semanas_mensaje('a') == {}
    m.close()


@pytest.mark.parametrize('url', ['postgres://example', 'postgresql://example'])
def test_factory_postgres_dispatch(url, monkeypatch):
    import core.memoria_pg
    from core.memoria import abrir_memoria
    calls = []
    marker = object()
    monkeypatch.setattr(core.memoria_pg, 'MemoriaPostgres', lambda dsn: calls.append(dsn) or marker)
    assert abrir_memoria(url) is marker
    monkeypatch.setenv('DATABASE_URL', url)
    assert abrir_memoria() is marker
    assert calls == [url, url]
    # Explicit in-memory requests always beat production DATABASE_URL.
    m = abrir_memoria(':memory:')
    assert isinstance(m, Memoria)
    m.close()


def test_factory_sqlite_paths(tmp_path, monkeypatch):
    from core.memoria import abrir_memoria
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'fallback.db'))
    m = abrir_memoria()
    m.close()
    assert (tmp_path / 'fallback.db').exists()
    monkeypatch.delenv('DB_PATH')
    monkeypatch.chdir(tmp_path)
    m = abrir_memoria()
    m.close()
    assert (tmp_path / 'libreta.db').exists()


def test_abrir_memoria_rechaza_url_no_postgres():
    import pytest
    from core.memoria import abrir_memoria

    with pytest.raises(ValueError, match="Session pooler"):
        abrir_memoria("https://abc.supabase.co")
