import base64
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import pytest

from bybit_mcp.catalog import lookup
from bybit_mcp.db import digest
from bybit_mcp.security import principal_var
from tests.test_service import service, register, rpc
from tests.test_trading import desk, limit


def binding(profile, scopes=None, operations=None):
    return {"profile_id": profile["id"], "environment": profile["environment"], "scopes": scopes or profile["scopes"], "operations": operations}


def profile(app, env="demo", key="one", scopes=None):
    return app.state.store.save_profile(env + " " + key, env, key, "fake-secret", scopes or ["bybit:read", "bybit:trade"], True)


def parent(app, bindings, **kw):
    return app.state.oauth.tokens.create({"name": "Authorization QA", "bindings": bindings, "expires_at": None, **kw})


def begin(app, client):
    c = register(client)
    verifier = "s" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    page = client.get("/oauth/authorize", params={"client_id": c["client_id"], "redirect_uri": c["redirect_uris"][0], "response_type": "code", "scope": "bybit:read bybit:trade", "resource": app.state.settings.resource, "code_challenge": challenge, "code_challenge_method": "S256"})
    assert page.status_code == 200
    fields = {k: re.search('name="' + k + '" value="([^"]+)"', page.text)[1] for k in ("flow_id", "csrf")}
    return c, verifier, fields, page


def consent(app, client, pat):
    c, verifier, fields, _ = begin(app, client)
    verify = client.post("/oauth/authorize", data={**fields, "decision": "verify", "authorization_token": pat["token"]})
    assert verify.status_code == 200, verify.text
    approved = client.post("/oauth/authorize", data={**fields, "decision": "allow"}, follow_redirects=False)
    assert approved.status_code == 303, approved.text
    form = {"grant_type": "authorization_code", "client_id": c["client_id"], "redirect_uri": c["redirect_uris"][0], "code_verifier": verifier, "code": parse_qs(urlparse(approved.headers["location"]).query)["code"][0]}
    return c, form


def login(app, client, pat):
    c, form = consent(app, client, pat)
    issued = client.post("/oauth/token", data=form)
    assert issued.status_code == 200, issued.text
    return c, issued.json()


def call(client, issued, name, **args):
    return rpc(client, issued["access_token"], name, args).json()["result"]


def test_token_only_consent_verifies_then_approves_without_bybit_request(service):
    app, client, calls = service
    p = profile(app)
    pat = parent(app, [binding(p, operations=[lookup("POST /v5/order/cancel")["id"]])])
    _, _, fields, page = begin(app, client)
    assert 'name="authorization_token"' in page.text
    assert all('name="' + f + '"' not in page.text for f in ("api_key", "api_secret", "environment", "scopes"))
    unverified = client.post("/oauth/authorize", data={**fields, "decision": "allow"})
    assert unverified.status_code == 400
    checked = client.post("/oauth/authorize", data={**fields, "decision": "verify", "authorization_token": pat["token"]})
    assert checked.status_code == 200 and "批准授权" in checked.text and "cancel" in checked.text and "无限期" in checked.text
    assert pat["token"] not in checked.text and "fake-secret" not in checked.text
    assert not calls
    approved = client.post("/oauth/authorize", data={**fields, "decision": "allow"}, follow_redirects=False)
    assert approved.status_code == 303 and not calls
    assert client.post("/oauth/authorize", data={**fields, "decision": "allow"}).status_code == 400


def test_admin_token_management_hash_only_and_own_csrf(service):
    app, client, _ = service
    p = profile(app)
    body = {"name": "Backend Token", "bindings": [binding(p)], "expires_at": None}
    assert client.post("/admin/api/authorization-tokens", json=body).status_code == 401
    headers = {"Authorization": "Bearer " + app.state.settings.admin_token}
    created = client.post("/admin/api/authorization-tokens", json=body, headers=headers)
    assert created.status_code == 201 and "no-store" in created.headers["cache-control"]
    value = created.json()
    listing = client.get("/admin/api/authorization-tokens", headers=headers).json()
    assert value["token"] not in json.dumps(listing) and "secret_hash" not in json.dumps(listing)
    assert value["token"] not in app.state.store.conn.execute("SELECT group_concat(data) FROM records").fetchone()[0]
    assert client.get("/admin/api/status", headers={"Authorization": "Bearer " + value["token"]}).status_code == 401
    assert not rpc(client, value["token"], "bybit_get_profile", {}).json()["result"]["isError"]
    client.post("/admin/login", data={"token": app.state.settings.admin_token})
    assert client.patch("/admin/api/authorization-tokens/" + value["id"], json={"name": "Changed"}).status_code == 403
    edited = client.patch("/admin/api/authorization-tokens/" + value["id"], json={"name": "Changed", "expires_at": time.time() + 300}, headers=headers)
    assert edited.status_code == 200 and edited.json()["name"] == "Changed"
    assert client.delete("/admin/api/authorization-tokens/" + value["id"], headers=headers).status_code == 200
    assert app.state.oauth.tokens.authenticate(value["token"]) is None


@pytest.mark.parametrize("mutation", ["revoked", "expired", "deleted_connection", "disabled_connection"])
def test_parent_changes_invalidate_access_refresh_and_pending_code(service, mutation):
    app, client, _ = service
    p = profile(app)
    pat = parent(app, [binding(p)])
    c, issued = login(app, client, pat)
    _, pending = consent(app, client, pat)
    if mutation == "revoked":
        app.state.oauth.tokens.revoke(pat["id"])
    elif mutation == "expired":
        raw = app.state.store.get("authorization_token", pat["id"])
        app.state.store.put("authorization_token", pat["id"], {**raw, "expires_at": time.time() - 1})
    elif mutation == "deleted_connection":
        app.state.store.delete("profile", p["id"])
    else:
        app.state.store.put("profile", p["id"], {**p, "enabled": False})
    assert app.state.oauth.principal(issued["access_token"]) is None
    assert client.post("/oauth/token", data=pending).json()["error"] == "invalid_grant"
    refresh = client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": c["client_id"], "refresh_token": issued["refresh_token"]})
    assert refresh.json()["error"] == "invalid_grant"


def test_multi_environment_and_key_operation_isolation_and_no_default_write(service):
    app, client, calls = service
    demo, other_demo, real, test = [profile(app, env, key) for env, key in [("demo", "one"), ("demo", "two"), ("mainnet", "real"), ("testnet", "test")]]
    wallet, create = lookup("GET /v5/account/wallet-balance")["id"], lookup("POST /v5/order/create")["id"]
    pat = parent(app, [binding(demo), binding(other_demo, ["bybit:read"], [wallet]), binding(real, ["bybit:read"], [wallet]), binding(test)], default_profile_id=demo["id"])
    _, issued = login(app, client, pat)
    connections = call(client, issued, "bybit_list_connections")["structuredContent"]["connections"]
    assert {b["profile_id"] for b in connections} == {demo["id"], other_demo["id"], real["id"], test["id"]}
    order = {"category": "linear", "symbol": "XAUUSDT", "side": "Buy", "orderType": "Limit", "qty": "0.01", "price": "99.00"}
    assert call(client, issued, "bybit_place_order", **order)["isError"]
    assert not calls  # Missing explicit connection did not send a mutation or validation request.
    for p in (real, other_demo):
        assert call(client, issued, "bybit_rest_trade", connection_id=p["id"], operation=create, params=order)["isError"]
        assert call(client, issued, "bybit_get_tickers", connection_id=p["id"], category="linear", symbol="XAUUSDT")["isError"]
        allowed = call(client, issued, "bybit_get_wallet", connection_id=p["id"], accountType="UNIFIED")
        assert not allowed["isError"], allowed
    assert not call(client, issued, "bybit_place_order", connection_id=demo["id"], **order)["isError"]
    assert not call(client, issued, "bybit_place_order", connection_id=test["id"], **order)["isError"]
    create_hosts = {r.url.host for r in calls if r.url.path == "/v5/order/create"}
    assert create_hosts == {"api-demo.bybit.com", "api-testnet.bybit.com"}
    app.state.oauth.tokens.update(pat["id"], {"bindings": [binding(real, ["bybit:read"], [wallet])], "default_profile_id": None})
    assert call(client, issued, "bybit_get_wallet", accountType="UNIFIED")["isError"]  # Never switch removed Demo default to mainnet.


def test_operation_policy_reduction_is_live_and_expansion_needs_reauthorization(service):
    app, client, _ = service
    p, added = profile(app), profile(app, "mainnet", "added")
    wallet, ticker = lookup("GET /v5/account/wallet-balance")["id"], lookup("GET /v5/market/tickers")["id"]
    pat = parent(app, [binding(p, ["bybit:read"], [wallet])])
    _, issued = login(app, client, pat)
    app.state.oauth.tokens.update(pat["id"], {"bindings": [binding(p), binding(added)]})
    assert call(client, issued, "bybit_get_tickers", category="linear")["isError"]
    assert call(client, issued, "bybit_place_order", category="linear")["isError"]
    assert len(call(client, issued, "bybit_list_connections")["structuredContent"]["connections"]) == 1
    app.state.oauth.tokens.update(pat["id"], {"bindings": [binding(p, ["bybit:read"], [ticker])]})
    assert app.state.oauth.principal(issued["access_token"]) is None  # No allowed operations left in the approved intersection.


def test_consent_policy_reduction_requires_second_approval(service):
    app, client, _ = service
    p = profile(app)
    pat = parent(app, [binding(p)])
    _, _, fields, _ = begin(app, client)
    assert client.post("/oauth/authorize", data={**fields, "decision": "verify", "authorization_token": pat["token"]}).status_code == 200
    app.state.oauth.tokens.update(pat["id"], {"bindings": [binding(p, ["bybit:read"])]})
    first = client.post("/oauth/authorize", data={**fields, "decision": "allow"}, follow_redirects=False)
    assert first.status_code == 409 and "再次批准" in first.text
    assert client.post("/oauth/authorize", data={**fields, "decision": "allow"}, follow_redirects=False).status_code == 303


def test_removed_permissions_do_not_return_to_old_grants_after_policy_expansion(service):
    app, client, _ = service
    p = profile(app)
    pat = parent(app, [binding(p)])
    c, issued = login(app, client, pat)
    _, pending = consent(app, client, pat)
    wallet = lookup("GET /v5/account/wallet-balance")["id"]
    app.state.oauth.tokens.update(pat["id"], {"bindings": [binding(p, ["bybit:read"], [wallet])]})
    app.state.oauth.tokens.update(pat["id"], {"bindings": [binding(p)]})
    assert call(client, issued, "bybit_place_order", category="linear")["isError"]
    assert call(client, issued, "bybit_get_tickers", category="linear")["isError"]
    assert not call(client, issued, "bybit_get_wallet", accountType="UNIFIED")["isError"]
    refreshed = client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": c["client_id"], "refresh_token": issued["refresh_token"]})
    assert refreshed.status_code == 200 and refreshed.json()["scope"] == "bybit:read"
    assert call(client, refreshed.json(), "bybit_get_tickers", category="linear")["isError"]
    pending_issued = client.post("/oauth/token", data=pending)
    assert pending_issued.status_code == 200 and pending_issued.json()["scope"] == "bybit:read"
    assert call(client, pending_issued.json(), "bybit_get_tickers", category="linear")["isError"]


def test_raw_trade_only_permission_allows_internal_public_instrument_validation(service):
    app, client, calls = service
    p = profile(app)
    pat = parent(app, [binding(p, ["bybit:trade"], [lookup("POST /v5/order/create")["id"]])])
    _, issued = login(app, client, pat)
    assert not call(client, issued, "bybit_get_profile")["isError"]
    assert call(client, issued, "bybit_get_instruments", category="linear", symbol="XAUUSDT")["isError"]
    result = call(client, issued, "bybit_place_order", category="linear", symbol="XAUUSDT", side="Buy", orderType="Limit", qty="0.01", price="99.00")
    assert not result["isError"], result
    assert any(r.url.path == "/v5/market/instruments-info" for r in calls)


@pytest.mark.parametrize("change", ["environment", "unknown_key", "scope", "operation_scope", "expiry", "empty_ops"])
def test_admin_rejects_mismatched_or_invalid_policy(service, change):
    app, _, _ = service
    p = profile(app)
    policy = {"name": "Invalid", "bindings": [binding(p)], "expires_at": None}
    if change == "environment": policy["bindings"][0]["environment"] = "mainnet"
    if change == "unknown_key": policy["bindings"][0]["profile_id"] = "unknown"
    if change == "scope": policy["bindings"][0]["scopes"] = ["bybit:funds"]
    if change == "operation_scope": policy["bindings"][0].update(scopes=["bybit:read"], operations=[lookup("POST /v5/order/create")["id"]])
    if change == "expiry": policy["expires_at"] = time.time() - 1
    if change == "empty_ops": policy["bindings"][0]["operations"] = []
    with pytest.raises(ValueError): app.state.oauth.tokens.create(policy)


async def test_ws_managed_cache_and_generic_rest_cannot_bypass_operation_policy(desk):
    _, p, exchange, _, app = desk
    pat = parent(app, [binding(p, ["bybit:read"], [lookup("GET /v5/market/tickers")["id"]])])
    grant = app.state.oauth.issue(p["id"], "test", ["bybit:read"], app.state.settings.resource, parent_token_id=pat["id"], bindings=[binding(p, ["bybit:read"], [lookup("GET /v5/market/tickers")["id"]])])
    ctx = principal_var.set(app.state.oauth.principal(grant["access_token"]))
    try:
        for channel in ("public", "private"):
            with pytest.raises(PermissionError): await app.state.bybit.websocket(p, channel, ["wallet"])
        with pytest.raises(PermissionError): await app.state.bybit.request(p, lookup("GET /v5/account/wallet-balance"), {"accountType": "UNIFIED"})
        assert not exchange.calls
    finally: principal_var.reset(ctx)


@pytest.mark.parametrize("mode,revoke", [("oauth", "parent"), ("oauth", "family"), ("oauth", "expire"), ("bearer", "parent"), ("bearer", "expire")])
async def test_expiry_jobs_obey_parent_and_family_revocation(desk, mode, revoke):
    trading, p, exchange, _, app = desk
    pat = parent(app, [binding(p)])
    if mode == "oauth":
        issued = app.state.oauth.issue(p["id"], "job-test", p["scopes"], app.state.settings.resource, parent_token_id=pat["id"], bindings=[binding(p)])
        principal = app.state.oauth.principal(issued["access_token"])
    else:
        principal = app.state.oauth.principal(pat["token"])
    ctx = principal_var.set(principal)
    try:
        preview = await trading.preview(p, limit(expire_seconds=30), p["scopes"])
        submitted = await trading.submit(p, preview["preview_id"], p["scopes"])
        assert submitted["state"] == "acknowledged"
    finally: principal_var.reset(ctx)
    jobs = app.state.store.list("order_expiry")
    assert jobs and jobs[0]["authorization"]["parent_token_id"] == pat["id"]
    if revoke == "parent": app.state.oauth.tokens.revoke(pat["id"])
    elif revoke == "family":
        family = app.state.store.get("family", principal["family"])
        app.state.store.put("family", principal["family"], {**family, "revoked": True})
    else:
        raw = app.state.store.get("authorization_token", pat["id"])
        app.state.store.put("authorization_token", pat["id"], {**raw, "expires_at": time.time()-1})
    app.state.store.put("order_expiry", jobs[0]["id"], {**jobs[0], "due_at": 0})
    before = len(exchange.calls)
    await trading.process_expiries()
    assert app.state.store.get("order_expiry", jobs[0]["id"])["state"] == "needs_attention"
    assert len(exchange.calls) == before


async def test_revocation_while_waiting_for_rate_limit_blocks_wire_request(desk, monkeypatch):
    _, p, exchange, _, app = desk
    pat = parent(app, [binding(p)])
    issued = app.state.oauth.issue(p["id"], "pacing-test", p["scopes"], app.state.settings.resource, parent_token_id=pat["id"], bindings=[binding(p)])
    ctx = principal_var.set(app.state.oauth.principal(issued["access_token"]))
    async def revoke_in_wait(_): app.state.oauth.tokens.revoke(pat["id"])
    monkeypatch.setattr("bybit_mcp.bybit.asyncio.sleep", revoke_in_wait)
    app.state.bybit.pacing[p["id"]] = time.monotonic()+1
    try:
        with pytest.raises(PermissionError): await app.state.bybit.request(p, lookup("GET /v5/market/tickers"), {"category": "linear"})
        assert not exchange.calls
    finally: principal_var.reset(ctx)


def test_native_bearer_needs_no_oauth_session_and_enforces_key_operations(service):
    app, client, calls = service
    demo, real = profile(app), profile(app, "mainnet", "real")
    wallet = lookup("GET /v5/account/wallet-balance")["id"]
    pat = parent(app, [binding(demo), binding(real, ["bybit:read"], [wallet])])
    issued = {"access_token": pat["token"]}
    assert app.state.oauth.principal(pat["token"])["auth_mode"] == "bearer"
    assert not app.state.store.list("family")
    assert len(call(client, issued, "bybit_list_connections")["structuredContent"]["connections"]) == 2
    assert not call(client, issued, "bybit_get_wallet", connection_id=real["id"], accountType="UNIFIED")["isError"]
    assert call(client, issued, "bybit_get_tickers", connection_id=real["id"], category="linear")["isError"]
    assert call(client, issued, "bybit_place_order", category="linear")["isError"]
    assert call(client, issued, "bybit_cancel_all_orders", connection_id=real["id"], category="linear")["isError"]
    assert all(r.method == "GET" for r in calls)


@pytest.mark.parametrize("change", ["expired", "revoked"])
def test_native_bearer_expiry_and_revocation_apply_to_next_request(service, change):
    app, client, _ = service
    p = profile(app)
    pat = parent(app, [binding(p, ["bybit:read"])])
    assert not rpc(client, pat["token"], "bybit_get_profile", {}).json()["result"]["isError"]
    if change == "revoked":
        app.state.oauth.tokens.revoke(pat["id"])
    else:
        raw = app.state.store.get("authorization_token", pat["id"])
        app.state.store.put("authorization_token", pat["id"], {**raw, "expires_at": time.time()-1})
    assert rpc(client, pat["token"], "bybit_get_profile", {}).status_code == 401
