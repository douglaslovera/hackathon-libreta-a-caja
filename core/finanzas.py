"""Cash-flow summaries; fiados enter cash only when collected."""
from datetime import datetime
import unicodedata
from core.modelos import DIAS, Transaccion


def flujo_semanal(transacciones: list[Transaccion]) -> dict:
    result = dict(ventas=0.0, cobros=0.0, gastos=0.0, flujo=0.0, fiado_pendiente=0.0, sin_monto=0)
    keys = {'venta': 'ventas', 'cobro_fiado': 'cobros', 'gasto': 'gastos', 'fiado': 'fiado_pendiente'}
    for t in transacciones:
        if t.monto is None:
            result['sin_monto'] += 1
        elif t.tipo in keys:
            result[keys[t.tipo]] += t.monto
    # A transaction-only summary cannot attribute payments to particular debts.
    result['fiado_pendiente'] = max(0, result['fiado_pendiente'] - result['cobros'])
    result['flujo'] = result['ventas'] + result['cobros'] - result['gastos']
    return {k: round(v, 2) if isinstance(v, float) else v for k,v in result.items()}


def cuota_sostenible(flujos_semanales: list[float], porcentaje: float = .30) -> dict:
    if not 0 <= porcentaje <= 1:
        raise ValueError('El porcentaje debe estar entre 0 y 1')
    average = sum(flujos_semanales) / len(flujos_semanales) if flujos_semanales else 0.0
    monthly = average * 4.33
    proposed = monthly * porcentaje
    cuota = max(0, proposed)
    explanation = (f'Promedio semanal: S/ {sum(flujos_semanales):.2f} ÷ {len(flujos_semanales)} semanas = S/ {average:.2f}.\n' if flujos_semanales else 'Sin semanas registradas: promedio semanal S/ 0.00.\n')
    explanation += f'S/ {average:.2f} × 4.33 = S/ {monthly:.2f} de flujo mensual.\nS/ {monthly:.2f} × {porcentaje:.0%} = S/ {proposed:.2f}.'
    if monthly < 0:
        explanation += ' La cuota se limita a cero porque el flujo es negativo.'
    explanation += f' Cuota sostenible: S/ {cuota:.2f}.'
    explanation += '\nEs una estimación y NO es una aprobación de crédito.'
    return dict(flujo_semanal_promedio=round(average, 2), flujo_mensual=round(monthly, 2), cuota=round(cuota, 2), porcentaje=porcentaje, explicacion=explanation)


def flujos_por_semana(transacciones: list[Transaccion]) -> list[float]:
    """Group insertion-ordered rows on a weekday rollback or ISO-week change.

    The frozen Transaccion has no timestamp. When enriched objects expose
    created_at (datetime or ISO string), also use its ISO year/week. Missing
    days/timestamps preserve the current group; repeated weekdays stay together.
    """
    normalize = lambda s: ''.join(c for c in unicodedata.normalize('NFD', s.lower()) if unicodedata.category(c) != 'Mn')
    days = {normalize(d): i for i,d in enumerate(DIAS)}
    groups, current = [], []
    previous_day = previous_week = None
    for t in transacciones:
        day = days.get(normalize(t.dia)) if t.dia else None
        stamp = getattr(t, 'created_at', None)
        week = None
        if stamp:
            stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')) if isinstance(stamp, str) else stamp
            week = stamp.isocalendar()[:2]
        if current and ((day is not None and previous_day is not None and day < previous_day) or (week is not None and previous_week is not None and week != previous_week)):
            groups.append(flujo_semanal(current)['flujo'])
            current, previous_day, previous_week = [], None, None
        current.append(t)
        if day is not None:
            previous_day = day
        if week is not None:
            previous_week = week
    if current:
        groups.append(flujo_semanal(current)['flujo'])
    return groups
