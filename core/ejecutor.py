"""Validate proposed actions and apply only amounts supported by evidence."""
from difflib import SequenceMatcher
import math
import unicodedata

from core.memoria import Memoria
from core.modelos import Accion, Resultado, TIPOS, Transaccion
from core.verificacion import monto_trazable


def _numero(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError('Cantidad o importe inválido')
    return result


def _nombre(value):
    text = ''.join(c for c in unicodedata.normalize('NFD', (value or '').lower()) if unicodedata.category(c) != 'Mn')
    return ' '.join(w for w in text.split() if w not in {'don', 'dona', 'senor', 'senora', 'el', 'la'})


def _similitud(a, b):
    a, b = _nombre(a), _nombre(b)
    if not a or not b:
        return 0
    if a == b:
        return 1
    if set(a.split()) <= set(b.split()) or set(b.split()) <= set(a.split()):
        return .9
    return SequenceMatcher(None, a, b).ratio()


def _deuda(accion, memoria, tienda_id):
    debts = memoria.deudas_abiertas(tienda_id)
    # The LLM sees debt ids in its context; trust an explicit one over fuzzy matching.
    if accion.ref_transaccion is not None:
        for debt in debts:
            if debt.transaccion_id == accion.ref_transaccion:
                return debt
    # "pago parcial <cliente>" is an output label, not the debt's product.
    producto = accion.producto if accion.producto and not accion.producto.startswith('pago parcial') else None
    candidates = []
    for debt in debts:
        client = _similitud(accion.cliente, debt.cliente) if accion.cliente else 1
        product = _similitud(producto, debt.producto) if producto else 1
        if (accion.cliente or producto) and client >= .65 and product >= .6:
            candidates.append(((client + product) / 2, debt))
    candidates.sort(key=lambda item: item[0], reverse=True)
    # Equal-quality matches cannot safely identify a single debt.
    if not candidates or (len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .05):
        return None
    return candidates[0][1]


def ejecutar(acciones: list[Accion], texto: str, *, tienda_id: str, id_mensaje: str,
             dia: str | None, memoria: Memoria) -> Resultado:
    resultado = Resultado()
    aliases = memoria.alias(tienda_id)

    def producto(value):
        if value is not None and not isinstance(value, str):
            raise ValueError('El producto debe ser texto')
        value = (value or '').strip().lower()
        return aliases.get(value, value)

    def guardar(tipo, nombre, cantidad=1, monto=None, cliente=None, pregunta=None):
        if tipo not in TIPOS:
            raise ValueError(f'Tipo inválido: {tipo}')
        if monto is None:
            pregunta = pregunta or f'¿Cuál es el monto total de {nombre or "esta operación"}?'
        t = memoria.agregar(Transaccion(tienda_id=tienda_id, id_mensaje=id_mensaje, tipo=tipo,
            producto=nombre, cantidad=cantidad, monto=monto, requiere_confirmacion=monto is None,
            dia=dia, cliente=cliente, pregunta=pregunta if monto is None else None))
        resultado.transacciones.append(t)
        if monto is None:
            resultado.preguntas.append(pregunta)
            memoria.fijar_pendiente(tienda_id, {'transaccion_id': t.id, 'pregunta': pregunta})
        return t

    def verificar(monto, extra=()):
        if monto is None:
            return None
        monto = round(_numero(monto), 2)
        if not monto_trazable(monto, texto, extra=extra):
            resultado.rechazos.append(f'El monto S/ {monto:.2f} no se puede verificar con los números del mensaje.')
            return None
        return monto

    for a in acciones:
        try:
            if a.accion == 'ignorar':
                resultado.ignorados.append(a.motivo or 'No corresponde al negocio.')
            elif a.accion in ('registrar', 'preguntar'):
                if a.tipo not in TIPOS:
                    raise ValueError(f'Tipo inválido: {a.tipo}')
                quantity = _numero(a.cantidad if a.cantidad is not None else 1)
                amount = None
                if a.accion == 'registrar':
                    if a.precio_unitario is not None:
                        amount = quantity * _numero(a.precio_unitario) - _numero(a.descuento or 0)
                    elif a.monto is not None:
                        amount = _numero(a.monto) - _numero(a.descuento or 0)
                    amount = verificar(amount)
                guardar(a.tipo, producto(a.producto), quantity, amount, a.cliente, a.pregunta)
            elif a.accion == 'cobrar_fiado':
                debt = _deuda(a, memoria, tienda_id)
                amount = _numero(a.monto) if a.monto is not None else None
                if debt and debt.pendiente is not None and amount is not None and amount > debt.pendiente and not a.total:
                    raise ValueError('El cobro supera la deuda pendiente; confirma a qué corresponde la diferencia.')
                full = debt is not None and debt.pendiente is not None and (a.total or amount is None or amount == debt.pendiente)
                if full:
                    amount = debt.pendiente
                    original = memoria.obtener(debt.transaccion_id)
                    name, quantity = debt.producto, original.cantidad
                else:
                    name = f'pago parcial {debt.cliente if debt else (a.cliente or "cliente")}'
                    quantity = 1
                amount = verificar(amount, [debt.pendiente] if debt and debt.pendiente is not None else [])
                question = None
                if amount is None:
                    question = f'¿Cuánto pagó {a.cliente or "el cliente"} y por qué deuda?'
                guardar('cobro_fiado', name, quantity, amount, a.cliente or (debt.cliente if debt else None), question)
                if debt and amount is not None:
                    memoria.registrar_cobro(tienda_id, debt.transaccion_id, None if full else amount)
                elif debt is None and amount is not None:
                    resultado.ignorados.append('Nota: registré el cobro sin vincularlo a una deuda abierta.')
            elif a.accion == 'corregir':
                original = memoria.obtener(a.ref_transaccion) if a.ref_transaccion is not None else None
                if original is None or original.tienda_id != tienda_id:
                    raise ValueError('No encontré esa transacción en esta tienda para corregirla.')
                if a.campo not in {'producto', 'cantidad', 'monto', 'tipo', 'precio', 'precio_unitario'}:
                    raise ValueError(f'Campo de corrección inválido: {a.campo}')
                fields = {}
                if a.campo == 'producto':
                    fields['producto'] = producto(a.nuevo_valor)
                elif a.campo == 'tipo':
                    if a.nuevo_valor not in TIPOS:
                        raise ValueError(f'Tipo inválido: {a.nuevo_valor}')
                    fields['tipo'] = a.nuevo_valor
                elif a.campo == 'cantidad':
                    fields['cantidad'] = _numero(a.nuevo_valor)
                else:
                    amount = _numero(a.nuevo_valor)
                    if a.campo in {'precio', 'precio_unitario'}:
                        amount *= original.cantidad
                    amount = verificar(amount)
                    if amount is None:
                        continue
                    fields.update(monto=amount, requiere_confirmacion=False, pregunta=None)
                updated = memoria.actualizar(original.id, **fields)
                resultado.corregidas.append(updated)
                pending = memoria.pendiente(tienda_id)
                if not updated.requiere_confirmacion and pending and pending.get('transaccion_id') == updated.id:
                    memoria.fijar_pendiente(tienda_id, None)
            else:
                raise ValueError(f'Acción inválida: {a.accion}')
        except (ValueError, TypeError, OverflowError) as error:
            resultado.rechazos.append(str(error))

    def detalle(t):
        cantidad = f'{t.cantidad:g}'
        amount = f'S/ {t.monto:.2f}' if t.monto is not None else 'monto pendiente'
        return f'{t.tipo} · {cantidad} {t.producto} · {amount}'

    lines = [f'✅ Anoté: {detalle(t)}' for t in resultado.transacciones]
    lines += [f'❓ {p}' for p in resultado.preguntas]
    lines += [f'✏️ Corregí: {detalle(t)}' for t in resultado.corregidas]
    lines += [f'🚫 No lo anoté: {r}' for r in resultado.rechazos]
    lines += resultado.ignorados
    resultado.respuesta = '\n'.join(lines)
    return resultado
