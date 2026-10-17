"""Opt-in integration contract: TEST_DATABASE_URL must name a disposable test database."""
import os
from uuid import uuid4

import pytest

from core.modelos import Resultado
from tests.memoria_contract import ComportamientoMemoria, transaction

pytestmark = pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='TEST_DATABASE_URL no configurada')


class TestMemoriaPostgres(ComportamientoMemoria):
    @pytest.fixture
    def m(self, tiendas):
        from core.memoria_pg import MemoriaPostgres
        memoria = MemoriaPostgres(os.environ['TEST_DATABASE_URL'])
        try:
            yield memoria
        finally:
            try:
                for tienda in tiendas:
                    memoria.borrar_tienda(tienda)
            finally:
                memoria.close()

    def test_persistence_and_idempotent_schema(self, m, tiendas):
        from core.memoria_pg import MemoriaPostgres
        a, _ = tiendas
        t = m.agregar(transaction(a, tipo='fiado', monto=11, cliente='Rosa'))
        m.fechar_semana(a, 'm', '2026-10-12')
        other = MemoriaPostgres(os.environ['TEST_DATABASE_URL'])
        try:
            assert other.obtener(t.id).monto == 11.0
            assert other.deudas_abiertas(a)[0].pendiente == 11.0
            assert other.semanas_mensaje(a) == {'m': '2026-10-12'}
        finally:
            other.close()

    def test_chats_log_and_rls(self, m, tiendas):
        a, _ = tiendas
        chat_id = -(uuid4().int % (2**62)) - 1
        try:
            m.recordar_chat(chat_id)
            m.recordar_chat(chat_id)
            assert m.chats().count(chat_id) == 1
            t = m.agregar(transaction(a))
            m.log(a, 'm', 'texto', '[]', Resultado(transacciones=[t]))
            with m._pool.connection() as db:
                result = db.execute('SELECT resultado FROM public.libreta_log WHERE tienda_id=%s', (a,)).fetchone()
                assert result['resultado']['transacciones'][0]['id'] == t.id
                tables = db.execute("SELECT relname,relrowsecurity FROM pg_class JOIN pg_namespace ON pg_namespace.oid=relnamespace WHERE nspname='public' AND relkind='r' AND relname LIKE 'libreta_%'").fetchall()
                assert len(tables) == 7
                assert all(row['relrowsecurity'] for row in tables)
                assert not db.execute("SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename LIKE 'libreta_%'").fetchall()
            m.borrar_tienda(a)
            with m._pool.connection() as db:
                assert db.execute('SELECT count(*) AS n FROM public.libreta_log WHERE tienda_id=%s', (a,)).fetchone()['n'] == 0
        finally:
            with m._pool.connection() as db:
                db.execute('DELETE FROM public.libreta_telegram_chats WHERE chat_id=%s', (chat_id,))

    def test_failed_debt_insert_rolls_back(self, m, tiendas, monkeypatch):
        a, _ = tiendas

        def fail(db, t):
            raise RuntimeError('fallo simulado')

        monkeypatch.setattr(m, '_crear_deuda', fail)
        with pytest.raises(RuntimeError, match='fallo simulado'):
            m.agregar(transaction(a, tipo='fiado', monto=10))
        assert m.transacciones(a) == []
        t = m.agregar(transaction(a, monto=10))
        with pytest.raises(RuntimeError, match='fallo simulado'):
            m.actualizar(t.id, tipo='fiado')
        assert m.obtener(t.id).tipo == 'venta'
        assert m.deudas_abiertas(a) == []
