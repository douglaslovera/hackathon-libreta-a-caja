import pytest
from core.verificacion import monto_trazable, numeros_en_texto


@pytest.mark.parametrize('text,expected', [
    ('0.30, 4,20, s/ 11 y s/.11', [.3,4.2,11,11]),
    ('un pan una leche uno dos tres', [1,1,1,2,3]),
    ('dieciséis veintidós veintitrés veintiún treinta y cinco cien', [16,22,23,21,35,100]),
    ('medio kilo media docena', [.5,.5,12]),
    ('tres cervezas a seis cincuenta', [3,6,50,6.5]),
    ('cuatro con veinte', [4,20,4.2]),
    ('ciento treinta y cinco', [135]),
])
def test_numbers(text, expected):
    assert numeros_en_texto(text) == expected


@pytest.mark.parametrize('amount,text', [(3,'10 a 0.30'), (180,'190 rebajó 10'), (19.5,'tres a seis cincuenta'), (21,'2 por 3 más 15'), (14,'2 por (3 más 4)'), (4.2,'cuatro con veinte'), (12,'docena'), (.5,'medio'), (9,'2 3 4'), (1,'8 3 4'), (3.005,'3')])
def test_traceable(amount,text):
    assert monto_trazable(amount,text)


@pytest.mark.parametrize('amount,text', [(99,'tres panes'), (4,'dos panes'), (25,'2 3 4 1'), (3.006,'3'), (float('nan'),'3'), (float('inf'),'3')])
def test_untraceable(amount,text):
    assert not monto_trazable(amount,text)


def test_extra_is_direct_evidence():
    assert monto_trazable(11, 'pagó todo', extra=[11])
    assert not monto_trazable(22, 'pagó todo', extra=[11])
