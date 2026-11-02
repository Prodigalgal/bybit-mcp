import copy
import json

import pytest
from jsonschema import Draft202012Validator

from bybit_mcp.catalog import lookup
from scripts.generate_catalog import md_parameters, merge_schema, schema_differences


def test_current_markdown_preserves_nested_fields_and_html_required_flags():
    schema = md_parameters('''### Request Parameters
| Parameter | Required | Type | Comments |
| category | **true** | string | Product type `linear`, `option`, `spot`, `inverse` |
| request | <b>true</b> | array | Orders |
|> symbol | **true** | string | Symbol such as `BTCUSDT` |
|> positionIdx | false | integer | `0`: one way; `1`: hedge buy; `2`: hedge sell |
|> details | false | object | Extra data |
|>> value | true | string | Amount |
|> triggerBy | false | string | `LastPrice`, `MarkPrice`, `IndexPrice`. Valid for `linear`, `inverse` |
### Response Parameters
| result | true | object | Not a request field |
''')
    assert schema['required'] == ['category', 'request']
    order = schema['properties']['request']['items']
    assert order['required'] == ['symbol']
    assert order['properties']['positionIdx']['enum'] == [0, 1, 2]
    assert order['properties']['triggerBy']['enum'] == ['LastPrice', 'MarkPrice', 'IndexPrice']
    assert order['properties']['details']['required'] == ['value']
    assert 'result' not in schema['properties'] and 'symbol' not in schema['properties']


def test_markdown_overrides_stale_explorer_required_enum_and_nested_fields():
    current = md_parameters('''### Request Parameters
| category | true | string | `linear`, `option`, `spot`, `inverse` |
| request | true | array | Orders |
|> symbol | true | string | Symbol |
|> positionIdx | false | integer | `0`, `1`, `2` |
|> orderLinkId | false | string | Optional except for options |
|> closeOnTrigger | false | boolean | Reduce on trigger |
''')
    old = copy.deepcopy(current)
    old['properties']['category']['enum'] = ['linear', 'option']
    items = old['properties']['request']['items']
    items['required'].append('orderLinkId')
    items['properties']['positionIdx']['enum'] = [0]
    del items['properties']['closeOnTrigger']
    merged = merge_schema(current, old)
    assert merged == current
    differences = schema_differences(current, old)
    assert {row['parameter'] for row in differences} >= {'category', 'request[]', 'request[].positionIdx'}


@pytest.mark.parametrize('category,symbol', [('linear', 'BTCUSDT'), ('inverse', 'BTCUSD'), ('spot', 'BTCUSDT')])
@pytest.mark.parametrize('position_idx', [0, 1, 2])
def test_official_non_option_batch_examples_accept_optional_defaults(category, symbol, position_idx):
    schema = lookup('POST /v5/order/create-batch')['schema']
    args = {'category': category, 'request': [{'symbol': symbol, 'side': 'Buy', 'orderType': 'Limit',
                                             'qty': '1', 'price': '100', 'positionIdx': position_idx,
                                             'futureParameter': 'passed-through'}]}
    Draft202012Validator(schema).validate(args)
    assert 'timeInForce' not in args['request'][0] and 'orderLinkId' not in args['request'][0]


def test_option_order_link_id_is_conditional_and_batch_requires_symbol():
    schema = lookup('POST /v5/order/create-batch')['schema']
    order = {'symbol': 'BTC-25DEC26-100000-C', 'side': 'Buy', 'orderType': 'Limit', 'qty': '1', 'price': '100'}
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors({'category': 'option', 'request': [order]}))
    validator.validate({'category': 'option', 'request': [{**order, 'orderLinkId': 'client-id'}]})
    del order['symbol']
    assert list(validator.iter_errors({'category': 'linear', 'request': [order]}))
    assert list(validator.iter_errors({'category': 'linear', 'request': []}))


def test_catalog_generation_records_source_conflicts_and_current_demo_path():
    batch = lookup('POST /v5/order/create-batch')
    assert any(row['parameter'] == 'request[]' and row['constraint'] == 'required'
               for row in batch['schema_differences'])
    assert lookup('GET /v5/spot-margin-trade/state')['demo']
    assert lookup('GET /v5/market/kline')['schema']['required'] == ['symbol', 'interval']
    json.dumps(batch)


@pytest.mark.parametrize('category,maximum', [('option', 5), ('spot', 10), ('linear', 20), ('inverse', 20)])
def test_batch_limits_follow_each_product_category(category, maximum):
    validator = Draft202012Validator(lookup('POST /v5/order/create-batch')['schema'])
    order = {'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Market', 'qty': '1', 'orderLinkId': 'caller-id'}
    orders = [{**order, 'orderLinkId': f'caller-id-{index}'} for index in range(maximum + 1)]
    validator.validate({'category': category, 'request': orders[:maximum]})
    assert list(validator.iter_errors({'category': category, 'request': orders}))
