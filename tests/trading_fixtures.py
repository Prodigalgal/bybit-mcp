"""Deterministic wire-level exchange fixture. Never forwards any network call."""
import copy
import json
import time
import uuid

import httpx


class Exchange:
    def __init__(self):
        self.calls, self.orders = [], {}
        self.leverage, self.position_size = "50", "0"
        self.placement_timeout, self.timeout_after_accept = False, False
        self.batch_fail_index = None
        self.instrument = {"symbol": "XAUUSDT", "status": "Trading", "settleCoin": "USDT", "baseCoin": "XAU", "quoteCoin": "USDT", "contractType": "LinearPerpetual",
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "1000", "maxMktOrderQty": "1000", "minNotionalValue": "5"},
            "priceFilter": {"tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "100", "leverageStep": "0.01"}}
        self.ticker = {"symbol": "XAUUSDT", "lastPrice": "100.00", "markPrice": "100.00", "indexPrice": "100.00", "bid1Price": "99.99", "ask1Price": "100.01"}

    def position(self):
        return {"symbol": "XAUUSDT", "positionIdx": 0, "side": "Buy" if self.position_size != "0" else "", "size": self.position_size,
                "leverage": self.leverage, "avgPrice": "100", "markPrice": "100", "unrealisedPnl": "0.50", "takeProfit": "110", "stopLoss": "90", "trailingStop": "0"}

    def create(self, params):
        order = {**params, "orderId": str(uuid.uuid4()), "orderStatus": "Untriggered" if params.get("triggerPrice") else "New", "cumExecQty": "0", "createdTime": str(int(time.time() * 1000)), "updatedTime": str(int(time.time() * 1000))}
        if params.get("orderType") == "Market" and not params.get("triggerPrice"):
            order.update(orderStatus="Filled", cumExecQty=params["qty"])
            self.position_size = "0" if params.get("reduceOnly") else params["qty"]
        self.orders[order["orderId"]] = order
        return {"orderId": order["orderId"], "orderLinkId": order["orderLinkId"]}

    def handle(self, request):
        self.calls.append(request)
        path, params = request.url.path, dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        result, code, message, ext = {}, 0, "OK", {}
        if path == "/v5/market/time":
            result = {"timeSecond": str(int(time.time())), "timeNano": str(time.time_ns())}
        elif path == "/v5/account/info":
            result = {"unifiedMarginStatus": 6}
        elif path == "/v5/account/wallet-balance":
            result = {"list": [{"accountType": "UNIFIED", "totalAvailableBalance": "100000", "coin": [{"coin": "USDT", "walletBalance": "100000", "equity": "100000"}]}]}
        elif path == "/v5/market/instruments-info":
            result = {"list": [copy.deepcopy(self.instrument)] if params.get("symbol") == "XAUUSDT" else []}
        elif path == "/v5/market/tickers":
            result = {"list": [self.ticker]}
        elif path == "/v5/position/list":
            result = {"list": [self.position()], "nextPageCursor": ""}
        elif path == "/v5/order/create":
            if self.placement_timeout and not self.timeout_after_accept:
                raise httpx.ReadTimeout("simulated lost acknowledgement", request=request)
            result = self.create(body)
            if self.placement_timeout:
                raise httpx.ReadTimeout("simulated lost acknowledgement after acceptance", request=request)
        elif path == "/v5/order/create-batch":
            result, failures = {"list": []}, []
            for i, order in enumerate(body["request"]):
                if i == self.batch_fail_index:
                    result["list"].append({"orderId": "", "orderLinkId": order["orderLinkId"]})
                    failures.append({"code": 110007, "msg": "Insufficient available balance"})
                else:
                    result["list"].append(self.create(order))
                    failures.append({"code": 0, "msg": "OK"})
            ext = {"list": failures}
        elif path in {"/v5/order/realtime", "/v5/order/history"}:
            orders = [o for o in self.orders.values() if o["symbol"] == params.get("symbol", o["symbol"])]
            for field in ["orderId", "orderLinkId"]:
                if params.get(field):
                    orders = [o for o in orders if o.get(field) == params[field]]
            if path.endswith("realtime") and not params.get("orderId") and not params.get("orderLinkId"):
                orders = [o for o in orders if o["orderStatus"] in {"New", "Untriggered", "PartiallyFilled"}]
            result = {"list": copy.deepcopy(orders), "nextPageCursor": ""}
        elif path in {"/v5/order/cancel", "/v5/order/amend"}:
            order = self.orders.get(body.get("orderId")) or next((o for o in self.orders.values() if o["orderLinkId"] == body.get("orderLinkId")), None)
            if not order:
                code, message = 110001, "Order does not exist"
            else:
                if path.endswith("cancel"):
                    order["orderStatus"] = "Cancelled"
                else:
                    order.update(body)
                result = {"orderId": order["orderId"], "orderLinkId": order["orderLinkId"]}
        elif path in {"/v5/order/cancel-batch", "/v5/order/amend-batch"}:
            results, errors = [], []
            for changes in body["request"]:
                order = self.orders.get(changes.get("orderId"))
                if order:
                    if path.endswith("cancel-batch"):
                        order["orderStatus"] = "Cancelled"
                    else:
                        order.update(changes)
                    results.append({"orderId": order["orderId"]})
                    errors.append({"code": 0, "msg": "OK"})
                else:
                    results.append({"orderId": changes["orderId"]})
                    errors.append({"code": 110001, "msg": "Order does not exist"})
            result, ext = {"list": results}, {"list": errors}
        elif path == "/v5/order/cancel-all":
            for order in self.orders.values():
                if order["symbol"] == body["symbol"]:
                    order["orderStatus"] = "Cancelled"
        elif path == "/v5/position/set-leverage":
            self.leverage = body["buyLeverage"]
        elif path == "/v5/position/trading-stop":
            result = {}
        elif path == "/v5/execution/list":
            result = {"list": [], "nextPageCursor": ""}
        return httpx.Response(200, json={"retCode": code, "retMsg": message, "result": result, "retExtInfo": ext, "time": int(time.time() * 1000)})
