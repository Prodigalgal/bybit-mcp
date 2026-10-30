import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from jsonschema import Draft202012Validator
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from bybit_mcp.app import create_app
from bybit_mcp.bybit import BybitError, sign
from bybit_mcp.catalog import CATALOG, ENDPOINTS, lookup
from bybit_mcp.config import SCOPES, Settings
from bybit_mcp.db import digest


@pytest.fixture
def service(tmp_path):
    calls = []
    def upstream(request):
        calls.append(request)
        path = request.url.path
        result = {}
        if path == "/v5/market/time":
            result = {"timeSecond": str(int(time.time())), "timeNano": str(time.time_ns())}
        elif path == "/v5/market/instruments-info":
            result = {"list": [{"symbol": request.url.params.get("symbol"), "settleCoin": "USDT", "status": "Trading", "lotSizeFilter": {"qtyStep": "0.001"}, "priceFilter": {"tickSize": "0.01"}}]}
        elif path == "/v5/order/create":
            result = {"orderId": "demo-order-1", "orderLinkId": json.loads(request.content).get("orderLinkId")}
        elif path == "/v5/user/create-sub-api":
            result = {"apiKey": "new-generated-key", "secret": "new-generated-secret"}
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result, "time": int(time.time() * 1000)})
    settings = Settings("http://testserver", "admin-" + "x" * 40, Fernet.generate_key().decode(), tmp_path)
    app = create_app(settings, httpx.MockTransport(upstream))
    with TestClient(app) as client:
        yield app, client, calls


def register(client, method="none"):
    r = client.post("/oauth/register", json={"client_name": "Protocol Test", "redirect_uris": ["https://client.example/callback"], "token_endpoint_auth_method": method})
    assert r.status_code == 201
    return r.json()


def authorize(app, client, scopes="bybit:read bybit:trade", method="none", environment="demo"):
    c = register(client, method)
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": c["client_id"], "redirect_uri": c["redirect_uris"][0], "response_type": "code", "scope": scopes,
              "resource": app.state.settings.resource, "state": "state-123", "code_challenge": challenge, "code_challenge_method": "S256"}
    r = client.get("/oauth/authorize", params=params)
    assert r.status_code == 200
    import re
    flow = re.search(r'name="flow_id" value="([^"]+)"', r.text)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text)[1]
    profile = app.state.store.save_profile("Test Demo", environment, "test-key", "test-secret", scopes.split(), True)
    pat = app.state.oauth.tokens.create({"name": "Test Token", "expires_at": None, "bindings": [{
        "profile_id": profile["id"], "environment": environment, "scopes": scopes.split(), "operations": None}]})
    r = client.post("/oauth/authorize", data={"flow_id": flow, "csrf": csrf, "authorization_token": pat["token"], "decision": "verify"})
    assert r.status_code == 200, r.text
    r = client.post("/oauth/authorize", data={"flow_id": flow, "csrf": csrf, "decision": "allow"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["iss"] == [app.state.settings.public_url] and q["state"] == ["state-123"]
    form = {"grant_type": "authorization_code", "client_id": c["client_id"], "redirect_uri": c["redirect_uris"][0], "code": q["code"][0], "code_verifier": verifier, "resource": app.state.settings.resource}
    if method == "client_secret_post":
        form["client_secret"] = c["client_secret"]
    return c, form


def access(app, client, scopes="bybit:read bybit:trade", environment="demo"):
    c, form = authorize(app, client, scopes, environment=environment)
    r = client.post("/oauth/token", data=form)
    assert r.status_code == 200, r.text
    return c, r.json()


def rpc(client, access_token, name, arguments):
    headers = {"Authorization": "Bearer " + access_token, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


def test_catalog_integrity_and_documented_scope():
    assert CATALOG["count"] == len(ENDPOINTS) >= 400
    assert len({(e["method"], e["path"]) for e in ENDPOINTS.values()}) == len(ENDPOINTS)
    for e in ENDPOINTS.values():
        Draft202012Validator.check_schema(e["schema"])
        assert set(e["schema"].get("required", [])) <= set(e["schema"].get("properties", {})), e["id"]
        assert e["path"].startswith("/v5/") and e["scope"] in SCOPES
        assert e["docs"].startswith("https://bybit-exchange.github.io/docs/v5/")
        assert e["schema"]["type"] == "object"
        if e["method"] != "GET":
            assert e["scope"] != "bybit:read"
    assert lookup("POST /v5/order/create")["demo"]
    assert not lookup("POST /v5/asset/withdraw/create")["demo"]


def test_metadata_and_unauthenticated_challenge(service):
    app, client, _ = service
    r = client.post("/mcp", json={})
    assert r.status_code == 401 and 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == app.state.settings.resource
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["client_id_metadata_document_supported"]


def test_ui_assets_are_content_addressed_and_revision_checked(service):
    import hashlib
    import re
    app, client, _ = service
    login = client.get("/admin")
    css_path = re.search(r'href="(/assets/[a-f0-9]+/app.css)"', login.text)[1]
    response = client.get(css_path)
    assert response.status_code == 200
    revision = css_path.split("/")[2]
    assert hashlib.sha256(response.content).hexdigest().startswith(revision)
    assert "immutable" in response.headers["cache-control"]
    assert client.get(css_path.replace(revision, "wrong-revision")).status_code == 404
    client.post("/admin/login", data={"token": app.state.settings.admin_token})
    html = client.get("/admin").text
    assert re.search(r'src="/assets/[a-f0-9]+/app.js"', html)
    assert re.search(r'src="/assets/[a-f0-9]+/trading.js"', html)
    assert '/static/app.js' not in html


def test_registration_rejects_unsafe_redirects(service):
    _, client, _ = service
    for uri in ["http://evil.example/callback", "https://client.example/callback#fragment", "https://user:pw@client.example/callback"]:
        assert client.post("/oauth/register", json={"redirect_uris": [uri]}).status_code == 400


def test_authorize_requires_pkce_exact_redirect_and_resource(service):
    app, client, _ = service
    c = register(client)
    params = {"client_id": c["client_id"], "redirect_uri": "https://attacker.example/cb"}
    assert client.get("/oauth/authorize", params=params).status_code == 400
    params["redirect_uri"] = c["redirect_uris"][0]
    params["response_type"] = "code"
    r = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert "error=invalid_request" in r.headers["location"]
    params.update(code_challenge="x" * 43, code_challenge_method="S256", resource="https://wrong.example/mcp")
    r = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert "error=invalid_target" in r.headers["location"]


def test_pkce_replay_and_audience(service):
    app, client, _ = service
    _, form = authorize(app, client)
    assert client.post("/oauth/token", data={**form, "code_verifier": "z" * 64}).status_code == 400
    assert client.post("/oauth/token", data={**form, "resource": "https://wrong.example/mcp"}).json()["error"] == "invalid_target"
    r = client.post("/oauth/token", data=form)
    assert r.status_code == 200
    assert client.post("/oauth/token", data=form).json()["error"] == "invalid_grant"
    grant = app.state.store.get("access", digest(r.json()["access_token"]))
    assert grant["audience"] == app.state.settings.resource


def test_confidential_client_enforces_secret(service):
    app, client, _ = service
    c, form = authorize(app, client, method="client_secret_post")
    assert client.post("/oauth/token", data={**form, "client_secret": "wrong"}).status_code == 401
    assert client.post("/oauth/token", data=form).status_code == 200
    assert "secret_hash" not in c


def test_confidential_basic_client_and_post_auth_are_not_interchangeable(service):
    app, client, _ = service
    c, form = authorize(app, client, method="client_secret_basic")
    assert client.post("/oauth/token", data={**form, "client_secret": c["client_secret"]}).status_code == 401
    r = client.post("/oauth/token", data=form, auth=(c["client_id"], c["client_secret"]))
    assert r.status_code == 200


def test_rsa_signature_verifies_with_public_key():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    signature, kind = sign(pem, "Bybit wire signing test")
    assert kind == "RSA"
    key.public_key().verify(base64.b64decode(signature), b"Bybit wire signing test", padding.PKCS1v15(), hashes.SHA256())


def test_refresh_rotation_reuse_revokes_family(service):
    app, client, _ = service
    c, tokens = access(app, client)
    form = {"grant_type": "refresh_token", "client_id": c["client_id"], "refresh_token": tokens["refresh_token"], "resource": app.state.settings.resource}
    r = client.post("/oauth/token", data=form)
    assert r.status_code == 200 and r.json()["refresh_token"] != tokens["refresh_token"]
    assert client.post("/oauth/token", data=form).status_code == 400
    assert app.state.oauth.principal(r.json()["access_token"]) is None


def test_refresh_cannot_increase_scope(service):
    app, client, _ = service
    c, tokens = access(app, client, "bybit:read")
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": c["client_id"], "refresh_token": tokens["refresh_token"], "scope": "bybit:read bybit:trade"})
    assert r.json()["error"] == "invalid_scope"


def test_token_revocation_and_expiry(service):
    app, client, _ = service
    c, tokens = access(app, client)
    assert client.post("/oauth/revoke", data={"client_id": c["client_id"], "token": tokens["refresh_token"]}).status_code == 200
    assert app.state.oauth.principal(tokens["access_token"]) is None
    app.state.store.put("access", digest("expired"), {"audience": app.state.settings.resource}, ttl=-1)
    assert app.state.oauth.principal("expired") is None


def test_mcp_initialize_list_profile_and_scope_challenge(service):
    app, client, _ = service
    _, tokens = access(app, client, "bybit:read")
    headers = {"Authorization": "Bearer " + tokens["access_token"], "Accept": "application/json, text/event-stream"}
    r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "test", "version": "1"}, "capabilities": {}}})
    assert r.status_code == 200, r.text
    assert "tools" in r.json()["result"]["capabilities"]
    tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    profile_tool = next(t for t in tools if t["name"] == "bybit_get_profile")
    assert profile_tool["_meta"]["openai/profile"] is True
    assert profile_tool["securitySchemes"][0]["type"] == "oauth2"
    r = rpc(client, tokens["access_token"], "bybit_get_profile", {}).json()["result"]
    assert r["structuredContent"]["environment"] == "demo"
    assert "test-key" not in json.dumps(r) and "test-secret" not in json.dumps(r)
    r = rpc(client, tokens["access_token"], "bybit_rest_trade", {"operation": "POST /v5/order/cancel-all", "params": {"category": "linear", "symbol": "BTCUSDT"}}).json()["result"]
    assert r["isError"] and "insufficient_scope" in r["_meta"]["mcp/www_authenticate"][0]


def test_mcp_origin_validation(service):
    app, client, _ = service
    _, tokens = access(app, client)
    r = client.post("/mcp", headers={"Authorization": "Bearer " + tokens["access_token"], "Origin": "https://attacker.example", "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403


def test_demo_order_signing_and_no_mainnet_fallback(service):
    app, client, calls = service
    _, tokens = access(app, client)
    r = rpc(client, tokens["access_token"], "bybit_place_order", {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Limit", "qty": "0.001", "price": "100.00"})
    result = r.json()["result"]
    assert not result.get("isError"), result
    assert result["structuredContent"]["submitted_order_links"][0].startswith("mcp-")
    order = next(r for r in calls if r.url.path == "/v5/order/create")
    assert order.url.host == "api-demo.bybit.com"
    assert next(r for r in calls if r.url.path == "/v5/market/instruments-info").url.host == "api.bybit.com"
    headers = order.headers
    expected, _ = sign("test-secret", headers["X-BAPI-TIMESTAMP"] + "test-key" + "5000" + order.content.decode())
    assert headers["X-BAPI-SIGN"] == expected
    result = rpc(client, tokens["access_token"], "bybit_rest_account", {"operation": "POST /v5/user/create-sub-member", "params": {}}).json()["result"]
    assert result["isError"]


@pytest.mark.parametrize("params", [
    {"category": "spot", "symbol": "BTCUSDT", "qty": "1"},
    {"category": "linear", "symbol": "BTCUSDC", "qty": "1"},
    {"category": "linear", "symbol": "BTCUSDT", "qty": "0.0001"},
])
def test_usdt_restriction_and_precision(service, params):
    app, client, calls = service
    _, tokens = access(app, client)
    args = {"side": "Buy", "orderType": "Market", **params}
    result = rpc(client, tokens["access_token"], "bybit_place_order", args).json()["result"]
    assert result["isError"]
    assert not any(c.url.path == "/v5/order/create" for c in calls)


def test_demo_ws_trade_rejected_before_send(service):
    app, client, _ = service
    _, tokens = access(app, client)
    result = rpc(client, tokens["access_token"], "bybit_ws_trade", {"operation": "order.create", "params": {}}).json()["result"]
    assert result["isError"] and "Demo" in result["content"][0]["text"]


def test_key_encryption_admin_token_csrf_and_no_leak(service):
    app, client, _ = service
    _, tokens = access(app, client)
    p = app.state.store.list("profile")[0]
    assert p["api_key"] != "test-key" and p["api_secret"] != "test-secret"
    assert b"test-secret" not in (app.state.settings.data_dir / "bybit-mcp.db").read_bytes()
    assert client.get("/admin/api/profiles").status_code == 401
    assert client.post("/admin/login", data={"token": "wrong"}).status_code == 401
    client.post("/admin/login", data={"token": app.state.settings.admin_token})
    r = client.get("/admin/api/profiles")
    assert r.status_code == 200 and "test-secret" not in r.text and "test-key" not in r.text
    assert client.delete("/admin/api/profiles/" + p["id"]).status_code == 403
    assert client.delete("/admin/api/profiles/" + p["id"], headers={"Authorization": "Bearer " + app.state.settings.admin_token}).status_code == 200
    assert app.state.oauth.principal(tokens["access_token"]) is None


def test_oauth_consent_csrf_and_no_secret_echo(service):
    _, client, _ = service
    r = client.post("/oauth/authorize", data={"flow_id": "invalid", "api_secret": "never-echo-this"})
    assert r.status_code == 400 and "never-echo-this" not in r.text


def test_admin_token_rotation_invalidates_existing_browser_sessions(service):
    app, client, _ = service
    old = app.state.settings.admin_token
    client.post("/admin/login", data={"token": old})
    assert client.get("/admin/api/status").status_code == 200
    app.state.settings.admin_token = "rotated-" + "z" * 40
    assert client.get("/admin/api/status").status_code == 401
    assert client.get("/admin/api/status", headers={"Authorization": "Bearer " + old}).status_code == 401


def test_disabled_profile_immediately_revokes_access(service):
    app, client, _ = service
    _, tokens = access(app, client)
    p = app.state.store.list("profile")[0]
    client.patch("/admin/api/profiles/" + p["id"], json={"enabled": False}, headers={"Authorization": "Bearer " + app.state.settings.admin_token})
    assert rpc(client, tokens["access_token"], "bybit_get_profile", {}).status_code == 401


def test_connection_isolation_between_stateless_requests(service):
    app, client, _ = service
    _, demo = access(app, client, environment="demo")
    _, mainnet = access(app, client, environment="mainnet")
    a = rpc(client, demo["access_token"], "bybit_get_profile", {}).json()["result"]["structuredContent"]
    b = rpc(client, mainnet["access_token"], "bybit_get_profile", {}).json()["result"]["structuredContent"]
    c = rpc(client, demo["access_token"], "bybit_get_profile", {}).json()["result"]["structuredContent"]
    assert a["id"] != b["id"] and a == c
    assert a["environment"] == "demo" and b["environment"] == "mainnet"


def test_malformed_admin_json_and_registration(service):
    app, client, _ = service
    assert client.post("/oauth/register", json=[]).status_code == 400
    r = client.post("/admin/api/profiles", json=[], headers={"Authorization": "Bearer " + app.state.settings.admin_token})
    assert r.status_code == 400 and r.json()["error"] == "JSON object required"


def test_new_api_credentials_use_encrypted_one_time_admin_download(service):
    app, client, _ = service
    p = app.state.store.save_profile("master", "mainnet", "master-key", "master-secret", ["bybit:read", "bybit:credentials"], False)
    headers = {"Authorization": "Bearer " + app.state.settings.admin_token}
    r = client.post("/admin/api/execute", headers=headers, json={"profile_id": p["id"], "operation": "POST /v5/user/create-sub-api", "params": {"subuid": 123, "readOnly": 1, "permissions": {"ContractTrade": ["Order"]}}, "confirm_environment": "mainnet"})
    assert r.status_code == 200, r.text
    assert "new-generated-key" not in r.text and "new-generated-secret" not in r.text
    ident = r.json()["sensitive_result"]["id"]
    item = app.state.store.get("secret_result", ident)
    assert "new-generated-secret" not in json.dumps(item)
    assert client.get("/admin/api/secret-results/" + ident).status_code == 401
    download = client.get("/admin/api/secret-results/" + ident, headers=headers)
    assert download.json()["result"]["secret"] == "new-generated-secret"
    assert client.get("/admin/api/secret-results/" + ident, headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_mutation_timeout_never_retried(tmp_path):
    calls = []
    def fail(request):
        calls.append(request.url.path)
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "time": int(time.time() * 1000)})
        raise httpx.ReadTimeout("timeout", request=request)
    s = Settings("http://testserver", "x" * 40, Fernet.generate_key().decode(), tmp_path)
    app = create_app(s, httpx.MockTransport(fail))
    p = app.state.store.save_profile("test", "demo", "key", "secret", ["bybit:read", "bybit:trade"], False)
    with pytest.raises(BybitError) as err:
        await app.state.bybit.request(p, lookup("POST /v5/order/create"), {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "1"})
    assert err.value.ambiguous and err.value.order_links
    assert calls.count("/v5/order/create") == 1
    await app.state.bybit.close()
