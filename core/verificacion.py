"""Conservative arithmetic provenance from numbers actually present in a message."""
from itertools import combinations, permutations
import math
import re
import unicodedata


def _normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto.lower()) if unicodedata.category(c) != 'Mn')


_UNIDADES = dict(zip('cero un uno una dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciseis diecisiete dieciocho diecinueve veinte'.split(), [0,1,1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]))
_UNIDADES.update({'veinti' + palabra: 20 + valor for palabra, valor in list(_UNIDADES.items()) if 1 <= valor <= 9})
_UNIDADES.update(dict(zip('treinta cuarenta cincuenta sesenta setenta ochenta noventa cien ciento medio media docena'.split(), [30,40,50,60,70,80,90,100,100,.5,.5,12])))


def numeros_en_texto(texto: str) -> list[float]:
    tokens = re.findall(r'\d+(?:[.,]\d+)?|[a-z]+', _normalizar(texto))
    parsed = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token[0].isdigit():
            value = float(token.replace(',', '.'))
        elif token in _UNIDADES:
            value = float(_UNIDADES[token])
        else:
            i += 1
            continue
        end = i + 1
        if value in (30,40,50,60,70,80,90) and tokens[end:end+1] == ['y'] and end+1 < len(tokens) and tokens[end+1] in _UNIDADES and 1 <= _UNIDADES[tokens[end+1]] <= 9:
            value += _UNIDADES[tokens[end+1]]
            end += 2
        elif value == 100 and end < len(tokens) and tokens[end] in _UNIDADES and 0 < _UNIDADES[tokens[end]] < 100:
            value += _UNIDADES[tokens[end]]
            end += 1
            if tokens[end:end+1] == ['y'] and end+1 < len(tokens) and tokens[end+1] in _UNIDADES and 1 <= _UNIDADES[tokens[end+1]] <= 9:
                value += _UNIDADES[tokens[end+1]]
                end += 2
        parsed.append((i, end, value))
        i = end
    values = [p[2] for p in parsed]
    for first, second in zip(parsed, parsed[1:]):
        between = tokens[first[1]:second[0]]
        # Adjacent spoken cents ("seis cincuenta") or explicit "con".
        spoken_cents = (not between and not tokens[first[0]][0].isdigit() and not tokens[second[0]][0].isdigit() and second[2] % 10 == 0)
        if (spoken_cents or between == ['con']) and 0 <= first[2] < 100 and 10 <= second[2] <= 99 and first[2].is_integer() and second[2].is_integer():
            values.append(round(first[2] + second[2] / 100, 2))
    return values


def monto_trazable(monto: float, texto: str, extra: list[float] = ()) -> bool:
    if not math.isfinite(monto):
        return False
    equal = lambda n: abs(monto - n) <= .005 + 1e-9
    if any(equal(n) for n in extra if n is not None and math.isfinite(n)):
        return True
    numbers = numeros_en_texto(texto)
    if any(equal(n) for n in numbers):
        return True
    # Each occurrence can be used at most once; evaluate both parenthesizations.
    def pairs(a, b):
        return (a+b, a-b, a*b)
    for count in (2, 3):
        for subset in combinations(numbers, count):
            for nums in set(permutations(subset)):
                if count == 2:
                    candidates = pairs(*nums)
                else:
                    a,b,c = nums
                    candidates = [v for x in pairs(a,b) for v in pairs(x,c)] + [v for x in pairs(b,c) for v in pairs(a,x)]
                if any(equal(n) for n in candidates):
                    return True
    return False
