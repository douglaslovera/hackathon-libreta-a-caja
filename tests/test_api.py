"""Offline HTTP contract checks using the real application service."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from canales.api import create_app
from core import servicio
from core.memoria import Memoria
from core.modelos import Accion


@pytest.fixture
def libreta(monkeypatch):
    monkeypatch.setattr(servicio, 'ahora', lambda: datetime(2026, 10, 17, 12, tzinfo=servicio.LIMA))

    def interpretar(texto, contexto):
        if texto.startswith('Respuesta a la pregunta'):
            return [Accion('corregir', ref_transaccion=contexto.ultimas[-1].id,
                           campo='monto', nuevo_valor=16.5)]
        if 'atunes' in texto:
            return [Accion('preguntar', tipo='venta', producto='atún', cantidad=3, pregunta='¿A cuánto?')]
        if 'fié' in texto:
            return [Accion('registrar', tipo='fiado', producto='aceite', cantidad=1, monto=11, cliente='Rosa')]
        return [Accion('registrar', tipo='venta', producto='pan', cantidad=10, precio_unitario=0.3)]

    memoria = Memoria()
    lib = servicio.Libreta(memoria, interpretar=interpretar)
    yield lib
    lib.close()


@pytest.fixture
def client(libreta):
    with TestClient(create_app(libreta, api_key='secret', cors_origins=['https://shop.example'])) as client:
        client.headers['Authorization'] = 'Bearer secret'
        yield client


def test_health_and_auth(client, libreta):
    with TestClient(create_app(libreta, api_key='secret')) as unauthenticated:
        assert unauthenticated.get('/health').status_code == 200
        assert unauthenticated.get('/shops/t/summary').status_code == 401
    assert client.get('/health', headers={'Authorization': ''}).json() == {'status': 'ok'}
    for authorization in ['', 'Bearer wrong', 'Basic secret', 'Bearer sécret']:
        # httpx requires ASCII header values, so encode this one explicitly.
        headers = {'Authorization': authorization.encode('utf-8')}
        for path in ['/shops/t/summary', '/docs', '/openapi.json']:
            assert client.get(path, headers=headers).status_code == 401
    assert client.get('/docs').status_code == 200
    schema = client.get('/openapi.json').json()
    assert {'Transaction', 'Debt', 'Result', 'CashFlow', 'LoanEstimate', 'Summary', 'Pending'} <= schema['components']['schemas'].keys()


def test_audio(client, monkeypatch):
    seen = []

    def transcribe(audio, name, vocab):
        seen.append((audio, name, vocab))
        return 'vendí 10 panes a 0.30'

    monkeypatch.setattr('core.voz.transcribir', transcribe)
    response = client.post('/shops/tg:123/audio', files={'file': ('original.webm', b'voice', 'audio/webm')})
    assert response.status_code == 200
    body = response.json()
    assert body['transcript'] == 'vendí 10 panes a 0.30'
    assert set(body['result']) == {'transactions', 'corrected', 'questions', 'rejected', 'ignored', 'reply'}
    t = body['result']['transactions'][0]
    assert t == dict(id=1, message_id=t['message_id'], type='venta', product='pan', quantity=10,
                     amount=3.0, needs_confirmation=False, day='sábado', customer=None, question=None)
    assert seen == [(b'voice', 'original.webm', [])]
    assert client.post('/shops/t/audio', content=(
        b'--voice\r\nContent-Disposition: form-data; name="file"; filename=""\r\n'
        b'Content-Type: audio/webm\r\n\r\nvoice\r\n--voice--\r\n'),
        headers={'Content-Type': 'multipart/form-data; boundary=voice'}).status_code == 200
    assert seen[-1][1] == 'nota.webm'


def test_audio_no_speech(client, libreta, monkeypatch):
    monkeypatch.setattr(libreta, 'transcribir', lambda *a, **kw: '')
    r = client.post('/shops/t/audio', files={'file': ('nota.ogg', b'voice', 'audio/ogg')})
    assert r.status_code == 422 and r.json() == {'detail': 'no_speech'}


def test_audio_size_and_type(client):
    r = client.post('/shops/t/audio', files={'file': ('nota.ogg', b'x' * (10 * 1024 * 1024 + 1), 'audio/ogg')})
    assert r.status_code == 413
    for name, mime in [('nota.exe', 'audio/ogg'), ('nota.ogg', 'text/plain'), ('nota', 'application/octet-stream')]:
        assert client.post('/shops/t/audio', files={'file': (name, b'x', mime)}).status_code == 415


def test_pending_answer_and_skip(client, libreta):
    assert client.get('/shops/t/pending').json() == {'pending': None}
    assert client.post('/shops/t/pending/answer', json={'text': 'a 5.50'}).status_code == 409
    libreta.registrar('vendí 3 atunes', tienda_id='t')
    assert client.get('/shops/t/pending').json() == {'pending': {'transaction_id': 1, 'question': '¿A cuánto?'}}
    r = client.post('/shops/t/pending/answer', json={'text': '16.50 en total'})
    assert r.status_code == 200
    assert r.json()['corrected'][0]['amount'] == 16.5
    assert client.get('/shops/t/pending').json() == {'pending': None}
    assert client.delete('/shops/t/pending').json() == {'skipped': False}
    libreta.registrar('vendí 3 atunes', tienda_id='t')
    assert client.delete('/shops/t/pending').json() == {'skipped': True}
    assert libreta.transacciones('t')[-1].requiere_confirmacion


def test_transactions_and_weeks(client, libreta):
    libreta.registrar('vendí 10 panes a 0.30', tienda_id='t')
    r = client.get('/shops/t/transactions?week=all').json()
    assert r['week'] == 'all' and len(r['transactions']) == 1
    assert client.get('/shops/t/transactions').json()['week'] == '2026-10-12'
    assert client.get('/shops/t/transactions?week=2026-10-05').json()['transactions'] == []
    for week in ['yesterday', '2026-02-30', '20261012', '2026-1-1', '2026-W42-1']:
        for path in ['transactions', 'cash-flow']:
            assert client.get(f'/shops/t/{path}', params={'week': week}).status_code == 422
    for shop in ['bad.id', 'x' * 65, 'ñ']:
        assert client.get(f'/shops/{shop}/summary').status_code == 422


def test_debts_flow_estimate_summary_and_reset(client, libreta):
    libreta.registrar('le fié aceite a Rosa, 11', tienda_id='t')
    libreta.registrar('vendí 10 panes a 0.30', tienda_id='t')
    debts = client.get('/shops/t/debts').json()['debts']
    assert debts == [dict(transaction_id=1, customer='Rosa', product='aceite', original_amount=11, outstanding=11)]
    flow = client.get('/shops/t/cash-flow').json()
    assert flow == dict(week='2026-10-12', sales=3, collections=0, expenses=0,
                        net_cash_flow=3, outstanding_credit=11, missing_amounts=0)
    assert client.get('/shops/t/cash-flow?week=all').json()['week'] == 'all'
    estimate = client.get('/shops/t/loan-estimate').json()
    assert set(estimate) == {'avg_weekly_cash_flow', 'monthly_cash_flow', 'monthly_payment',
                             'percentage', 'explanation', 'weeks'}
    assert estimate['avg_weekly_cash_flow'] == 3
    assert estimate['monthly_cash_flow'] == 12.99
    assert estimate['weeks'] == [{'week': '2026-10-12', 'net_cash_flow': 3}]
    summary = client.get('/shops/t/summary').json()
    assert summary['cash_flow'] == flow and summary['loan_estimate'] == estimate
    assert summary['debts'] == debts and summary['pending'] is None
    assert len(summary['transactions']) == 2
    assert client.delete('/shops/t').json() == {'reset': True}
    assert client.get('/shops/t/transactions?week=all').json()['transactions'] == []
    assert client.get('/shops/t/debts').json() == {'debts': []}


def test_batch(client, libreta):
    data = 'id_mensaje,dia,mensaje\nX1,lunes,vendí 10 panes a 0.30\nX2,lunes,vendí 3 atunes\n'.encode('utf-8-sig')
    r = client.post('/shops/t/batch', files={'file': ('entrada.csv', data, 'text/csv')})
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('text/csv')
    assert r.headers['content-disposition'] == 'attachment; filename="entrega.csv"'
    assert r.headers['x-rows'] == '2' and r.headers['x-needs-confirmation'] == '1'
    assert float(r.headers['x-net-cash-flow']) == 3
    assert 'X1,venta,pan,10,3.00,NO' in r.content.decode('utf-8-sig')
    assert libreta.transacciones('t', 'all') == []
    assert client.post('/shops/t/batch', files={'file': ('entrada.txt', data)}).status_code == 415


def test_cors(client):
    r = client.options('/shops/t/audio', headers={'Origin': 'https://shop.example',
                       'Access-Control-Request-Method': 'POST',
                       'Access-Control-Request-Headers': 'authorization,content-type'})
    assert r.status_code == 200 and r.headers['access-control-allow-origin'] == 'https://shop.example'
    r = client.get('/shops/t/summary', headers={'Origin': 'https://shop.example'})
    assert {'x-rows', 'x-needs-confirmation', 'x-net-cash-flow', 'content-disposition'} <= set(
        r.headers['access-control-expose-headers'].lower().replace(' ', '').split(','))


def test_main_auth_and_no_telegram(tmp_path):
    from main import build_app

    with pytest.raises(ValueError, match='API_KEY'):
        build_app({})
    with TestClient(build_app({'ALLOW_NO_AUTH': '1', 'TELEGRAM_TOKEN': 'must-not-poll',
                              'DB_PATH': str(tmp_path / 'nested/libreta.db')},
                              no_telegram=True)) as client:
        assert client.get('/shops/t/summary').status_code == 200


def test_main_telegram_lifespan(tmp_path, monkeypatch):
    import canales.telegram
    from main import build_app

    events = []

    def method(name):
        async def run(*args, **kwargs):
            events.append((name, kwargs))
        return run

    bot = SimpleNamespace(**{name: method(name) for name in ['initialize', 'start', 'stop', 'shutdown']},
                          updater=SimpleNamespace(start_polling=method('poll'), stop=method('stop_poll')))
    captured = []
    monkeypatch.setattr(canales.telegram, 'crear_aplicacion', lambda token, lib: captured.append((token, lib)) or bot)
    app = build_app({'API_KEY': 'secret', 'TELEGRAM_TOKEN': 'fake', 'DB_PATH': str(tmp_path / 'bot.db')})
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert captured == [('fake', app.state.libreta)]
        assert events == [('initialize', {}), ('start', {}), ('poll', {'drop_pending_updates': False})]
    assert [name for name, _ in events] == ['initialize', 'start', 'poll', 'stop_poll', 'stop', 'shutdown']


def test_main_database_url_selects_backend_and_closes(monkeypatch, tmp_path):
    import main
    from unittest.mock import Mock

    memoria = Memoria()
    close = Mock(wraps=memoria.close)
    monkeypatch.setattr(memoria, 'close', close)
    factory = Mock(return_value=memoria)
    monkeypatch.setattr(main, 'abrir_memoria', factory)
    monkeypatch.chdir(tmp_path)
    url = 'postgresql://example.invalid/postgres'
    app = main.build_app({'ALLOW_NO_AUTH': '1', 'DATABASE_URL': url, 'DB_PATH': 'ignored/ledger.db'})
    with TestClient(app) as client:
        assert client.get('/shops/t/summary').status_code == 200
    factory.assert_called_once_with(url)
    close.assert_called_once_with()
    assert not (tmp_path / 'ignored').exists()
