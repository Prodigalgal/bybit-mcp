import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from bybit_mcp.app import create_app
from bybit_mcp.bybit import BybitClient
from bybit_mcp.catalog import lookup
from bybit_mcp.config import Settings
from bybit_mcp.market_data import candle_end
from tests.test_service import access, rpc


NOW = 1793612100000


@pytest.fixture
def markets(tmp_path):
    calls, failures, control = [], set(), {}
    def upstream(request):
        calls.append(request)
        params, path = request.url.params, request.url.path
        symbol = params.get('symbol', '')
        if (symbol, path) in failures:
            return httpx.Response(200, json={'retCode': 10001, 'retMsg': 'Synthetic component rejection', 'time': NOW})
        if path.endswith('/tickers'):
            result = {'list': [{'symbol': symbol, 'lastPrice': '100.12345678', 'fundingRate': '0.0001'}]}
        elif path.endswith('/instruments-info'):
            result = {'list': [{'symbol': symbol, 'status': 'Trading', 'settleCoin': 'USDT', 'symbolType': 'stock',
                               'lotSizeFilter': {'qtyStep': '0.001'}, 'priceFilter': {'tickSize': '0.01'}}]}
        elif path.endswith('/kline'):
            interval = params['interval']
            step = 86400000 if interval == 'D' else int(interval) * 60000
            start = NOW // step * step
            result = {'list': [[str(start-i*step), '100', '101', '99', '999' if i == 0 else str(100-i), '1', '100']
                               for i in range(int(params['limit']))]}
        elif path.endswith('/open-interest'):
            result = {'list': [{'openInterest': '12', 'timestamp': '2000'}, {'openInterest': '10', 'timestamp': '1000'}],
                      'nextPageCursor': 'next-oi-page'}
        elif path.endswith('/funding/history'):
            result = {'list': [{'fundingRate': '0.0001', 'fundingRateTimestamp': '2000'}]}
        elif path.endswith('/orderbook'):
            result = {'s': symbol, 'b': [['100', '1']], 'a': [['101', '1']], 'ts': NOW}
        else:
            raise AssertionError('Snapshot requested a non-market endpoint: ' + path)
        if control.get('after_response'):
            control['after_response']()
        return httpx.Response(200, json={'retCode': 0, 'retMsg': 'OK', 'result': result, 'time': NOW})
    app = create_app(Settings('http://testserver', 'x'*40, Fernet.generate_key().decode(), tmp_path), httpx.MockTransport(upstream))
    with TestClient(app) as client:
        yield app, client, calls, failures, control


def snapshot(client, token, **arguments):
    return rpc(client, token, 'bybit_get_market_snapshot', {'symbols': ['MUUSDT'], 'kline_limit': 2, **arguments}).json()['result']


def test_demo_snapshot_has_real_mainnet_source_and_never_uses_api_credentials(markets, monkeypatch):
    app, client, calls, _, _ = markets
    _, tokens = access(app, client)
    def no_credentials(*args):
        raise AssertionError('Public market research must not decrypt Bybit keys')
    monkeypatch.setattr(app.state.store, 'decrypt', no_credentials)
    response = snapshot(client, tokens['access_token'], symbols=['MUUSDT', 'MSFTUSDT'])
    assert not response['isError']
    data = response['structuredContent']
    assert data['account_environment'] == 'demo' and data['market_data_environment'] == 'mainnet'
    assert data['upstream_host'] == 'https://api.bybit.com' and data['state'] == 'complete'
    assert [item['symbol'] for item in data['items']] == ['MUUSDT', 'MSFTUSDT']
    assert all(r.url.host == 'api.bybit.com' and not any(k.startswith('x-bapi-') for k in r.headers) for r in calls)
    assert all(r.method == 'GET' and r.url.path.startswith('/v5/market/') for r in calls)
    item = data['items'][0]
    assert item['ticker']['data']['lastPrice'] == '100.12345678'
    assert item['open_interest']['list'][0]['timestamp'] == '1000'
    assert item['open_interest']['nextPageCursor'] == 'next-oi-page'
    for candles in item['klines'].values():
        assert len(candles['list']) == 2 and candles['excluded_open_candles'] == 1
        assert all(row[4] != '999' for row in candles['list'])
        assert candles['next_end_ms'] == int(candles['list'][0][0]) - 1
    assert len(response['content'][0]['text']) < len(json.dumps(data)) / 2


def test_raw_quotes_and_profile_distinguish_testnet_from_demo(markets):
    app, client, calls, _, _ = markets
    _, tokens = access(app, client, environment='testnet')
    quote = rpc(client, tokens['access_token'], 'bybit_get_tickers', {'category': 'linear', 'symbol': 'MUUSDT'}).json()['result']['structuredContent']
    assert quote['environment'] == quote['account_environment'] == quote['market_data_environment'] == 'testnet'
    assert calls[-1].url.host == 'api-testnet.bybit.com'
    assert quote['as_of_ms'] == NOW and quote['retrieved_at_ms'] > 0
    profile = rpc(client, tokens['access_token'], 'bybit_get_profile', {}).json()['result']['structuredContent']
    assert profile['market_data_environment'] == profile['account_environment'] == 'testnet'


def test_component_failure_keeps_other_symbols_and_explicit_errors(markets):
    _, client, _, failures, _ = markets
    app = markets[0]
    _, tokens = access(app, client)
    failures.add(('MUUSDT', '/v5/market/tickers'))
    response = snapshot(client, tokens['access_token'], symbols=['MUUSDT', 'MSFTUSDT'], include=[])
    data = response['structuredContent']
    assert not response['isError'] and data['partial'] and data['state'] == 'partial'
    assert data['items'][0]['errors']['ticker']['code'] == 10001
    assert data['items'][1]['ticker']['data']['symbol'] == 'MSFTUSDT'
    failures.add(('MSFTUSDT', '/v5/market/tickers'))
    response = snapshot(client, tokens['access_token'], symbols=['MUUSDT', 'MSFTUSDT'], include=[])
    assert response['isError'] and response['structuredContent']['state'] == 'failed'


def test_spot_skips_derivatives_and_can_include_open_candles(markets):
    app, client, calls, _, _ = markets
    _, tokens = access(app, client)
    response = snapshot(client, tokens['access_token'], category='spot', intervals=['5'], closed_klines_only=False)
    data = response['structuredContent']
    assert data['state'] == 'complete' and not response['isError']
    assert set(data['items'][0]['unavailable']) == {'open_interest', 'funding'}
    assert not any(r.url.path.endswith(('/open-interest', '/funding/history')) for r in calls)
    assert data['items'][0]['klines']['5']['list'][-1][4] == '999'


def test_snapshot_obeys_operation_allowlist_for_each_component(markets):
    app, client, calls, _, _ = markets
    profile = app.state.store.save_profile('restricted', 'demo', 'fake', 'fake', ['bybit:read'], True)
    allowed = [lookup('GET /v5/market/' + name)['id'] for name in ('tickers', 'kline')]
    pat = app.state.oauth.tokens.create({'name': 'market-only', 'expires_at': None, 'bindings': [{
        'profile_id': profile['id'], 'environment': 'demo', 'scopes': ['bybit:read'], 'operations': allowed}]})
    response = snapshot(client, pat['token'], include=['open_interest'])
    assert response['structuredContent']['partial']
    assert 'not authorized' in response['structuredContent']['items'][0]['errors']['open_interest']['error']
    assert not any(r.url.path.endswith('/open-interest') for r in calls)


def test_snapshot_does_not_return_data_after_parent_revocation(markets):
    app, client, _, _, control = markets
    profile = app.state.store.save_profile('revocable', 'demo', 'fake', 'fake', ['bybit:read'], True)
    pat = app.state.oauth.tokens.create({'name': 'revocable', 'expires_at': None, 'bindings': [{
        'profile_id': profile['id'], 'environment': 'demo', 'scopes': ['bybit:read'], 'operations': None}]})
    control['after_response'] = lambda: app.state.oauth.tokens.revoke(pat['id'])
    response = snapshot(client, pat['token'], include=[], intervals=['60'])
    assert response['isError'] and 'items' not in response.get('structuredContent', {})


@pytest.mark.parametrize('arguments', [
    {'symbols': ['MUUSDT', 'MUUSDT']}, {'symbols': ['https://example.com']},
    {'symbols': ['MUUSDT'] * 9}, {'kline_limit': 201}, {'intervals': ['17']},
])
def test_snapshot_rejects_unbounded_or_invalid_inputs_without_network(markets, arguments):
    app, client, calls, _, _ = markets
    _, tokens = access(app, client)
    response = snapshot(client, tokens['access_token'], **arguments)
    assert response['isError'] and not calls


@pytest.mark.parametrize('start,interval,end', [
    ('2026-12-01', 'M', '2027-01-01'), ('2028-02-01', 'M', '2028-03-01'),
    ('2026-10-26', 'W', '2026-11-02'), ('2026-11-01', 'D', '2026-11-02'),
])
def test_calendar_candles_close_on_calendar_boundaries(start, interval, end):
    stamp = lambda text: int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()*1000)
    assert candle_end(stamp(start), interval) == stamp(end)


class TradeSocket:
    def __init__(self, code=0, batch=None, timeout=False):
        self.code, self.batch, self.timeout, self.sent = code, batch, timeout, []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def send(self, message):
        self.sent.append(json.loads(message))
    async def recv(self):
        request = self.sent[-1]
        if request['op'] == 'auth':
            return json.dumps({'retCode': 0, 'op': 'auth'})
        if self.timeout:
            raise asyncio.TimeoutError
        return json.dumps({'reqId': request['reqId'], 'retCode': self.code, 'retMsg': 'OK' if not self.code else 'Synthetic rejection',
                           'retExtInfo': {'list': self.batch or []}, 'data': {}})


@pytest.mark.parametrize('code', [0, 10001, 10429])
def test_ws_ack_rejection_is_an_mcp_error_and_preserves_code(markets, monkeypatch, code):
    app, client, _, _, _ = markets
    _, tokens = access(app, client, environment='mainnet')
    socket = TradeSocket(code)
    monkeypatch.setattr(BybitClient, 'clock', AsyncMock(return_value=0))
    monkeypatch.setattr('bybit_mcp.bybit.websockets.connect', lambda *a, **kw: socket)
    result = rpc(client, tokens['access_token'], 'bybit_ws_trade', {'operation': 'order.create', 'params': {
        'category': 'linear', 'symbol': 'MUUSDT', 'side': 'Buy', 'orderType': 'Market', 'qty': '0.001'}}).json()['result']
    assert result['isError'] == bool(code)
    data = result['structuredContent']
    assert data['data']['retCode'] == code and data['state'] == ('rejected' if code else 'accepted')
    assert 'fill confirmation' in data['note']


def test_ws_batch_reports_each_order_and_timeout_never_resends(markets, monkeypatch):
    app, client, _, _, _ = markets
    _, tokens = access(app, client, environment='mainnet')
    socket = TradeSocket(batch=[{'code': 0}, {'code': 110007, 'msg': 'Insufficient balance'}])
    monkeypatch.setattr(BybitClient, 'clock', AsyncMock(return_value=0))
    monkeypatch.setattr('bybit_mcp.bybit.websockets.connect', lambda *a, **kw: socket)
    order = {'symbol': 'MUUSDT', 'side': 'Buy', 'orderType': 'Market', 'qty': '0.001'}
    arguments = {'operation': 'order.create-batch', 'params': {'category': 'linear', 'request': [order, order]}}
    result = rpc(client, tokens['access_token'], 'bybit_ws_trade', arguments).json()['result']
    assert result['isError'] and result['structuredContent']['state'] == 'partially_rejected'
    assert [row['accepted'] for row in result['structuredContent']['batch_results']] == [True, False]
    socket.timeout = True
    socket.sent.clear()
    result = rpc(client, tokens['access_token'], 'bybit_ws_trade', arguments).json()['result']
    assert result['isError'] and result['structuredContent']['outcome_unknown']
    assert len(result['structuredContent']['order_links']) == 2
    assert sum(message['op'] == 'order.create-batch' for message in socket.sent) == 1
