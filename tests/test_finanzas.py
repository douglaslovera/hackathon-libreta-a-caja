import csv
from datetime import datetime
from pathlib import Path
import pytest
from core.finanzas import cuota_sostenible, flujo_semanal, flujos_por_semana
from core.modelos import Transaccion


def t(tipo='venta', monto=10, dia=None):
    return Transaccion('a','m',tipo,'pan',monto=monto,dia=dia)


def test_cashflow():
    result = flujo_semanal([t('venta',100),t('gasto',30),t('fiado',50),t('cobro_fiado',20),t(monto=None)])
    assert result == dict(ventas=100,cobros=20,gastos=30,flujo=90,fiado_pendiente=30,sin_monto=1)
    assert flujo_semanal([])['flujo'] == 0


def test_full_expected_kit_flow():
    path = Path(__file__).resolve().parents[1] / 'Kit_Reto_Mibanco/ejemplos_respuestas.csv'
    with path.open(encoding='utf-8-sig',newline='') as file:
        rows = list(csv.DictReader(file))
    transactions = [t(r['tipo'],float(r['monto']) if r['monto'] else None) for r in rows]
    sums = {kind:sum(float(r['monto']) for r in rows if r['tipo']==kind and r['monto']) for kind in ('venta','gasto','cobro_fiado')}
    result = flujo_semanal(transactions)
    assert result['flujo'] == pytest.approx(sums['venta'] + sums['cobro_fiado'] - sums['gasto'])
    assert result['sin_monto'] == 3
    assert result['flujo'] == 788.9


@pytest.mark.parametrize('flows,average,monthly,quota', [([100,200],150,649.5,194.85), ([],0,0,0), ([-100],-100,-433,0), ([0],0,0,0)])
def test_installment(flows,average,monthly,quota):
    result = cuota_sostenible(flows)
    assert result['flujo_semanal_promedio'] == average
    assert result['flujo_mensual'] == monthly
    assert result['cuota'] == quota
    assert '× 4.33' in result['explicacion'] and '× 30%' in result['explicacion']
    assert result['explicacion'].endswith('Es una estimación y NO es una aprobación de crédito.')
    assert cuota_sostenible([100],.2)['cuota'] == 86.6
    with pytest.raises(ValueError):
        cuota_sostenible([100],2)


def test_week_groups():
    assert flujos_por_semana([]) == []
    assert flujos_por_semana([t(dia='lunes'),t(dia='lunes'),t(dia='domingo'),t(dia='martes'),t(dia=None)]) == [30,20]
    first, second = t(dia='lunes'), t(dia='martes')
    first.created_at = datetime(2026,10,5)
    second.created_at = '2026-10-13T12:00:00Z'
    assert flujos_por_semana([first,second]) == [10,10]
