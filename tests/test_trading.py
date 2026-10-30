import asyncio
import copy
import time
from decimal import Decimal

import httpx
import pytest
from cryptography.fernet import Fernet

from bybit_mcp.app import create_app
from bybit_mcp.config import Settings
from bybit_mcp.db import Store
from bybit_mcp.trading import Trading
from tests.trading_fixtures import Exchange


@pytest.fixture
async def desk(tmp_path):
    exchange = Exchange()
    settings = Settings("http://testserver", "admin-" + "x" * 40, Fernet.generate_key().decode(), tmp_path)
    app = create_app(settings, httpx.MockTransport(exchange.handle))
    profile = app.state.store.save_profile("Demo test", "demo", "test-key", "test-secret", ["bybit:read", "bybit:trade", "bybit:funds"], True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Authorization": "Bearer " + settings.admin_token}) as http:
        yield app.state.trading, profile, exchange, http, app
    await app.state.bybit.close()
    app.state.store.conn.close()


def limit(**changes):
    return {"symbol": "XAUUSDT", "kind": "limit", "side": "Buy", "amount_mode": "qty", "amount": "0.1", "price": "99.00", **changes}


def context(exchange):
    return {"symbol": "XAUUSDT", "instrument": exchange.instrument, "ticker": exchange.ticker, "positions": [exchange.position()]}


@pytest.mark.parametrize("draft,expected", [
    (limit(), {"qty": "0.1", "price": "99.00", "orderType": "Limit", "timeInForce": "PostOnly"}),
    (limit(kind="conditional_limit", trigger_price="101", trigger_by="MarkPrice"), {"triggerDirection": 1, "triggerBy": "MarkPrice", "triggerPrice": "101"}),
    (limit(kind="conditional_market", trigger_price="98", trigger_by="LastPrice"), {"triggerDirection": 2, "orderType": "Market", "timeInForce": "IOC"}),
])
def test_native_order_mapping(draft, expected):
    params, _ = Trading.normalize(draft, context(Exchange()))
    for key, value in expected.items():
        assert params[key] == value


def test_margin_budget_uses_actual_account_leverage_and_floors_quantity():
    params, summary = Trading.normalize(limit(amount_mode="margin", amount="20", price="100"), context(Exchange()))
    assert params["qty"] == "10"
    assert summary["notional_usdt"] == "1000"
    assert summary["actual_leverage"] == "50"
    assert summary["estimated_initial_margin_usdt"] == "20"
    assert summary["pnl_at_one_percent_usdt"] == "10.00"


def test_percentage_brackets_for_long_and_short():
    params, _ = Trading.normalize(limit(price="100", take_profit={"value": "1", "unit": "percent"}, stop_loss={"value": "0.5", "unit": "percent"}), context(Exchange()))
    assert Decimal(params["takeProfit"]) == Decimal("101") and Decimal(params["stopLoss"]) == Decimal("99.5")
    params, _ = Trading.normalize(limit(side="Sell", price="100", take_profit={"value": "1", "unit": "percent"}, stop_loss={"value": "0.5", "unit": "percent"}), context(Exchange()))
    assert Decimal(params["takeProfit"]) == Decimal("99") and Decimal(params["stopLoss"]) == Decimal("100.5")


@pytest.mark.parametrize("changes", [
    {"amount": "0.0001"}, {"price": "99.001"}, {"amount": "-1"}, {"amount": "NaN"},
    {"kind": "conditional_limit", "trigger_price": "99", "trigger_direction": 1},
    {"kind": "conditional_market", "trigger_price": "100"},
    {"reduce_only": True},
    {"take_profit": {"value": "95"}},
    {"take_profit": {"value": "101", "order_type": "Limit", "limit_price": "102"}, "tpsl_mode": "Full"},
    {"kind": "market", "expire_seconds": 60},
    {"slippage": "0.2"}, {"expire_seconds": 1},
    {"stoploss": "90"}, {"reduce_only": "false"}, {"position_idx": True},
    {"take_profit": "101"}, {"take_profit": {"value": "101", "triger_by": "MarkPrice"}},
    {"close_on_trigger": True},
])
def test_invalid_financial_parameters_rejected_before_placement(changes):
    with pytest.raises(ValueError):
        Trading.normalize(limit(**changes), context(Exchange()))


async def test_durable_exactly_once_submission_under_concurrent_calls(desk):
    trading, p, exchange, _, _ = desk
    plan = await trading.preview(p, limit())
    a, b = await asyncio.gather(trading.submit(p, plan["preview_id"]), trading.submit(p, plan["preview_id"]))
    assert len([r for r in exchange.calls if r.url.path == "/v5/order/create"]) == 1
    assert {a["state"], b["state"]} <= {"acknowledged", "submitting"}
    assert a["orders"][0]["orderLinkId"] == b["orders"][0]["orderLinkId"]
    assert (await trading.submit(p, plan["preview_id"]))["replayed"]


async def test_lost_acknowledgement_is_reconciled_without_resending(desk):
    trading, p, exchange, _, _ = desk
    plan = await trading.preview(p, limit())
    exchange.placement_timeout = exchange.timeout_after_accept = True
    result = await trading.submit(p, plan["preview_id"])
    assert result["state"] == "unknown"
    assert (await trading.submit(p, plan["preview_id"]))["state"] == "unknown"
    checked = await trading.reconcile(p, plan["preview_id"])
    assert checked["state"] == "confirmed" and checked["observed"][0]["found"]
    assert len([r for r in exchange.calls if r.url.path == "/v5/order/create"]) == 1


async def test_missing_order_does_not_prove_failure_or_allow_resubmission(desk):
    trading, p, exchange, _, _ = desk
    plan = await trading.preview(p, limit())
    exchange.placement_timeout = True
    await trading.submit(p, plan["preview_id"])
    checked = await trading.reconcile(p, plan["preview_id"])
    assert checked["state"] == "unknown" and not checked["observed"][0]["found"]
    await trading.submit(p, plan["preview_id"])
    assert len([r for r in exchange.calls if r.url.path == "/v5/order/create"]) == 1


async def test_preview_bound_to_profile_and_expiry(desk):
    trading, p, _, _, app = desk
    plan = await trading.preview(p, limit())
    other = app.state.store.save_profile("other", "demo", "different-key", "secret", p["scopes"], True)
    with pytest.raises(ValueError):
        await trading.submit(other, plan["preview_id"])
    plan["expires_at"] = time.time() - 1
    app.state.store.put("order_preview", plan["preview_id"], plan)
    with pytest.raises(ValueError):
        await trading.submit(p, plan["preview_id"])


async def test_leverage_change_requires_new_margin_preview(desk):
    trading, p, exchange, _, _ = desk
    plan = await trading.preview(p, limit(amount_mode="margin", amount="20"))
    exchange.leverage = "100"
    result = await trading.submit(p, plan["preview_id"])
    assert result["state"] == "rejected" and "杠杆" in result["error"]
    assert not any(r.url.path == "/v5/order/create" for r in exchange.calls)


async def test_batch_partial_failure_reports_every_item(desk):
    trading, p, exchange, _, _ = desk
    exchange.batch_fail_index = 1
    plan = await trading.preview(p, {"symbol": "XAUUSDT", "orders": [limit(), limit(price="98")]})
    result = await trading.submit(p, plan["preview_id"])
    assert result["state"] == "partially_rejected"
    assert [r["accepted"] for r in result["batch_results"]] == [True, False]
    assert len(exchange.orders) == 1


async def test_expiry_persists_restart_and_cancels_only_pending_order(desk):
    trading, p, exchange, _, app = desk
    plan = await trading.preview(p, limit(expire_seconds=60))
    await trading.submit(p, plan["preview_id"])
    job = app.state.store.list("order_expiry")[0]
    job["due_at"] = time.time() - 1
    app.state.store.put("order_expiry", job["id"], job)
    second_store = Store(app.state.settings.data_dir / "bybit-mcp.db", app.state.settings.encryption_key)
    restarted = Trading(trading.client, second_store)
    await restarted.process_expiries()
    assert next(iter(exchange.orders.values()))["orderStatus"] == "Cancelled"
    job = second_store.get("order_expiry", job["id"])
    job["due_at"] = time.time() - 1
    second_store.put("order_expiry", job["order_link_id"], job)
    await restarted.process_expiries()
    assert second_store.get("order_expiry", job["order_link_id"])["state"] == "completed"
    assert not any(r.url.path == "/v5/order/create" and b'"reduceOnly":true' in r.content for r in exchange.calls)
    second_store.conn.close()


async def test_expiry_stops_after_permission_revocation(desk):
    trading, p, exchange, _, app = desk
    plan = await trading.preview(p, limit(expire_seconds=60))
    await trading.submit(p, plan["preview_id"])
    job = app.state.store.list("order_expiry")[0]
    job["due_at"] = time.time() - 1
    app.state.store.put("order_expiry", job["id"], job)
    saved = app.state.store.get("profile", p["id"]); saved["enabled"] = False
    app.state.store.put("profile", p["id"], saved)
    await trading.process_expiries()
    assert app.state.store.get("order_expiry", job["id"])["state"] == "needs_attention"
    assert not any(r.url.path == "/v5/order/cancel" for r in exchange.calls)


async def test_order_amend_and_cancel_confirm_observed_state(desk):
    trading, p, exchange, _, _ = desk
    plan = await trading.preview(p, limit())
    await trading.submit(p, plan["preview_id"])
    ident = next(iter(exchange.orders))
    changed = await trading.mutate_order(p, "amend", "XAUUSDT", ident, {"price": "98.50", "qty": "0.2"})
    assert changed["observed"]["price"] == "98.50"
    cancelled = await trading.mutate_order(p, "cancel", "XAUUSDT", ident)
    assert cancelled["observed"]["orderStatus"] == "Cancelled"
    saved = trading.store.get("order_submission", plan["preview_id"])
    assert saved["state"] == "confirmed" and saved["observed"][0]["order"]["orderStatus"] == "Cancelled"
    stale = {**cancelled["observed"], "orderStatus": "New", "updatedTime": "1"}
    trading.observe_orders(p, [stale])
    assert trading.store.get("order_submission", plan["preview_id"])["observed"][0]["order"]["orderStatus"] == "Cancelled"


async def test_admin_close_preview_uses_latest_position_and_reduce_only(desk):
    _, p, exchange, http, _ = desk
    exchange.position_size = "0.5"
    r = await http.post("/admin/api/trading/close-preview", json={"profile_id": p["id"], "symbol": "XAUUSDT", "position_idx": 0, "percent": 50})
    assert r.status_code == 200, r.text
    order = r.json()["orders"][0]
    assert order["reduceOnly"] and order["side"] == "Sell" and Decimal(order["qty"]) == Decimal("0.25")
    assert not any(r.url.path == "/v5/order/create" for r in exchange.calls)


async def test_admin_trade_submission_requires_matching_environment(desk):
    _, p, _, http, _ = desk
    r = await http.post("/admin/api/trading/submit", json={"profile_id": p["id"], "preview_id": "x", "confirm_environment": "mainnet"})
    assert r.status_code == 400


async def test_stops_validation_and_batch_cancel_partial_results(desk):
    trading, p, exchange, http, _ = desk
    exchange.position_size = "1"
    r = await http.post("/admin/api/trading/action", json={"profile_id": p["id"], "symbol": "XAUUSDT", "action": "stops", "confirm_environment": "demo", "params": {"positionIdx": 0, "tpslMode": "Partial", "tpSize": "0.1", "slSize": "0.2", "takeProfit": "110"}})
    assert r.status_code == 400
    plan = await trading.preview(p, limit())
    await trading.submit(p, plan["preview_id"])
    ident = next(iter(exchange.orders))
    r = await http.post("/admin/api/trading/action", json={"profile_id": p["id"], "symbol": "XAUUSDT", "action": "batch_cancel", "confirm_environment": "demo", "orders": [{"orderId": ident}, {"orderId": "missing"}]})
    assert r.status_code == 200, r.text
    assert [i["code"] for i in r.json()["items"]] == [0, 110001]


async def test_leverage_update_uses_official_parameter_names_and_reads_back(desk):
    _, p, exchange, http, _ = desk
    r = await http.post("/admin/api/trading/action", json={"profile_id": p["id"], "symbol": "XAUUSDT", "action": "leverage", "confirm_environment": "demo", "buy_leverage": "20", "sell_leverage": "20"})
    assert r.status_code == 200, r.text
    assert r.json()["positions"][0]["leverage"] == "20"
    assert exchange.leverage == "20"
