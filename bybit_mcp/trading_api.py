import time
from decimal import Decimal, ROUND_DOWN

from fastapi import HTTPException, Request

from .trading import number, text, exact_step


def install_trading_api(app, admin, json_object, trading, store):
    def connection(ident):
        if not isinstance(ident, str) or not ident or len(ident) > 128:
            raise ValueError("请指定有效账户连接")
        profile = store.profile(ident)
        if not profile:
            raise HTTPException(404, "请先选择已验证的账户连接")
        return profile

    @app.get("/admin/api/trading/context")
    async def context(request: Request, profile_id: str, symbol: str, include_wallet: bool = False):
        admin(request)
        profile = connection(profile_id)
        result = await trading.context(profile, symbol)
        if include_wallet:
            try:
                result["wallet"] = await trading.read(profile, "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
            except Exception:
                result["wallet_error"] = "余额读取失败，合约和订单查询可分别刷新"
        return result

    @app.get("/admin/api/trading/table")
    async def table(request: Request, profile_id: str, symbol: str, tab: str = "orders", cursor: str = ""):
        admin(request)
        profile = connection(profile_id)
        trading.permission(profile, None, "bybit:read")
        paths = {"orders": "/v5/order/realtime", "positions": "/v5/position/list", "history": "/v5/order/history", "executions": "/v5/execution/list"}
        if tab not in paths or len(cursor) > 2048:
            raise ValueError("无效的查询分类或分页游标")
        if not symbol.endswith("USDT"):
            raise ValueError("仅查询当前 USDT 合约")
        params = {"category": "linear", "symbol": symbol, "limit": 50}
        if cursor:
            params["cursor"] = cursor
        result = await trading.read(profile, paths[tab], params)
        return {"profile_id": profile_id, "environment": profile["environment"], "symbol": symbol, "tab": tab,
                "items": result.get("list", []), "next_cursor": result.get("nextPageCursor", ""), "at": time.time()}

    @app.get("/admin/api/trading/managed")
    async def managed(request: Request, profile_id: str):
        admin(request)
        profile = connection(profile_id)
        trading.permission(profile, None, "bybit:read")
        jobs = sorted([j for j in store.list("order_expiry") if j["profile_id"] == profile_id], key=lambda j: j["created_at"], reverse=True)[:40]
        return {"profile_id": profile_id, "jobs": jobs, "submissions": trading.submissions(profile)}

    @app.post("/admin/api/trading/preview")
    async def preview(request: Request):
        admin(request, True)
        data = await json_object(request)
        return await trading.preview(connection(data.get("profile_id")), data.get("draft"))

    @app.post("/admin/api/trading/submit")
    async def submit(request: Request):
        admin(request, True)
        data = await json_object(request)
        profile = connection(data.get("profile_id"))
        if data.get("confirm_environment") != profile["environment"]:
            raise ValueError("请确认提交环境")
        return await trading.submit(profile, data.get("preview_id", ""))

    @app.post("/admin/api/trading/reconcile")
    async def reconcile(request: Request):
        admin(request, True)
        data = await json_object(request)
        return await trading.reconcile(connection(data.get("profile_id")), data.get("submission_id", ""))

    @app.post("/admin/api/trading/action")
    async def action(request: Request):
        admin(request, True)
        data = await json_object(request)
        profile = connection(data.get("profile_id"))
        trading.permission(profile, None, "bybit:trade")
        trading.permission(profile, None, "bybit:read")
        if data.get("confirm_environment") != profile["environment"]:
            raise ValueError("请确认操作环境")
        symbol, action = data.get("symbol", ""), data.get("action")
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            raise ValueError("请指定当前 USDT 合约")
        if action in {"amend", "cancel"}:
            return await trading.mutate_order(profile, action, symbol, data.get("order_id"), data.get("changes"))
        if action in {"batch_cancel", "batch_amend"}:
            orders = data.get("orders")
            if not isinstance(orders, list) or not 1 <= len(orders) <= 20 or not all(isinstance(o, dict) for o in orders):
                raise ValueError("批量操作支持 1 至 20 笔订单")
            if len({o.get("orderId") for o in orders}) != len(orders) or any(not isinstance(o.get("orderId"), str) or not o["orderId"] for o in orders):
                raise ValueError("订单 ID 不能缺失或重复")
            allowed = {"orderId"} if action == "batch_cancel" else {"orderId", "qty", "price", "triggerPrice", "takeProfit", "stopLoss", "tpTriggerBy", "slTriggerBy", "triggerBy", "tpslMode", "tpLimitPrice", "slLimitPrice"}
            if any(not set(o) <= allowed for o in orders):
                raise ValueError("存在不支持的批量修改字段")
            payload = [{"symbol": symbol, **o} for o in orders]
            result = await trading.api(profile, "POST /v5/order/" + ("cancel-batch" if action == "batch_cancel" else "amend-batch"), {"category": "linear", "request": payload})
            details = result["data"].get("retExtInfo", {}).get("list", [])
            if action == "batch_cancel":
                import asyncio
                await asyncio.gather(*[trading.find_order(profile, symbol, order_id=o["orderId"]) for o in orders])
            return {"acknowledged": True, "response": result, "items": [{"order_id": o["orderId"], "code": details[i].get("code") if i < len(details) else None,
                    "message": details[i].get("msg") if i < len(details) else "请刷新核对逐笔状态"} for i, o in enumerate(orders)]}
        if action in {"disable_expiry", "retry_expiry"}:
            job = trading.owned("order_expiry", data.get("job_id", ""), profile)
            job.update(state="disabled" if action == "disable_expiry" else "scheduled", due_at=time.time(), error="", attempts=0)
            store.put("order_expiry", data["job_id"], job)
            store.audit(profile["id"], "expiry." + action, "ok", symbol)
            return job
        ctx = await trading.context(profile, symbol)
        if action == "leverage":
            limits = ctx["instrument"].get("leverageFilter", {})
            step = Decimal(limits.get("leverageStep") or "1")
            buy = exact_step(data.get("buy_leverage"), step, "多仓杠杆")
            sell = exact_step(data.get("sell_leverage"), step, "空仓杠杆")
            minimum, maximum = Decimal(limits.get("minLeverage") or "1"), Decimal(limits.get("maxLeverage") or "1")
            if any(not minimum <= x <= maximum for x in [buy, sell]):
                raise ValueError(f"该合约杠杆范围为 {text(minimum)} 至 {text(maximum)}")
            if any(p.get("positionIdx", 0) == 0 for p in ctx["positions"]) and buy != sell:
                raise ValueError("单向持仓模式下多空杠杆需一致")
            ack = await trading.api(profile, "POST /v5/position/set-leverage", {"category": "linear", "symbol": symbol, "buyLeverage": text(buy), "sellLeverage": text(sell)})
            positions = await trading.read(profile, "/v5/position/list", {"category": "linear", "symbol": symbol})
            return {"acknowledged": True, "response": ack, "positions": positions["list"]}
        if action == "stops":
            params = data.get("params")
            allowed = {"positionIdx", "takeProfit", "stopLoss", "tpTriggerBy", "slTriggerBy", "trailingStop", "activePrice", "tpslMode", "tpSize", "slSize", "tpOrderType", "slOrderType", "tpLimitPrice", "slLimitPrice"}
            if not isinstance(params, dict) or not set(params) <= allowed:
                raise ValueError("无效的仓位止盈止损参数")
            idx = params.get("positionIdx", 0)
            if isinstance(idx, bool) or idx not in (0, 1, 2):
                raise ValueError("无效的持仓索引")
            position = next((p for p in ctx["positions"] if p.get("positionIdx", 0) == idx and Decimal(p.get("size") or "0") > 0), None)
            if not position:
                raise ValueError("该持仓索引没有可设置的仓位，请刷新")
            tick = Decimal(ctx["instrument"]["priceFilter"]["tickSize"])
            if params.get("tpslMode", "Full") not in ("Full", "Partial"):
                raise ValueError("无效的止盈止损模式")
            for field in ["tpTriggerBy", "slTriggerBy"]:
                if field in params and params[field] not in ("LastPrice", "MarkPrice", "IndexPrice"):
                    raise ValueError("无效的止盈止损触发依据")
            for prefix in ["tp", "sl"]:
                if params.get(prefix + "OrderType", "Market") not in ("Market", "Limit"):
                    raise ValueError("无效的止盈止损执行类型")
                if params.get(prefix + "OrderType") == "Limit" and prefix + "LimitPrice" not in params:
                    raise ValueError("限价止盈止损需要填写对应限价")
            for key in ["takeProfit", "stopLoss", "trailingStop", "activePrice", "tpLimitPrice", "slLimitPrice"]:
                if key in params:
                    value = number(params[key], key, zero=key in {"takeProfit", "stopLoss", "trailingStop"})
                    if value and value % tick:
                        raise ValueError(key + " 不符合价格步长")
            if params.get("tpslMode", "Full") == "Partial":
                tp_size, sl_size = number(params.get("tpSize"), "部分止盈数量"), number(params.get("slSize"), "部分止损数量")
                step = Decimal(ctx["instrument"]["lotSizeFilter"]["qtyStep"])
                if tp_size != sl_size or tp_size > Decimal(position["size"]) or tp_size % step:
                    raise ValueError("部分止盈与止损数量需相同、符合步长且不超过仓位数量")
            elif params.get("tpOrderType", "Market") != "Market" or params.get("slOrderType", "Market") != "Market":
                raise ValueError("Full 模式只支持市价止盈止损")
            if not any(k in params for k in {"takeProfit", "stopLoss", "trailingStop"}):
                raise ValueError("请设置止盈、止损或移动止损，0 表示取消对应设置")
            return await trading.api(profile, "POST /v5/position/trading-stop", {"category": "linear", "symbol": symbol, **params})
        raise ValueError("不支持的交易操作")

    @app.post("/admin/api/trading/close-preview")
    async def close_preview(request: Request):
        admin(request, True)
        data = await json_object(request)
        profile = connection(data.get("profile_id"))
        ctx = await trading.context(profile, data.get("symbol", ""))
        idx = data.get("position_idx", 0)
        position = next((p for p in ctx["positions"] if p.get("positionIdx", 0) == idx and Decimal(p.get("size") or "0") > 0), None)
        if not position:
            raise ValueError("仓位已变化或没有持仓，请刷新")
        percent = number(data.get("percent", 100), "平仓比例")
        if percent > 100:
            raise ValueError("平仓比例不能超过 100%")
        step = Decimal(ctx["instrument"]["lotSizeFilter"]["qtyStep"])
        qty = (Decimal(position["size"]) * percent / 100 / step).to_integral_value(rounding=ROUND_DOWN) * step
        return await trading.preview(profile, {"symbol": ctx["symbol"], "side": "Sell" if position["side"] == "Buy" else "Buy", "kind": "market",
            "amount_mode": "qty", "amount": text(qty), "position_idx": idx, "reduce_only": True})
