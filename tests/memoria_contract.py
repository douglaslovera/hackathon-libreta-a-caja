"""Backend-independent ledger contract, collected by both backend test modules."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from core.modelos import Resultado, Transaccion


def transaction(store='a', **kwargs):
    return Transaccion(tienda_id=store, id_mensaje='m', tipo=kwargs.pop('tipo','venta'), producto=kwargs.pop('producto','pan'), **kwargs)


class ComportamientoMemoria:
    @pytest.fixture
    def tiendas(self):
        prefix = 'test:' + uuid4().hex
        return prefix + ':a', prefix + ':b'

    def test_debt_lifecycle_correction_and_isolation(self, m, tiendas):
        a, b = tiendas
        t = m.agregar(transaction(a, tipo='fiado',monto=20,cliente='Rosa',cantidad=2))
        m.registrar_cobro(a,t.id,5)
        assert m.deudas_abiertas(a)[0].pendiente == 15
        m.actualizar(t.id,monto=25,producto='arroz')
        debt = m.deudas_abiertas(a)[0]
        assert (debt.monto_original,debt.pendiente,debt.producto) == (25,20,'arroz')
        with pytest.raises(ValueError):
            m.registrar_cobro(b,t.id,5)
        with pytest.raises(ValueError):
            m.registrar_cobro(a,t.id,-1)
        m.registrar_cobro(a,t.id,None)
        assert m.deudas_abiertas(a) == []
        t2 = m.agregar(transaction(a, tipo='fiado',cliente='Pepe'))
        m.registrar_cobro(a,t2.id,10)
        assert m.deudas_abiertas(a)[0].pendiente is None
        m.actualizar(t2.id,monto=30)
        assert m.deudas_abiertas(a)[0].pendiente == 30
        m.actualizar(t2.id,tipo='venta')
        assert not m.deudas_abiertas(a)
        m.actualizar(t2.id,tipo='fiado')
        assert m.deudas_abiertas(a)[0].pendiente == 30


    def test_search_and_delete(self, m, tiendas):
        a, b = tiendas
        t = m.agregar(transaction(a, tipo='fiado', producto='aceite primor',cliente='Rosa',monto=11))
        other = m.agregar(transaction(b,producto='aceite primor'))
        assert [r.id for r in m.buscar(a,'aceite')] == [t.id]
        assert [r.id for r in m.buscar(a,'Rosa')] == [t.id]
        assert m.buscar(a,'" *') == []
        m.actualizar(t.id,producto='leche')
        assert m.buscar(a,'aceite') == []
        m.guardar_alias(a,'lech','leche')
        m.fijar_pendiente(a, {'pregunta':'¿Monto?'})
        m.log(a,'m','texto','[]',Resultado(transacciones=[t]))
        m.borrar_tienda(a)
        assert not m.transacciones(a) and not m.deudas_abiertas(a)
        assert not m.buscar(a,'leche') and not m.alias(a) and m.pendiente(a) is None
        assert m.obtener(other.id) is not None


    def test_threads_and_invalid_updates(self, m, tiendas):
        a, b = tiendas
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: m.agregar(transaction(a, monto=i)), range(80)))
        assert len(m.transacciones(a)) == 80
        assert len({t.id for t in m.transacciones(a)}) == 80
        with pytest.raises(ValueError):
            m.actualizar(-1,monto=1)
        with pytest.raises(ValueError):
            m.actualizar(m.transacciones(a)[0].id,tienda_id=b)
        assert m.obtener(-1) is None


    def test_created_metadata_survives_roundtrip(self, m, tiendas):
        a, b = tiendas
        t = m.agregar(transaction(a))
        assert t.created_at
        assert m.obtener(t.id).created_at == t.created_at
        assert m.transacciones(a)[0].created_at == t.created_at

    def test_context_alias_and_pending(self, m, tiendas):
        a, b = tiendas
        rows = [m.agregar(transaction(a, monto=i)) for i in range(12)]
        m.guardar_alias(a, ' GUEVOS ', 'Huevos')
        m.fijar_pendiente(a, {'transaccion_id': rows[-1].id, 'pregunta': '¿Cuánto?'})
        contexto = m.contexto(a, 'lunes')
        assert contexto.dia == 'lunes'
        assert [t.id for t in contexto.ultimas] == [t.id for t in rows[-10:]]
        assert contexto.alias == {'guevos': 'huevos'}
        assert m.pendiente(a)['transaccion_id'] == rows[-1].id
        assert m.transacciones(b) == []
        assert m.ultimas(a, 0) == []
        m.fijar_pendiente(a, None)
        assert m.pendiente(a) is None

    def test_weeks_keep_first_date_and_delete(self, m, tiendas):
        a, b = tiendas
        m.fechar_semana(a, 'm', '2026-10-05')
        m.fechar_semana(a, 'm', '2026-10-12')
        m.fechar_semana(b, 'm', '2026-10-12')
        assert m.semanas_mensaje(a) == {'m': '2026-10-05'}
        m.borrar_tienda(a)
        assert m.semanas_mensaje(a) == {}
        assert m.semanas_mensaje(b) == {'m': '2026-10-12'}

    def test_search_prefix_multiple_terms_and_limits(self, m, tiendas):
        a, b = tiendas
        t = m.agregar(transaction(a, producto='aceite primor', cliente='Rosa'))
        assert [r.id for r in m.buscar(a, 'acei inexistente')] == [t.id]
        assert m.buscar(a, 'aceite', 0) == []
        assert m.buscar(a, 'aceite', -1) == []
        assert m.buscar(b, 'aceite') == []
