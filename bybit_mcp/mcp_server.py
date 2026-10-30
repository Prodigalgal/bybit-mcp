import copy
import json
from urllib.parse import urlparse

import mcp.types as t
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from .bybit import BybitError
from .catalog import ENDPOINTS, BY_PATH, lookup, search

from .security import principal_var, enforce

COMMON = {
    "bybit_get_server_time": ("GET", "/v5/market/time"),
    "bybit_get_instruments": ("GET", "/v5/market/instruments-info"),
    "bybit_get_tickers": ("GET", "/v5/market/tickers"),
    "bybit_get_kline": ("GET", "/v5/market/kline"),
    "bybit_get_orderbook": ("GET", "/v5/market/orderbook"),
    "bybit_get_funding_history": ("GET", "/v5/market/funding/history"),
    "bybit_get_wallet": ("GET", "/v5/account/wallet-balance"),
    "bybit_get_positions": ("GET", "/v5/position/list"),
    "bybit_get_open_orders": ("GET", "/v5/order/realtime"),
    "bybit_get_order_history": ("GET", "/v5/order/history"),
    "bybit_get_executions": ("GET", "/v5/execution/list"),
    "bybit_place_order": ("POST", "/v5/order/create"),
    "bybit_amend_order": ("POST", "/v5/order/amend"),
    "bybit_cancel_order": ("POST", "/v5/order/cancel"),
    "bybit_cancel_all_orders": ("POST", "/v5/order/cancel-all"),
    "bybit_batch_place_orders": ("POST", "/v5/order/create-batch"),
    "bybit_batch_amend_orders": ("POST", "/v5/order/amend-batch"),
    "bybit_batch_cancel_orders": ("POST", "/v5/order/cancel-batch"),
    "bybit_set_leverage": ("POST", "/v5/position/set-leverage"),
    "bybit_set_trading_stop": ("POST", "/v5/position/trading-stop"),
    "bybit_get_closed_pnl": ("GET", "/v5/position/closed-pnl"),
    "bybit_demo_funds": ("POST", "/v5/account/demo-apply-money"),
}
ROUTERS = {"bybit_rest_" + scope.split(":")[1]: scope for scope in ["bybit:read", "bybit:trade", "bybit:account", "bybit:funds", "bybit:credentials"]}


def tool(name, description, schema, scope="bybit:read", read=True, meta=None, output=None, external=False):
    schema = copy.deepcopy(schema)
    schema.setdefault("properties", {})["connection_id"] = {"type": "string", "description": "ID from bybit_list_connections. Required for every mutation when authorization covers multiple connections; never infer a mainnet connection."}
    security = [{"type": "oauth2", "scopes": scope if isinstance(scope, list) else [scope]}]
    extra = {"securitySchemes": security, **(meta or {})}
    return t.Tool(name=name, description=description, inputSchema=schema,
                  annotations=t.ToolAnnotations(readOnlyHint=read, destructiveHint=not read,
                                                idempotentHint=read or name == "bybit_submit_usdt_order", openWorldHint=external),
                  outputSchema=output, _meta=extra, securitySchemes=security)


def object_schema(props=None, required=None):
    return {"type": "object", "properties": props or {}, "required": required or [], "additionalProperties": False}


def create_mcp(settings, client, oauth, trading=None):
    server = Server("bybit-v5-mcp", version="0.3.2", instructions=(
        "Bybit V5 gateway. Check bybit_get_profile for account environment and restrictions. "
        "Search/describe endpoints for the full catalog. Demo is separate from testnet and mainnet. "
        "Verify exact instruments before trading. Order acknowledgements do not prove fills; query orders/executions. "
        "For reviewed USDT orders prefer bybit_preview_usdt_order then bybit_submit_usdt_order and bybit_reconcile_submission; these persist submission identity and optional expiry. Raw V5 placement tools require the caller to keep a stable orderLinkId when reconciling uncertain outcomes. "
        "Never request API secrets in chat; connection credentials are entered only in the backend admin. OAuth uses a separately generated authorization Token. List authorized connections and pass connection_id explicitly for multi-connection trading."))

    @server.list_tools()
    async def list_tools():
        tools = [
            tool("bybit_get_profile", "Read the connected Bybit profile and environment. Contains no API credentials.", object_schema(), scope=[],
                 meta={"openai/profile": True}, output={"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "environment": {"type": "string"}}, "required": ["id"]}),
            tool("bybit_list_connections", "List the environments, key connection IDs and effective operations authorized by the current Token. No API secrets are returned.", object_schema(), scope=[]),
            tool("bybit_search_endpoints", "Search the complete official V5 REST catalog. Returned operation IDs work with scoped REST tools. No account request is sent.",
                 object_schema({"query": {"type": "string"}, "group": {"type": "string"}, "demo_only": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), scope=[]),
            tool("bybit_describe_endpoint", "Get the endpoint method, parameters, required scope, Demo availability and official documentation.",
                 object_schema({"operation": {"type": "string"}}, ["operation"]), scope=[]),
        ]
        for name, scope in ROUTERS.items():
            tools.append(tool(name, f"Call a catalogued V5 endpoint requiring {scope}. Use search/describe first. No arbitrary URLs. Mutations affect the connected account.",
                              object_schema({"operation": {"type": "string"}, "params": {"type": "object", "additionalProperties": True}}, ["operation", "params"]),
                              scope, read=scope == "bybit:read", external=scope == "bybit:funds"))
        for name, key in COMMON.items():
            e = BY_PATH.get(key)
            if e:
                tools.append(tool(name, e["title"] + ". " + e["docs"] + (" Demo supported." if e["demo"] else " Demo unavailable."), e["schema"], e["scope"], read=e["method"] == "GET", external=e["public"] or e["scope"] == "bybit:funds"))
        stream_schema = object_schema({"topics": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
                                       "seconds": {"type": "integer", "minimum": 1, "maximum": 15}, "max_messages": {"type": "integer", "minimum": 1, "maximum": 50},
                                       "category": {"type": "string", "enum": ["spot", "linear", "inverse", "option", "spread"]}}, ["topics"])
        tools.extend([
            tool("bybit_public_stream", "Read a bounded public WebSocket snapshot, e.g. tickers.BTCUSDT or orderbook.1.BTCUSDT. Demo uses mainnet public data.", stream_schema, external=True),
            tool("bybit_private_stream", "Read private order/execution/position/wallet/greeks events for up to 15 seconds. No events may return an empty result.", stream_schema),
            tool("bybit_ws_trade", "Send one WebSocket trading request (mainnet/testnet only). Demo must use REST. Ack is not fill confirmation.",
                 object_schema({"operation": {"type": "string", "enum": ["order.create", "order.amend", "order.cancel", "order.create-batch", "order.amend-batch", "order.cancel-batch"]}, "params": {"type": "object"}}, ["operation", "params"]), "bybit:trade", read=False),
        ])
        if trading:
            tools.extend([
                tool("bybit_preview_usdt_order", "Validate and preview one USDT order or up to 20 same-symbol orders. Supports limit, market, conditional_limit/market; qty/notional/margin amounts, TP/SL, and expire_seconds. Preview is valid for 120 seconds and does not place an order. Margin sizing uses the account's actual leverage.",
                     object_schema({"draft": {"type": "object", "description": "symbol, side Buy/Sell, kind limit/market/conditional_limit/conditional_market, amount_mode qty/notional/margin, amount string, price, time_in_force, position_idx auto/0/1/2, trigger_price/by/direction, reduce_only, close_on_trigger, expire_seconds; take_profit/stop_loss objects {value,unit price/percent,trigger_by,order_type,limit_price}; batch: symbol and orders array."}}, ["draft"])),
                tool("bybit_submit_usdt_order", "Submit a reviewed preview_id exactly once. Repeated calls return the persisted result, including unknown outcomes, and never create another order. Optional expiry cancellation runs on the server and only cancels unfilled quantities; verify orders/executions separately.",
                     object_schema({"preview_id": {"type": "string"}}, ["preview_id"]), ["bybit:read", "bybit:trade"], read=False),
                tool("bybit_reconcile_submission", "Query the original orderLinkId values of a previous preview submission. An absent order is not proof of failure; this tool never retries placement.",
                     object_schema({"submission_id": {"type": "string"}}, ["submission_id"])),
                tool("bybit_get_managed_orders", "List this account's durable submissions and scheduled expiry cancellations, including states requiring attention.", object_schema()),
            ])
        if settings.full_tools:
            for e in ENDPOINTS.values():
                tools.append(tool("v5_" + e["id"], e["title"] + ". " + e["docs"], e["schema"], e["scope"], e["method"] == "GET", external=e["public"] or e["scope"] == "bybit:funds"))
        return tools

    @server.call_tool()
    async def call_tool(name, arguments):
        principal = principal_var.get()
        if not principal:
            return t.CallToolResult(isError=True, content=[t.TextContent(type="text", text="Bearer authentication required: use an authorization Token or OAuth access token")],
                                    _meta={"mcp/www_authenticate": [oauth.challenge(error="invalid_token")]})
        arguments = dict(arguments or {})
        selected = arguments.pop("connection_id", None)
        profile = None
        try:
            principal = oauth.tokens.effective(principal)
            if not principal:
                raise PermissionError("Authorization Token expired or revoked")
            bindings = principal["bindings"]
            metadata = {"bybit_get_profile", "bybit_list_connections", "bybit_search_endpoints", "bybit_describe_endpoint"}
            scope = "bybit:read"
            if name in metadata:
                scope = []
            elif name in ROUTERS:
                scope = ROUTERS[name]
            elif name in COMMON:
                scope = BY_PATH[COMMON[name]]["scope"]
            elif name.startswith("v5_") and name[3:] in ENDPOINTS:
                scope = ENDPOINTS[name[3:]]["scope"]
            elif name == "bybit_ws_trade":
                scope = "bybit:trade"
            elif name == "bybit_submit_usdt_order":
                scope = ["bybit:read", "bybit:trade"]
            required = scope if isinstance(scope, list) else [scope]
            mutation = any(s != "bybit:read" for s in required)
            if name not in metadata or selected:
                if mutation and principal.get("multiple_connections") and not selected:
                    raise ValueError("Multiple connections authorized: explicitly pass connection_id from bybit_list_connections for every mutation")
                ident = selected or principal.get("default_profile_id")
                if not ident and not principal.get("multiple_connections") and len(bindings) == 1:
                    ident = bindings[0]["profile_id"]
                binding = next((b for b in bindings if b["profile_id"] == ident), None)
                if not binding:
                    raise PermissionError("Select an authorized connection_id from bybit_list_connections; the server never substitutes another key")
                profile = client.store.profile(ident)
                missing = [s for s in required if s not in binding["scopes"]]
                if missing:
                    return t.CallToolResult(isError=True, content=[t.TextContent(type="text", text="Required scope for this connection: " + " ".join(missing))],
                                            _meta={"mcp/www_authenticate": [oauth.challenge(missing, "insufficient_scope", "Reconnect with an authorized Token")]})
                scopes = binding["scopes"]
                enforce(profile)
            if name == "bybit_list_connections":
                result = {"connections": [oauth.tokens.describe_binding(b) for b in bindings], "default_connection_id": principal.get("default_profile_id"),
                          "explicit_connection_required_for_mutations": principal.get("multiple_connections", False)}
            elif name == "bybit_get_profile":
                if not profile and len(bindings) == 1:
                    profile = client.store.profile(bindings[0]["profile_id"])
                if profile:
                    binding = next(b for b in bindings if b["profile_id"] == profile["id"])
                    result = {"id": profile["id"], "name": profile["name"], "environment": profile["environment"], "scopes": binding["scopes"], "operations": binding["operations"], "usdt_only": profile["usdt_only"]}
                else:
                    result = {"id": principal["parent_token_id"], "name": "Bybit 多连接授权", "connections": [oauth.tokens.describe_binding(b) for b in bindings]}
            elif name == "bybit_search_endpoints":
                result = search(**arguments)
            elif name == "bybit_describe_endpoint":
                result = lookup(arguments["operation"])
            elif name == "bybit_preview_usdt_order" and trading:
                result = await trading.preview(profile, arguments["draft"], scopes)
            elif name == "bybit_submit_usdt_order" and trading:
                result = await trading.submit(profile, arguments["preview_id"], scopes)
            elif name == "bybit_reconcile_submission" and trading:
                enforce(profile, "managed:read_orders", "bybit:read")
                result = await trading.reconcile(profile, arguments["submission_id"], scopes)
            elif name == "bybit_get_managed_orders" and trading:
                enforce(profile, "managed:read_orders", "bybit:read")
                result = {"submissions": trading.submissions(profile), "expiries": [{k: v for k, v in j.items() if k != "authorization"} for j in client.store.list("order_expiry") if j["profile_id"] == profile["id"]]}
            elif name in ROUTERS:
                e = lookup(arguments["operation"])
                if e["scope"] != scope:
                    raise ValueError("Use the router matching endpoint scope: " + e["scope"])
                result = await client.request(profile, e, arguments["params"], scopes)
                if trading and e["method"] == "GET" and e["path"] in {"/v5/order/realtime", "/v5/order/history"}:
                    trading.observe_orders(profile, result["data"]["result"].get("list", []))
            elif name in COMMON or (name.startswith("v5_") and name[3:] in ENDPOINTS):
                e = BY_PATH[COMMON[name]] if name in COMMON else ENDPOINTS[name[3:]]
                result = await client.request(profile, e, arguments, scopes)
                if trading and e["method"] == "GET" and e["path"] in {"/v5/order/realtime", "/v5/order/history"}:
                    trading.observe_orders(profile, result["data"]["result"].get("list", []))
            elif name in {"bybit_public_stream", "bybit_private_stream"}:
                result = await client.websocket(profile, "private" if name == "bybit_private_stream" else "public", **arguments)
            elif name == "bybit_ws_trade":
                result = await client.websocket_trade(profile, arguments["operation"], arguments["params"], scopes)
            else:
                raise ValueError("Unknown tool")
            failed = result.get("state") in {"rejected", "unknown", "partially_rejected"} or any(r.get("accepted") is False for r in result.get("batch_results", []))
            return t.CallToolResult(content=[t.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))], structuredContent=result, isError=failed)
        except (ValueError, PermissionError, BybitError) as e:
            error = {"error": str(e), "code": getattr(e, "code", None), "outcome_unknown": getattr(e, "ambiguous", False), "order_links": getattr(e, "order_links", [])}
            return t.CallToolResult(isError=True, content=[t.TextContent(type="text", text=json.dumps(error, ensure_ascii=False))], structuredContent=error)
        except Exception:
            client.store.audit(profile["id"] if profile else None, name, "internal_error")
            return t.CallToolResult(isError=True, content=[t.TextContent(type="text", text="Upstream request failed. Check the server audit log; credentials are never included.")])

    host = urlparse(settings.public_url).netloc
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*", "testserver"],
        allowed_origins=[settings.public_url, "https://chatgpt.com", "http://127.0.0.1:*", "http://localhost:*"])
    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True, security_settings=security, max_request_body_size=524288)
    return server, manager, list_tools


class ProtectedMCP:
    def __init__(self, manager, oauth):
        self.manager, self.oauth = manager, oauth

    async def __call__(self, scope, receive, send):
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode()
        principal = self.oauth.principal(auth[7:]) if auth.lower().startswith("bearer ") else None
        if not principal:
            response = JSONResponse({"error": "invalid_token", "error_description": "Use an authorization Token as Bearer, or connect with OAuth"}, 401,
                                    headers={"WWW-Authenticate": self.oauth.challenge(error="invalid_token"), "Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        ctx = principal_var.set(principal)
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            principal_var.reset(ctx)
