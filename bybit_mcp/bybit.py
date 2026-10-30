import asyncio
import base64
import copy
import hashlib
import hmac
import json
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from jsonschema import Draft202012Validator

from .catalog import BY_PATH
from .security import enforce

HOSTS = {"mainnet": "https://api.bybit.com", "testnet": "https://api-testnet.bybit.com", "demo": "https://api-demo.bybit.com"}
WS_HOSTS = {"mainnet": "wss://stream.bybit.com", "testnet": "wss://stream-testnet.bybit.com", "demo": "wss://stream-demo.bybit.com"}
SECRET_FIELDS = {"secret", "apisecret", "apikey", "secretkey", "password", "privatekey", "signature", "x-bapi-sign", "x-bapi-api-key"}

def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower().replace("_", "") in SECRET_FIELDS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


class BybitError(Exception):
    def __init__(self, message, code=None, ambiguous=False):
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


def sign(secret, payload):
    if secret.lstrip().startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(secret.encode(), password=None)
        return base64.b64encode(key.sign(payload.encode(), padding.PKCS1v15(), hashes.SHA256())).decode(), "RSA"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest(), "HMAC"


class BybitClient:
    def __init__(self, settings, store, transport=None):
        self.settings, self.store = settings, store
        self.http = httpx.AsyncClient(timeout=20, proxy=settings.proxy, transport=transport,
                                      trust_env=False, follow_redirects=False,
                                      limits=httpx.Limits(max_connections=30, max_keepalive_connections=10))
        self.clocks = {}
        self.limiter = asyncio.Semaphore(8)
        self.pacing = {}
        self.pacing_lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    async def clock(self, env):
        old = self.clocks.get(env)
        if old and time.monotonic() - old[1] < 300:
            return old[0]
        start = time.time() * 1000
        r = await self.http.get(HOSTS[env] + "/v5/market/time")
        r.raise_for_status()
        body = r.json()
        if body.get("retCode") != 0:
            raise BybitError("Could not synchronize Bybit server time")
        server = int(body.get("time") or int(body["result"]["timeSecond"]) * 1000)
        offset = server - (start + time.time() * 1000) / 2
        self.clocks[env] = (offset, time.monotonic())
        return offset

    async def verify_credentials(self, env, key, secret):
        if env not in HOSTS or not key or not secret:
            raise ValueError("请选择环境并填写 API Key 与 API Secret")
        profile = {"id": "validation", "environment": env, "api_key": self.store.encrypt(key), "api_secret": self.store.encrypt(secret)}
        return await self._request(profile, BY_PATH[("GET", "/v5/account/info")], {})

    async def _request(self, profile, endpoint, params, internal=False):
        enforce(profile, None if internal else endpoint["id"], None if internal else endpoint["scope"])
        env = profile["environment"]
        if env not in HOSTS:
            raise ValueError("Unknown Bybit environment")
        public = endpoint["public"]
        # Demo public streams/quotes share mainnet; signed calls NEVER fall back.
        host = HOSTS["mainnet" if env == "demo" and public else env]
        method, path = endpoint["method"], endpoint["path"]
        if method == "GET":
            def val(v):
                return str(v).lower() if isinstance(v, bool) else json.dumps(v, separators=(",", ":")) if isinstance(v, (list, dict)) else str(v)
            query = urlencode([(k, val(v)) for k, v in sorted(params.items()) if v is not None])
            body, url = "", host + path + ("?" + query if query else "")
            payload = query
        else:
            body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
            payload, url = body, host + path
        headers = {"Content-Type": "application/json", "User-Agent": "Bybit-MCP/0.3.2"}
        if not public:
            offset = await self.clock(env)
            key, secret = self.store.decrypt(profile["api_key"]), self.store.decrypt(profile["api_secret"])
        try:
            # Conservative per-connection pacing. Upstream endpoint-specific limits
            # still apply and are returned to the caller; mutations are not retried.
            async with self.pacing_lock:
                identity = profile.get("id", "public")
                now = time.monotonic()
                wait = max(0, self.pacing.get(identity, now) - now)
                self.pacing[identity] = now + wait + .125
                if len(self.pacing) > 4096:
                    self.pacing = {k: v for k, v in self.pacing.items() if v > now - 60}
            if wait:
                await asyncio.sleep(wait)
            async with self.limiter:
                enforce(profile, None if internal else endpoint["id"], None if internal else endpoint["scope"])
                if not public:
                    timestamp, recv = str(int(time.time() * 1000 + offset)), "5000"
                    signature, _ = sign(secret, timestamp + key + recv + payload)
                    headers.update({"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": timestamp,
                                    "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": signature})
                response = await self.http.request(method, url, content=body.encode() if body else None, headers=headers)
        except httpx.HTTPError as e:
            raise BybitError("Bybit network failure. Mutation outcome may be unknown; query orderLinkId before retrying." if method != "GET" else "Bybit network failure", ambiguous=method != "GET") from e
        if response.status_code != 200:
            raise BybitError(f"Bybit HTTP {response.status_code}", response.status_code, method != "GET" and response.status_code >= 500)
        try:
            result = response.json()
        except ValueError as e:
            raise BybitError("Bybit returned a non-JSON response", ambiguous=method != "GET") from e
        code = result.get("retCode")
        if code != 0:
            if code == 10002:
                self.clocks.pop(env, None)
            message = str(result.get("retMsg", "Bybit rejected request"))[:600]
            if not public:
                message = message.replace(key, "[REDACTED]").replace(secret, "[REDACTED]")
            raise BybitError(message, code)
        extra = {}
        if endpoint["scope"] == "bybit:credentials" and method != "GET":
            def sensitive(value):
                if isinstance(value, dict):
                    return any(k.lower().replace("_", "") in SECRET_FIELDS and v for k, v in value.items()) or any(sensitive(v) for v in value.values())
                return isinstance(value, list) and any(sensitive(v) for v in value)
            if sensitive(result):
                from .db import token
                ident = token()
                self.store.put("secret_result", ident, {"profile": profile["id"], "operation": endpoint["id"], "created_at": time.time(),
                                                       "encrypted": self.store.encrypt(json.dumps(result))}, 600)
                extra["sensitive_result"] = {"id": ident, "expires_in": 600,
                    "download_url": self.settings.public_url + "/admin/api/secret-results/" + ident,
                    "note": "New API credentials are encrypted. The service owner can download once in the admin console within 10 minutes. No connection signing credentials are returned."}
        return {"environment": env, "operation": endpoint["id"], "data": redact(result), **extra,
                "rate_limit": {h: response.headers[h] for h in ["X-Bapi-Limit", "X-Bapi-Limit-Status", "X-Bapi-Limit-Reset-Timestamp"] if h in response.headers}}

    async def request(self, profile, endpoint, params, granted_scopes=None):
        enforce(profile, endpoint["id"], endpoint["scope"])
        if not profile.get("enabled", True):
            raise PermissionError("Connection is disabled")
        scope = endpoint["scope"]
        allowed = set(profile.get("scopes", []))
        if granted_scopes is not None:
            allowed &= set(granted_scopes)
        if scope not in allowed:
            raise PermissionError("Required scope: " + scope)
        if profile["environment"] == "demo" and not endpoint["demo"]:
            raise ValueError("Bybit Demo does not support this endpoint; no mainnet fallback is allowed.")
        params = copy.deepcopy(params)
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
        if len(json.dumps(params)) > 150000:
            raise ValueError("Request is too large")
        # Signing headers are never sourced from params. API-key management
        # endpoints legitimately accept a target `apiKey` in the JSON body.
        if any(k.lower().startswith("x-bapi-") for k in params):
            raise ValueError("Signing headers are managed by the service")
        mutation = endpoint["method"] != "GET"
        if "request" in params and (not isinstance(params["request"], list) or not all(isinstance(o, dict) for o in params["request"])):
            raise ValueError("request must be an array of objects")
        if profile.get("usdt_only") and scope == "bybit:trade":
            params.setdefault("category", "linear")
        errors = list(Draft202012Validator(endpoint["schema"]).iter_errors(params))
        if errors:
            error = errors[0]
            raise ValueError(f"Invalid parameter {'.'.join(map(str, error.path)) or '(request)'}: {error.message}")
        if profile.get("usdt_only") and scope == "bybit:trade":
            if not endpoint["path"].startswith(("/v5/order/", "/v5/position/")):
                raise ValueError("USDT-only connection permits standard order and position endpoints; other trading products require an unrestricted connection")
            params.setdefault("category", "linear")
            if params["category"] != "linear" or params.get("settleCoin", "USDT") != "USDT" or params.get("coin", "USDT") != "USDT":
                raise ValueError("This connection only allows linear USDT contracts")
            orders = params.get("request", [params])
            for order in orders:
                symbol = order.get("symbol")
                if symbol and (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9_]+USDT", symbol)):
                    raise ValueError("Only USDT-settled symbols are allowed")
                if symbol and mutation and ("create" in endpoint["path"] or "amend" in endpoint["path"]):
                    await self.validate_order(profile, params["category"], order)
        if profile.get("usdt_only") and endpoint["path"] == "/v5/account/demo-apply-money":
            if any(x.get("coin") != "USDT" for x in params.get("utaDemoApplyMoney", [])):
                raise ValueError("This connection only requests USDT demo funds")
        if endpoint["path"] in {"/v5/order/create", "/v5/order/create-batch"}:
            for order in params.get("request", [params]):
                order.setdefault("orderLinkId", "mcp-" + uuid.uuid4().hex)
        try:
            result = await self._request(profile, endpoint, params)
            self.store.audit(profile["id"], endpoint["id"], "ok", params.get("symbol", ""))
            if endpoint["path"] in {"/v5/order/create", "/v5/order/create-batch"}:
                result["submitted_order_links"] = [o["orderLinkId"] for o in params.get("request", [params])]
                result["note"] = "Accepted by API is not a fill confirmation; check orders, executions or private streams."
            if endpoint["path"] in {"/v5/order/create-batch", "/v5/order/amend-batch", "/v5/order/cancel-batch"}:
                info = result["data"].get("retExtInfo", {}).get("list", [])
                result["batch_results"] = [{"index": i, "order_link_id": o.get("orderLinkId"), "order_id": o.get("orderId"),
                    "accepted": int(info[i].get("code", 0)) == 0 if i < len(info) else None,
                    "code": info[i].get("code") if i < len(info) else None,
                    "message": info[i].get("msg", "") if i < len(info) else "Check individual order status"} for i, o in enumerate(params["request"])]
            return result
        except BybitError as e:
            self.store.audit(profile["id"], endpoint["id"], "unknown" if e.ambiguous else "error", str(e.code))
            if e.ambiguous:
                e.order_links = [o.get("orderLinkId") for o in params.get("request", [params]) if o.get("orderLinkId")]
            raise

    async def validate_order(self, profile, category, order):
        result = await self._request(profile, BY_PATH[("GET", "/v5/market/instruments-info")], {"category": category, "symbol": order["symbol"]}, internal=True)
        items = result["data"].get("result", {}).get("list", [])
        item = next((x for x in items if x.get("symbol") == order["symbol"]), None)
        if not item or item.get("settleCoin") != "USDT" or item.get("status") != "Trading":
            raise ValueError("Exact symbol is not an active USDT contract in this environment")
        for field, rule in [("qty", item.get("lotSizeFilter", {}).get("qtyStep")), ("price", item.get("priceFilter", {}).get("tickSize"))]:
            if field not in order:
                continue
            try:
                value, step = Decimal(str(order[field])), Decimal(rule or "0")
                if not value.is_finite() or value < 0 or (value == 0 and not (field == "qty" and order.get("reduceOnly") and order.get("closeOnTrigger"))) or (step and value % step):
                    raise ValueError(f"{field} must match instrument precision {rule}")
            except InvalidOperation as e:
                raise ValueError(f"Invalid {field}") from e

    async def websocket(self, profile, channel, topics, seconds=3, max_messages=10, category="linear"):
        enforce(profile, "ws:" + channel, "bybit:read")
        if not 1 <= seconds <= 15 or not 1 <= max_messages <= 50 or not 1 <= len(topics) <= 10:
            raise ValueError("Use 1-15 seconds, 1-50 messages and 1-10 topics")
        if channel not in {"public", "private"} or category not in {"spot", "linear", "inverse", "option", "spread"}:
            raise ValueError("Invalid WebSocket channel/category")
        if channel == "private":
            allowed = {"order", "execution", "execution.fast", "position", "wallet", "greeks"}
            if any(t.split(".")[0] not in allowed for t in topics):
                raise ValueError("Unsupported private topic")
        elif any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", t) for t in topics):
            raise ValueError("Invalid public topic")
        env = profile["environment"]
        host = WS_HOSTS["mainnet" if env == "demo" and channel == "public" else env]
        url = host + ("/v5/private" if channel == "private" else "/v5/public/" + category)
        messages = []
        async with websockets.connect(url, open_timeout=10, max_size=2**21, proxy=self.settings.proxy or None) as ws:
            enforce(profile, "ws:" + channel, "bybit:read")
            if channel == "private":
                expires = int(time.time() * 1000 + await self.clock(env) + 10000)
                signature, _ = sign(self.store.decrypt(profile["api_secret"]), "GET/realtime" + str(expires))
                enforce(profile, "ws:" + channel, "bybit:read")
                await ws.send(json.dumps({"op": "auth", "args": [self.store.decrypt(profile["api_key"]), expires, signature]}))
                auth = json.loads(await asyncio.wait_for(ws.recv(), 8))
                if auth.get("success") is not True:
                    raise BybitError("WebSocket authentication failed")
            enforce(profile, "ws:" + channel, "bybit:read")
            await ws.send(json.dumps({"op": "subscribe", "args": topics}))
            until = time.monotonic() + seconds
            while time.monotonic() < until and len(messages) < max_messages:
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), max(.01, until - time.monotonic())))
                    if message.get("op") == "subscribe" and message.get("success") is False:
                        raise BybitError("WebSocket subscription rejected")
                    if "topic" in message:
                        enforce(profile, "ws:" + channel, "bybit:read")
                        messages.append(redact(message))
                except asyncio.TimeoutError:
                    break
        enforce(profile, "ws:" + channel, "bybit:read")
        return {"environment": env, "channel": channel, "messages": messages, "note": "Bounded snapshot; an empty private stream can mean no account events occurred."}

    async def websocket_trade(self, profile, operation, params, scopes):
        if profile["environment"] == "demo":
            raise ValueError("Bybit Demo does not support WS Trade. Use REST trading tools.")
        mapping = {"order.create": "create", "order.amend": "amend", "order.cancel": "cancel", "order.create-batch": "create-batch", "order.amend-batch": "amend-batch", "order.cancel-batch": "cancel-batch"}
        if operation not in mapping:
            raise ValueError("Unsupported WS trade operation")
        endpoint = BY_PATH[("POST", "/v5/order/" + mapping[operation])]
        enforce(profile, "ws:trade", "bybit:trade")
        enforce(profile, endpoint["id"], endpoint["scope"])
        if "bybit:trade" not in set(profile["scopes"]) & set(scopes):
            raise PermissionError("Required scope: bybit:trade")
        params = copy.deepcopy(params)
        if profile.get("usdt_only"):
            if params.get("category") != "linear":
                raise ValueError("Only linear USDT contracts are permitted")
            for order in params.get("request", [params]):
                if not str(order.get("symbol", "")).endswith("USDT"):
                    raise ValueError("Only USDT-settled symbols are permitted")
                if "create" in operation or "amend" in operation:
                    await self.validate_order(profile, "linear", order)
        for order in params.get("request", [params]):
            if "create" in operation:
                order.setdefault("orderLinkId", "mcp-" + uuid.uuid4().hex)
        errors = list(Draft202012Validator(endpoint["schema"]).iter_errors(params))
        if errors:
            raise ValueError(errors[0].message)
        expires = int(time.time() * 1000 + await self.clock(profile["environment"]) + 10000)
        signature, _ = sign(self.store.decrypt(profile["api_secret"]), "GET/realtime" + str(expires))
        async with websockets.connect(WS_HOSTS[profile["environment"]] + "/v5/trade", open_timeout=10, proxy=self.settings.proxy or None) as ws:
            await ws.send(json.dumps({"op": "auth", "args": [self.store.decrypt(profile["api_key"]), expires, signature]}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if ack.get("retCode") != 0:
                raise BybitError("WS Trade authentication failed")
            req_id = uuid.uuid4().hex
            payload = {"reqId": req_id, "header": {"X-BAPI-TIMESTAMP": str(int(time.time() * 1000 + await self.clock(profile["environment"]))), "X-BAPI-RECV-WINDOW": "5000"}, "op": operation, "args": [params]}
            enforce(profile, "ws:trade", "bybit:trade")
            enforce(profile, endpoint["id"], endpoint["scope"])
            await ws.send(json.dumps(payload))
            try:
                while True:
                    answer = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if answer.get("reqId") == req_id:
                        break
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
                self.store.audit(profile["id"], operation, "unknown", req_id)
                raise BybitError("WS Trade response not received; check orderLinkId before retrying", ambiguous=True) from e
        self.store.audit(profile["id"], operation, str(answer.get("retCode")), req_id)
        return {"environment": profile["environment"], "data": redact(answer), "submitted_order_links": [o.get("orderLinkId") for o in params.get("request", [params])]}
