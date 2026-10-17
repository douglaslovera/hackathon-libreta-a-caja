import csv
from pathlib import Path
import pytest
from core.ejecutor import ejecutar
from core.memoria import Memoria
from core.modelos import Accion, Transaccion


def run(actions,text='',memoria=None,store='a',message='m'):
    return ejecutar(actions,text,tienda_id=store,id_mensaje=message,dia='lunes',memoria=memoria if memoria is not None else Memoria())


def test_selected_kit_end_to_end():
    base = Path(__file__).resolve().parents[1] / 'Kit_Reto_Mibanco'
    with (base/'ejemplos_mensajes.csv').open(encoding='utf-8-sig',newline='') as file:
        messages = {r['id_mensaje']:r for r in csv.DictReader(file)}
    actions = {
        'E01':[Accion('registrar',tipo='venta',producto='pan',cantidad=10,precio_unitario=.3),Accion('registrar',tipo='venta',producto='leche gloria',cantidad=3,precio_unitario=4.2)],
        'E03':[Accion('registrar',tipo='fiado',producto='aceite primor',cantidad=1,monto=11,cliente='Rosa María')],
        'E06':[Accion('registrar',tipo='gasto',producto='saco de arroz',cantidad=1,precio_unitario=190,descuento=10)],
        'E09':[Accion('registrar',tipo='venta',producto='atún',cantidad=3)],
        'E11':[Accion('registrar',tipo='venta',producto='pan',cantidad=8,precio_unitario=.3)],
        'E12':[Accion('cobrar_fiado',cliente='Rosa María',producto='aceite')],
        'E14':[Accion('registrar',tipo='fiado',producto='cerveza',cantidad=2,cliente='don Pepe')],
        'E18':[Accion('registrar',tipo='gasto',producto='devolución yogur',monto=5.5)],
        'E21':[Accion('cobrar_fiado',cliente='don Pepe',monto=10)],
        'E26':[Accion('ignorar',motivo='Compra personal')],
    }
    m = Memoria()
    for key, values in actions.items():
        msg = messages[key]
        result = ejecutar(values,msg['mensaje'],tienda_id='a',id_mensaje=key,dia=msg['dia'],memoria=m)
        assert not result.rechazos
    with (base/'ejemplos_respuestas.csv').open(encoding='utf-8-sig',newline='') as file:
        expected = [r for r in csv.DictReader(file) if r['id_mensaje'] in actions]
    assert [t.fila_csv() for t in m.transacciones('a')] == expected


def test_registration_alias_guard_and_question():
    m = Memoria()
    m.guardar_alias('a','guevos','huevos')
    result = run([Accion('registrar',tipo='venta',producto=' GUEVOS ',cantidad=6,precio_unitario=.5)],'6 guevos a 0.50',m)
    assert result.transacciones[0].producto == 'huevos'
    assert result.transacciones[0].monto == 3
    result = run([Accion('registrar',tipo='venta',producto='pan',monto=999)],'vendí pan',m)
    assert result.transacciones[0].monto is None
    assert result.transacciones[0].requiere_confirmacion
    assert result.rechazos and result.preguntas
    assert m.pendiente('a')['transaccion_id'] == result.transacciones[0].id
    assert '❓' in result.respuesta and '🚫' in result.respuesta
    assert m._db.execute('SELECT count(*) FROM log').fetchone()[0] == 0


def test_question_ignore_and_invalid_actions():
    m = Memoria()
    result = run([Accion('preguntar',tipo='gasto',producto='bolsas',pregunta='¿Cuánto costaron?',monto=30),Accion('ignorar',motivo='Personal'),Accion('registrar',tipo='invalido',monto=30)],'30',m)
    assert len(result.transacciones) == 1
    assert result.transacciones[0].cantidad == 1
    assert result.transacciones[0].monto is None
    assert result.preguntas == ['¿Cuánto costaron?']
    assert result.ignorados == ['Personal']
    assert len(result.rechazos) == 1
    assert m.pendiente('b') is None


@pytest.mark.parametrize('field,value', [('monto',float('nan')),('cantidad',-1),('precio_unitario',float('inf'))])
def test_invalid_numbers(field,value):
    result = run([Accion('registrar',tipo='venta',producto='pan',**{field:value})],'1')
    assert result.rechazos and not result.transacciones


def test_full_fuzzy_and_partial_repayments():
    m = Memoria()
    debt = m.agregar(Transaccion('a','old','fiado','aceite primor',cantidad=2,monto=22,cliente='Rosa María'))
    partial = run([Accion('cobrar_fiado',cliente='Rosa Maria',monto=5)],'pagó 5',m)
    assert partial.transacciones[0].producto == 'pago parcial Rosa María'
    assert m.deudas_abiertas('a')[0].pendiente == 17
    paid = run([Accion('cobrar_fiado',cliente='Rosa Marí',producto='aceite',total=True)],'pagó todo',m)
    assert paid.transacciones[0].cantidad == 2
    assert paid.transacciones[0].producto == 'aceite primor'
    assert paid.transacciones[0].monto == 17
    assert not m.deudas_abiertas('a')
    assert m.obtener(debt.id).monto == 22


def test_repayment_guard_does_not_reduce_debt():
    m = Memoria()
    m.agregar(Transaccion('a','old','fiado','arroz',monto=100,cliente='Pepe'))
    result = run([Accion('cobrar_fiado',cliente='Pepe',monto=29)],'me pagó algo',m)
    assert result.transacciones[0].monto is None
    assert result.rechazos
    assert m.deudas_abiertas('a')[0].pendiente == 100


def test_missing_and_ambiguous_debt():
    m = Memoria()
    result = run([Accion('cobrar_fiado',cliente='Ana')],'pagó',m)
    assert result.transacciones[0].tipo == 'cobro_fiado'
    assert result.transacciones[0].monto is None
    result = run([Accion('cobrar_fiado',cliente='Ana',monto=10)],'pagó 10',m)
    assert result.transacciones[0].monto == 10 and result.ignorados
    for _ in range(2):
        m.agregar(Transaccion('a','old','fiado','pan',monto=7,cliente='Pepe'))
    result = run([Accion('cobrar_fiado',cliente='Pepe')],'pagó todo',m)
    assert result.preguntas and result.transacciones[0].monto is None
    assert len(m.deudas_abiertas('a')) == 2
    result = run([Accion('cobrar_fiado',cliente='Pepe')],'pagó todo',m,store='b')
    assert result.transacciones[0].monto is None


def test_product_only_debt_and_unknown_partial():
    m = Memoria()
    m.agregar(Transaccion('a','old','fiado','cerveza',cantidad=2,cliente='Pepe'))
    result = run([Accion('cobrar_fiado',producto='cerveza',monto=10)],'pagó 10',m)
    assert result.transacciones[0].producto == 'pago parcial Pepe'
    assert m.deudas_abiertas('a')[0].pendiente is None
    result = run([Accion('cobrar_fiado',producto='cerveza',total=True)],'pagó todo',m)
    assert result.transacciones[0].monto is None
    assert m.deudas_abiertas('a')


def test_corrections_scope_guard_and_pending():
    m = Memoria()
    original = run([Accion('preguntar',tipo='venta',producto='pan')],'pan',m).transacciones[0]
    result = run([Accion('corregir',ref_transaccion=original.id,campo='monto',nuevo_valor=5)],'eran 5',m)
    assert result.corregidas[0].monto == 5
    assert not result.corregidas[0].requiere_confirmacion
    assert m.pendiente('a') is None
    assert result.respuesta.startswith('✏️ Corregí:')
    assert len(m.transacciones('a')) == 1
    bad = run([Accion('corregir',ref_transaccion=original.id,campo='monto',nuevo_valor=99)],'eran 5',m)
    assert bad.rechazos and not bad.corregidas
    assert m.obtener(original.id).monto == 5
    for ref,store,field in [(original.id,'b','monto'),(999,'a','monto'),(original.id,'a','tienda_id')]:
        result = run([Accion('corregir',ref_transaccion=ref,campo=field,nuevo_valor=7)],'7',m,store=store)
        assert result.rechazos and not result.corregidas
    run([Accion('corregir',ref_transaccion=original.id,campo='cantidad',nuevo_valor=3)],'3 panes',m)
    result = run([Accion('corregir',ref_transaccion=original.id,campo='precio',nuevo_valor=2)],'3 panes a 2',m)
    assert result.corregidas[0].monto == 6
    run([Accion('corregir',ref_transaccion=original.id,campo='producto',nuevo_valor=' ARROZ '),Accion('corregir',ref_transaccion=original.id,campo='tipo',nuevo_valor='fiado')],'arroz fiado',m)
    assert m.obtener(original.id).producto == 'arroz'
    assert m.deudas_abiertas('a')[0].pendiente == 6


def test_overpayment_is_not_silently_truncated():
    m = Memoria()
    m.agregar(Transaccion('a','old','fiado','pan',monto=10,cliente='Ana'))
    result = run([Accion('cobrar_fiado',cliente='Ana',monto=15)],'pagó 15',m)
    assert result.rechazos and not result.transacciones
    assert m.deudas_abiertas('a')[0].pendiente == 10


def test_explicit_amount_discount_and_decimal_rounding():
    result = run([Accion('registrar',tipo='gasto',producto='arroz',monto=190,descuento=10)],'190 menos 10')
    assert result.transacciones[0].monto == 180
    result = run([Accion('registrar',tipo='venta',producto='pan',cantidad=3,precio_unitario=.333)],'3 a 0.333')
    assert result.transacciones[0].monto == 1


def test_cobro_parcial_reduce_deuda_aunque_producto_sea_etiqueta():
    from core.memoria import Memoria
    from core.ejecutor import ejecutar
    from core.modelos import Accion

    m = Memoria()
    ejecutar([Accion('registrar', tipo='fiado', producto='cerveza', cantidad=2, monto=13, cliente='don Pepe')],
             'le fié 2 cervezas a don Pepe a 13', tienda_id='t', id_mensaje='A', dia='lunes', memoria=m)
    r = ejecutar([Accion('cobrar_fiado', cliente='Pepe', producto='pago parcial don Pepe', monto=10)],
                 'don Pepe me pagó 10 de lo que debe', tienda_id='t', id_mensaje='B', dia='martes', memoria=m)
    assert r.transacciones[0].fila_csv()['producto'] == 'pago parcial don Pepe'
    assert m.deudas_abiertas('t')[0].pendiente == 3


def test_cobro_usa_ref_transaccion_explicita():
    from core.memoria import Memoria
    from core.ejecutor import ejecutar
    from core.modelos import Accion

    m = Memoria()
    for cliente in ('Ana', 'Ana María'):
        ejecutar([Accion('registrar', tipo='fiado', producto='arroz', cantidad=1, monto=5, cliente=cliente)],
                 'fié 1 arroz a 5', tienda_id='t', id_mensaje=cliente, dia='lunes', memoria=m)
    objetivo = m.deudas_abiertas('t')[1]
    r = ejecutar([Accion('cobrar_fiado', cliente='Ana', ref_transaccion=objetivo.transaccion_id, total=True)],
                 'Ana María me pagó lo del arroz', tienda_id='t', id_mensaje='C', dia='martes', memoria=m)
    assert r.transacciones[0].monto == 5
    assert [d.cliente for d in m.deudas_abiertas('t')] == ['Ana']
