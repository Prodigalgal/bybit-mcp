import json

import pytest

from bybit_mcp.catalog import lookup
from tests.test_service import service, rpc
from tests.test_authorization import binding, parent, login, call


def public_profile(app, client):
    response = client.post('/admin/api/profiles', json={'name': '主网行情', 'public_only': True},
                           headers={'Authorization': 'Bearer ' + app.state.settings.admin_token})
    assert response.status_code == 200, response.text
    return response.json()


def test_keyless_connection_creation_saves_no_credentials_or_exchange_account_request(service):
    app, client, requests = service
    profile = public_profile(app, client)
    assert not requests
    stored = app.state.store.profile(profile['id'])
    assert 'api_key' not in stored and 'api_secret' not in stored
    assert profile['public_only'] and profile['environment'] == profile['market_data_environment'] == 'mainnet'
    assert profile['account_environment'] is None and profile['scopes'] == ['bybit:read']
    response = client.post('/admin/api/profiles/' + profile['id'] + '/test',
                           headers={'Authorization': 'Bearer ' + app.state.settings.admin_token})
    assert response.status_code == 200
    assert requests[-1].url.path == '/v5/market/time' and requests[-1].url.host == 'api.bybit.com'
    assert not any(header.startswith('x-bapi-') for header in requests[-1].headers)


@pytest.mark.parametrize('changes', [{'environment': 'testnet'}, {'api_key': 'unexpected'},
                                    {'api_secret': 'unexpected'}, {'scopes': ['bybit:read', 'bybit:trade']}])
def test_keyless_creation_rejects_private_credentials_or_other_environments(service, changes):
    app, client, requests = service
    response = client.post('/admin/api/profiles', json={'public_only': True, **changes},
                           headers={'Authorization': 'Bearer ' + app.state.settings.admin_token})
    assert response.status_code == 400 and not requests


def test_keyless_oauth_authorizes_public_data_but_never_accounts_or_trading(service):
    app, client, requests = service
    profile = public_profile(app, client)
    pat = parent(app, [binding(profile)])
    _, issued = login(app, client, pat)
    quote = call(client, issued, 'bybit_get_tickers', category='linear', symbol='MUUSDT')
    assert not quote['isError']
    assert quote['structuredContent']['market_data_environment'] == 'mainnet'
    assert quote['structuredContent']['account_environment'] is None
    assert quote['structuredContent']['public_only']
    for name, arguments in [
        ('bybit_get_wallet', {'accountType': 'UNIFIED'}),
        ('bybit_rest_read', {'operation': 'GET /v5/account/info', 'params': {}}),
        ('bybit_private_stream', {'topics': ['wallet']}),
        ('bybit_ws_trade', {'operation': 'order.create', 'params': {}}),
    ]:
        assert call(client, issued, name, **arguments)['isError']
    assert len(requests) == 1 and requests[0].url.path == '/v5/market/tickers'
    info = call(client, issued, 'bybit_get_profile')['structuredContent']
    assert info['public_only'] and info['account_environment'] is None
    assert all(lookup(operation)['public'] for operation in info['operations'] if operation != 'ws:public')


def test_public_connection_permissions_cannot_be_expanded_by_admin_or_token(service):
    app, client, requests = service
    profile = public_profile(app, client)
    headers = {'Authorization': 'Bearer ' + app.state.settings.admin_token}
    assert client.patch('/admin/api/profiles/' + profile['id'], json={'scopes': ['bybit:read', 'bybit:trade']}, headers=headers).status_code == 400
    assert client.post('/admin/api/execute', json={'profile_id': profile['id'], 'operation': 'GET /v5/account/info', 'params': {}}, headers=headers).status_code == 403
    with pytest.raises(ValueError):
        parent(app, [binding(profile, operations=[lookup('GET /v5/account/info')['id']])])
    assert not requests


def test_one_token_selects_each_key_without_escalation_or_environment_fallback(service):
    app, client, requests = service
    public = public_profile(app, client)
    demo = app.state.store.save_profile('模拟 Key A', 'demo', 'demo-synthetic', 'fake-secret', ['bybit:read', 'bybit:trade'], True)
    mainnet = app.state.store.save_profile('实盘 Key B', 'mainnet', 'mainnet-synthetic', 'fake-secret', ['bybit:read', 'bybit:trade'], True)
    pat = parent(app, [binding(public), binding(demo), binding(mainnet)], default_profile_id=public['id'])
    _, issued = login(app, client, pat)
    connections = call(client, issued, 'bybit_list_connections')['structuredContent']['connections']
    assert len(connections) == 3
    assert next(c for c in connections if c['profile_id'] == public['id'])['public_only']
    assert 'fake-secret' not in json.dumps(connections) and 'mainnet-synthetic' not in json.dumps(connections)
    quote = call(client, issued, 'bybit_get_tickers', category='linear', symbol='MUUSDT')['structuredContent']
    assert quote['public_only'] and quote['market_data_environment'] == 'mainnet'
    order = {'category': 'linear', 'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Market', 'qty': '0.001'}
    before = len(requests)
    assert call(client, issued, 'bybit_place_order', **order)['isError']
    assert call(client, issued, 'bybit_place_order', connection_id=public['id'], **order)['isError']
    assert len(requests) == before
    for profile, host in [(demo, 'api-demo.bybit.com'), (mainnet, 'api.bybit.com')]:
        response = call(client, issued, 'bybit_place_order', connection_id=profile['id'], **order)
        assert not response['isError'], response
        assert response['structuredContent']['account_environment'] == profile['environment']
        request = [r for r in requests if r.url.path == '/v5/order/create'][-1]
        assert request.url.host == host and request.headers['X-BAPI-API-KEY'] == ('demo-synthetic' if profile is demo else 'mainnet-synthetic')
    app.state.oauth.tokens.update(pat['id'], {'bindings': [binding(public), binding(demo), binding(mainnet, ['bybit:read'])], 'default_profile_id': public['id']})
    before = len(requests)
    assert call(client, issued, 'bybit_place_order', connection_id=mainnet['id'], **order)['isError']
    assert len(requests) == before
    assert not call(client, issued, 'bybit_place_order', connection_id=demo['id'], **order)['isError']


def test_public_consent_displays_quote_source_without_claiming_an_account(service):
    app, client, _ = service
    profile = public_profile(app, client)
    pat = parent(app, [binding(profile)])
    from tests.test_authorization import begin
    _, _, fields, _ = begin(app, client)
    checked = client.post('/oauth/authorize', data={**fields, 'decision': 'verify', 'authorization_token': pat['token']})
    assert checked.status_code == 200
    assert '主网公开数据 · 免 Key' in checked.text and '无账户及交易权限' in checked.text
    assert 'GET /v5/account/info' not in checked.text and 'POST /v5/order/create' not in checked.text
