"""USDT order planning, durable submission identity, and order lifecycle controls."""
import asyncio
import copy
import re
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from .bybit import BybitError
from .catalog import lookup, BY_PATH
from .db import token
from .security import enforce, principal_var, job_authorization
from .authorization_tokens import AuthorizationTokens

FINAL = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}
ACTIVE = {"New", "PartiallyFilled", "Untriggered", "Triggered", "Created"}

def number(value, field, zero=False):
    if isinstance(value, bool):
        raise ValueError(field + " 必须是有效数字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(field + " 必须是有效数字") from e
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ValueError(field + (" 必须大于等于 0" if zero else " 必须大于 0"))
    return result


def text(value):
    return format(value, "f")


def exact_step(value, step, field):
    value = number(value, field)
    if step > 0 and value % step:
        raise ValueError(f"{field} 必须符合步长 {text(step)}，请调整后重新预览")
    return value


class Trading:
    def __init__(self, client, store):
        self.client, self.store = client, store
        self.job_lock = asyncio.Lock()

    @staticmethod
    def permission(profile, scopes, required):
        if not profile or not profile.get("enabled"):
            raise PermissionError("账户连接已停用或不存在")
        allowed = set(profile["scopes"]) & set(scopes if scopes is not None else profile["scopes"])
        if required not in allowed:
            raise PermissionError("Required scope: " + required)

    async def api(self, profile, operation, params, scopes=None):
        endpoint = lookup(operation)
        result = await self.client.request(profile, endpoint, params, scopes)
        if endpoint["method"] == "GET" and endpoint["path"] in {"/v5/order/realtime", "/v5/order/history"}:
            self.observe_orders(profile, result["data"]["result"].get("list", []))
        return result

    def observe_orders(self, profile, orders):
        """Merge authoritative exchange observations without terminal-state regressions."""
        incoming = {o["orderLinkId"]: o for o in orders if o.get("orderLinkId")}
        if not incoming:
            return
        for record in self.store.list("order_submission"):
            if record["profile_id"] != profile["id"] or not any(o["orderLinkId"] in incoming for o in record["orders"]):
                continue
            previous = {o["order_link_id"]: o for o in record.get("observed", [])}
            for order in record["orders"]:
                link = order["orderLinkId"]
                new = incoming.get(link)
                if not new:
                    continue
                old = previous.get(link, {}).get("order") or {}
                if old.get("orderStatus") in FINAL and new.get("orderStatus") not in FINAL:
                    continue
                if int(old.get("updatedTime") or 0) > int(new.get("updatedTime") or 0):
                    continue
                previous[link] = {"order_link_id": link, "found": True, "order": new}
                job = self.store.get("order_expiry", link)
                if job and new.get("orderStatus") in FINAL:
                    job.update(state="completed", final_order_status=new["orderStatus"], checked_at=time.time())
                    self.store.put("order_expiry", link, job, 86400 * 7)
            record["observed"] = [previous.get(o["orderLinkId"], {"order_link_id": o["orderLinkId"], "found": False, "order": None}) for o in record["orders"]]
            record["checked_at"] = time.time()
            if all(o["found"] for o in record["observed"]):
                record["state"] = "confirmed"
            self.store.put("order_submission", record["preview_id"], record, 86400 * 7)

    async def read(self, profile, path, params, scopes=None):
        response = await self.api(profile, "GET " + path, params, scopes)
        return response["data"]["result"]

    async def context(self, profile, symbol, scopes=None):
        self.permission(profile, scopes, "bybit:read")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9_]+USDT", symbol):
            raise ValueError("请填写精确的大写 USDT 合约代码")
        instrument, tickers, positions = await asyncio.gather(
            self.read(profile, "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}, scopes),
            self.read(profile, "/v5/market/tickers", {"category": "linear", "symbol": symbol}, scopes),
            self.read(profile, "/v5/position/list", {"category": "linear", "symbol": symbol}, scopes),
        )
        item = next((i for i in instrument.get("list", []) if i.get("symbol") == symbol), None)
        ticker = next((i for i in tickers.get("list", []) if i.get("symbol") == symbol), None)
        if not item or item.get("settleCoin") != "USDT" or item.get("status") != "Trading" or not ticker:
            raise ValueError("该环境未找到处于 Trading 状态的精确 USDT 合约")
        return {"environment": profile["environment"], "symbol": symbol, "instrument": item,
                "ticker": ticker, "positions": positions.get("list", []), "at": time.time()}

    @staticmethod
    def normalize(draft, ctx):
        if not isinstance(draft, dict):
            raise ValueError("订单草稿必须是对象")
        allowed = {"symbol", "kind", "side", "amount_mode", "amount", "price", "time_in_force", "position_idx", "trigger_price", "trigger_by", "trigger_direction", "reduce_only", "close_on_trigger", "tpsl_mode", "take_profit", "stop_loss", "slippage", "expire_seconds"}
        if not set(draft) <= allowed:
            raise ValueError("草稿存在未识别字段：" + ", ".join(sorted(set(draft) - allowed)))
        if draft.get("close_on_trigger") and not draft.get("reduce_only"):
            raise ValueError("触发平仓保护用于平仓，请同时启用只减仓并确认持仓索引")
        item, ticker = ctx["instrument"], ctx["ticker"]
        for flag in ["reduce_only", "close_on_trigger"]:
            if flag in draft and not isinstance(draft[flag], bool):
                raise ValueError(flag + " 必须是布尔值")
        lot, pf = item["lotSizeFilter"], item["priceFilter"]
        step, tick = number(lot["qtyStep"], "数量步长"), number(pf["tickSize"], "价格步长")
        side, kind = draft.get("side", "Buy"), draft.get("kind", "limit")
        if side not in ("Buy", "Sell") or kind not in ("limit", "market", "conditional_limit", "conditional_market"):
            raise ValueError("无效的订单方向或类型")
        conditional, market = kind.startswith("conditional"), kind.endswith("market")
        last = number(ticker["lastPrice"], "最新价")
        params = {"category": "linear", "symbol": ctx["symbol"], "side": side,
                  "orderType": "Market" if market else "Limit", "orderLinkId": "mcp-" + uuid.uuid4().hex}
        warnings = []
        position_idx = draft.get("position_idx", "auto")
        if isinstance(position_idx, bool) or not isinstance(position_idx, (str, int)):
            raise ValueError("持仓索引必须为 auto、0、1、2")
        if position_idx == "auto":
            indices = {p.get("positionIdx", 0) for p in ctx["positions"]}
            position_idx = (1 if side == "Buy" else 2) if indices and 0 not in indices else 0
            if draft.get("reduce_only") and indices and 0 not in indices:
                position_idx = 2 if side == "Buy" else 1
        try:
            position_idx = int(position_idx)
        except (TypeError, ValueError) as e:
            raise ValueError("持仓索引必须为 auto、0、1、2") from e
        if position_idx not in {0, 1, 2}:
            raise ValueError("持仓索引必须为 auto、0、1、2")
        params["positionIdx"] = position_idx
        if market:
            reference = number(ticker.get("ask1Price") or last, "卖一价") if side == "Buy" else number(ticker.get("bid1Price") or last, "买一价")
            params["timeInForce"] = "IOC"
        else:
            reference = exact_step(draft.get("price"), tick, "委托价格")
            params["price"] = text(reference)
            tif = draft.get("time_in_force", "PostOnly")
            if tif not in {"GTC", "IOC", "FOK", "PostOnly"}:
                raise ValueError("无效的委托有效方式")
            params["timeInForce"] = tif
            minimum, maximum = Decimal(pf.get("minPrice", "0")), Decimal(pf.get("maxPrice", "0"))
            if reference < minimum or maximum and reference > maximum:
                raise ValueError("委托价格超出合约允许范围")
            if tif == "PostOnly":
                cross = reference >= Decimal(ticker.get("ask1Price") or last) if side == "Buy" else reference <= Decimal(ticker.get("bid1Price") or last)
                if cross and not conditional:
                    warnings.append("该 PostOnly 价格当前会立即成交，Bybit 可能将委托取消；请核对买卖方向和价格。")
        if conditional:
            trigger = exact_step(draft.get("trigger_price"), tick, "触发价格")
            trigger_by = draft.get("trigger_by", "MarkPrice")
            if trigger_by not in ("LastPrice", "MarkPrice", "IndexPrice"):
                raise ValueError("无效的条件单触发依据")
            field = {"LastPrice": "lastPrice", "MarkPrice": "markPrice", "IndexPrice": "indexPrice"}.get(trigger_by)
            if not field:
                raise ValueError("无效的条件单触发依据")
            current = number(ticker.get(field) or last, "触发参考价")
            if trigger == current:
                raise ValueError("触发价不能等于当前参考价")
            direction = draft.get("trigger_direction", "auto")
            direction = (1 if trigger > current else 2) if direction == "auto" else int(direction)
            if direction not in {1, 2} or (direction == 1 and trigger <= current) or (direction == 2 and trigger >= current):
                raise ValueError("上涨触发需高于参考价，下跌触发需低于参考价")
            params.update(triggerPrice=text(trigger), triggerBy=trigger_by, triggerDirection=direction)
            if market:
                reference = trigger
            warnings.append("条件单触发时才检查可用保证金，触发时资金不足可能导致订单失败。")
        matching = [p for p in ctx["positions"] if p.get("positionIdx", 0) == position_idx]
        leverage = next((Decimal(p["leverage"]) for p in matching if p.get("leverage") and Decimal(p["leverage"]) > 0), None)
        amount = number(draft.get("amount"), "委托金额 / 数量")
        mode = draft.get("amount_mode", "qty")
        if mode == "qty":
            qty = exact_step(amount, step, "委托数量")
        elif mode in ("notional", "margin"):
            if mode == "margin" and not leverage:
                raise ValueError("无法读取当前账户实际杠杆，请先设置杠杆或改用数量 / 名义金额")
            qty = ((amount * (leverage if mode == "margin" else 1)) / reference / step).to_integral_value(rounding=ROUND_DOWN) * step
        else:
            raise ValueError("无效的数量计算方式")
        minimum_qty = Decimal(lot["minOrderQty"])
        maximum_qty = Decimal(lot.get("maxMktOrderQty" if market else "maxOrderQty") or lot.get("maxOrderQty") or "0")
        if qty <= 0 or not draft.get("reduce_only") and (qty < minimum_qty or maximum_qty and qty > maximum_qty):
            raise ValueError(f"数量需在 {text(minimum_qty)} 至 {text(maximum_qty)} 之间，并符合步长 {text(step)}")
        notional = qty * reference
        if not draft.get("reduce_only") and notional < Decimal(lot.get("minNotionalValue") or "0"):
            raise ValueError("委托名义金额低于合约最小下单金额")
        params["qty"] = text(qty)
        for source, target in [("reduce_only", "reduceOnly"), ("close_on_trigger", "closeOnTrigger")]:
            if draft.get(source):
                params[target] = True
        if params.get("reduceOnly"):
            position = next((p for p in matching if Decimal(p.get("size") or "0") > 0), None)
            if not position or position.get("side") == side:
                raise ValueError("只减仓委托必须对应现有反向持仓及正确的持仓索引")
            if qty > Decimal(position["size"]):
                warnings.append("只减仓委托数量超过当前仓位，Bybit 可能调整可执行数量。")
        tp_mode = draft.get("tpsl_mode", "Full")
        if tp_mode not in ("Full", "Partial"):
            raise ValueError("无效的止盈止损模式")
        for key, target in [("take_profit", "takeProfit"), ("stop_loss", "stopLoss")]:
            spec = draft.get(key)
            if spec is not None and not isinstance(spec, dict):
                raise ValueError("止盈止损参数必须是对象")
            if spec is not None and not set(spec) <= {"value", "unit", "trigger_by", "order_type", "limit_price"}:
                raise ValueError("止盈止损参数存在未识别字段")
            if not spec or spec.get("value") in (None, ""):
                continue
            if params.get("reduceOnly"):
                raise ValueError("只减仓订单不能附带止盈止损")
            value = number(spec["value"], "止盈止损价格 / 百分比")
            if spec.get("unit", "price") == "percent":
                direction = (1 if side == "Buy" else -1) * (1 if key == "take_profit" else -1)
                value = ((reference * (1 + direction * value / 100)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            elif spec.get("unit", "price") != "price":
                raise ValueError("止盈止损只支持价格或百分比")
            value = exact_step(value, tick, "止盈止损价格")
            favorable = value > reference if side == "Buy" else value < reference
            if favorable != (key == "take_profit") or value == reference:
                raise ValueError("止盈应位于盈利方向，止损应位于亏损方向；基准为委托价或触发参考价")
            trigger_by, order_type = spec.get("trigger_by", "MarkPrice"), spec.get("order_type", "Market")
            if trigger_by not in ("LastPrice", "MarkPrice", "IndexPrice") or order_type not in ("Market", "Limit"):
                raise ValueError("无效的止盈止损触发方式")
            if tp_mode == "Full" and order_type == "Limit":
                raise ValueError("全仓止盈止损只支持市价；限价止盈止损请选择 Partial")
            prefix = "tp" if key == "take_profit" else "sl"
            params.update({target: text(value), prefix + "TriggerBy": trigger_by, prefix + "OrderType": order_type, "tpslMode": tp_mode})
            if order_type == "Limit":
                params[prefix + "LimitPrice"] = text(exact_step(spec.get("limit_price"), tick, "止盈止损限价"))
        slippage = draft.get("slippage")
        if slippage:
            if not market or conditional:
                raise ValueError("滑点设置仅支持普通市价订单")
            tolerance = number(slippage, "滑点百分比")
            if not Decimal("0.01") <= tolerance <= 10 or tolerance.as_tuple().exponent < -2:
                raise ValueError("滑点百分比需在 0.01 至 10 之间，最多两位小数")
            params.update(slippageToleranceType="Percent", slippageTolerance=text(tolerance))
        expires = draft.get("expire_seconds", 0)
        if isinstance(expires, bool) or not isinstance(expires, int) or expires != 0 and not 30 <= expires <= 86400:
            raise ValueError("自动撤单时长为 0 或 30 至 86400 秒")
        if expires and market and not conditional:
            raise ValueError("普通市价单不支持到期撤单")
        if params.get("timeInForce") in {"IOC", "FOK"} and expires and not conditional:
            raise ValueError("IOC / FOK 委托不需要到期撤单")
        summary = {"reference_price": text(reference), "qty": text(qty), "notional_usdt": text(notional),
                   "amount_mode": mode,
                   "actual_leverage": text(leverage) if leverage else None,
                   "estimated_initial_margin_usdt": text(notional / leverage) if leverage else None,
                   "pnl_at_one_percent_usdt": text(notional * Decimal("0.01")),
                   "qty_step": text(step), "tick_size": text(tick), "expiry_seconds": expires,
                   "warnings": warnings, "note": "保证金与盈亏为估算，不含手续费、资金费和滑点。实际以下单时账户及 Bybit 风控为准。"}
        return params, summary

    async def preview(self, profile, draft, scopes=None):
        self.permission(profile, scopes, "bybit:read")
        if not isinstance(draft, dict):
            raise ValueError("订单草稿必须是对象")
        if "orders" in draft and (not set(draft) <= {"symbol", "orders"} or not isinstance(draft["orders"], list)):
            raise ValueError("批量草稿使用 symbol 和 orders 数组，每笔参数放入 orders 中")
        ctx = await self.context(profile, draft.get("symbol", ""), scopes)
        orders = draft.get("orders")
        if orders is None:
            orders = [draft]
        if not isinstance(orders, list) or not 1 <= len(orders) <= 20:
            raise ValueError("每次预览支持 1 至 20 笔 USDT 订单")
        normalized, summaries = [], []
        for index, order in enumerate(orders):
            if not isinstance(order, dict) or order.get("symbol", ctx["symbol"]) != ctx["symbol"]:
                raise ValueError("批量草稿需使用同一个 USDT 合约")
            try:
                params, summary = self.normalize(order, ctx)
            except ValueError as e:
                raise ValueError(f"第 {index + 1} 笔：{e}") from e
            normalized.append(params)
            summaries.append(summary)
        ident = token()
        result = {"preview_id": ident, "profile_id": profile["id"], "environment": profile["environment"], "symbol": ctx["symbol"],
                  "orders": normalized, "summaries": summaries, "created_at": time.time(), "expires_at": time.time() + 120,
                  "batch": "orders" in draft, "total_notional_usdt": text(sum(Decimal(s["notional_usdt"]) for s in summaries))}
        self.store.put("order_preview", ident, result, 120)
        return result

    def owned(self, kind, ident, profile):
        if not isinstance(ident, str) or not ident or len(ident) > 256:
            raise ValueError("无效的记录编号")
        item = self.store.get(kind, ident)
        if not item or item.get("profile_id") != profile["id"] or item.get("environment") != profile["environment"]:
            raise ValueError("记录不存在、已过期或不属于当前账户")
        return item

    def submissions(self, profile):
        return sorted([s for s in self.store.list("order_submission") if s.get("profile_id") == profile["id"]], key=lambda x: x["created_at"], reverse=True)[:40]

    def schedule(self, profile, submission, preview):
        for index, (order, summary) in enumerate(zip(preview["orders"], preview["summaries"])):
            duration = summary["expiry_seconds"]
            if not duration:
                continue
            ident = order["orderLinkId"]
            self.store.insert_once("order_expiry", ident, {"profile_id": profile["id"], "environment": profile["environment"],
                "symbol": order["symbol"], "order_link_id": ident, "submission_id": submission,
                "due_at": time.time() + duration, "expires_at": time.time() + duration,
                "state": "scheduled", "created_at": time.time(), "attempts": 0, "authorization": job_authorization(profile)})

    async def submit(self, profile, preview_id, scopes=None):
        self.permission(profile, scopes, "bybit:trade")
        self.permission(profile, scopes, "bybit:read")
        if not isinstance(preview_id, str) or not preview_id or len(preview_id) > 256:
            raise ValueError("无效的预览编号")
        old = self.store.get("order_submission", preview_id)
        if old:
            self.owned("order_submission", preview_id, profile)
            enforce(profile, BY_PATH[("POST", "/v5/order/create-batch" if old.get("batch") or len(old["orders"]) > 1 else "/v5/order/create")]["id"], "bybit:trade")
            return {**old, "replayed": True}
        preview = self.owned("order_preview", preview_id, profile)
        enforce(profile, BY_PATH[("POST", "/v5/order/create-batch" if preview["batch"] else "/v5/order/create")]["id"], "bybit:trade")
        if any(s["expiry_seconds"] for s in preview["summaries"]):
            for method, path in [("POST", "/v5/order/cancel"), ("GET", "/v5/order/realtime"), ("GET", "/v5/order/history")]:
                endpoint = BY_PATH[(method, path)]
                enforce(profile, endpoint["id"], endpoint["scope"])
        if preview["expires_at"] < time.time():
            raise ValueError("预览已过期，请重新预览")
        record = {"profile_id": profile["id"], "environment": profile["environment"], "symbol": preview["symbol"],
                  "created_at": time.time(), "state": "submitting", "preview_id": preview_id,
                  "orders": preview["orders"], "summaries": preview["summaries"], "batch": preview["batch"]}
        if not self.store.insert_once("order_submission", preview_id, record, 86400 * 7):
            return {**self.owned("order_submission", preview_id, profile), "replayed": True}
        self.schedule(profile, preview_id, preview)
        try:
            current = await self.context(profile, preview["symbol"], scopes)
            for order, summary in zip(preview["orders"], preview["summaries"]):
                if summary["amount_mode"] == "margin":
                    position = next((p for p in current["positions"] if p.get("positionIdx", 0) == order["positionIdx"]), None)
                    if not position or not position.get("leverage") or Decimal(position["leverage"]) != Decimal(summary["actual_leverage"]):
                        raise ValueError("账户杠杆在预览后发生变化，请重新预览")
            if preview["batch"]:
                orders = [{k: v for k, v in o.items() if k != "category"} for o in preview["orders"]]
                ack = await self.api(profile, "POST /v5/order/create-batch", {"category": "linear", "request": orders}, scopes)
            else:
                ack = await self.api(profile, "POST /v5/order/create", preview["orders"][0], scopes)
            record.update(state="acknowledged", response=ack)
            if preview["batch"]:
                info = ack["data"].get("retExtInfo", {}).get("list", [])
                record["batch_results"] = [{"index": i, "order_link_id": order["orderLinkId"],
                    "accepted": int(info[i].get("code", 0)) == 0 if i < len(info) else None,
                    "code": info[i].get("code") if i < len(info) else None,
                    "message": info[i].get("msg", "") if i < len(info) else "逐笔结果需核查"} for i, order in enumerate(preview["orders"])]
                if any(r["accepted"] is False for r in record["batch_results"]):
                    record["state"] = "partially_rejected"
                    for item in record["batch_results"]:
                        if item["accepted"] is False:
                            self.store.delete("order_expiry", item["order_link_id"])
        except Exception as e:
            # An unclassified exception after claiming is conservatively unknown.
            unknown = not isinstance(e, (ValueError, PermissionError, BybitError)) or getattr(e, "ambiguous", False)
            record.update(state="unknown" if unknown else "rejected", error=str(e)[:600] if isinstance(e, (ValueError, PermissionError, BybitError)) else "提交结果待核查", code=getattr(e, "code", None))
            if not unknown:
                for order in preview["orders"]:
                    self.store.delete("order_expiry", order["orderLinkId"])
        observed = self.store.get("order_submission", preview_id) or {}
        if observed.get("observed"):
            record["observed"] = observed["observed"]
            record["checked_at"] = observed.get("checked_at")
            if all(o["found"] for o in observed["observed"]) and record["state"] != "partially_rejected":
                record["state"] = "confirmed"
        self.store.put("order_submission", preview_id, record, 86400 * 7)
        return record

    async def find_order(self, profile, symbol, order_link_id=None, order_id=None, scopes=None):
        identifier = order_link_id or order_id
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ValueError("无效的订单编号")
        params = {"category": "linear", "symbol": symbol}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        elif order_id:
            params["orderId"] = order_id
        else:
            raise ValueError("需要订单 ID 或客户订单编号")
        for path in ["/v5/order/realtime", "/v5/order/history"]:
            result = await self.read(profile, path, params, scopes)
            for order in result.get("list", []):
                if order.get("symbol") == symbol and ((order_link_id and order.get("orderLinkId") == order_link_id) or (order_id and order.get("orderId") == order_id)):
                    return order
        return None

    async def reconcile(self, profile, ident, scopes=None):
        self.permission(profile, scopes, "bybit:read")
        record = self.owned("order_submission", ident, profile)
        for order in record["orders"]:
            await self.find_order(profile, order["symbol"], order["orderLinkId"], scopes=scopes)
        record = self.owned("order_submission", ident, profile)
        previous = {o["order_link_id"]: o for o in record.get("observed", [])}
        record["observed"] = [previous.get(o["orderLinkId"], {"order_link_id": o["orderLinkId"], "found": False, "order": None}) for o in record["orders"]]
        record["checked_at"] = time.time()
        if all(o["found"] for o in record["observed"]):
            record["state"] = "confirmed"
        # Absence is not proof a timed-out request was rejected; never resubmit it.
        self.store.put("order_submission", ident, record, 86400 * 7)
        return record

    async def mutate_order(self, profile, action, symbol, order_id, changes=None, scopes=None):
        self.permission(profile, scopes, "bybit:trade")
        order = await self.find_order(profile, symbol, order_id=order_id, scopes=scopes)
        if not order or order.get("orderStatus") not in ACTIVE:
            raise ValueError("订单当前不可修改或撤销，请刷新订单状态")
        params = {"category": "linear", "symbol": symbol, "orderId": order_id}
        if action == "amend":
            allowed = {"qty", "price", "triggerPrice", "takeProfit", "stopLoss", "tpTriggerBy", "slTriggerBy", "triggerBy", "tpslMode", "tpOrderType", "slOrderType", "tpLimitPrice", "slLimitPrice"}
            if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
                raise ValueError("请填写支持修改的价格、总数量或止盈止损字段")
            params.update(changes)
            if "qty" in changes and number(changes["qty"], "修改后总数量") < Decimal(order.get("cumExecQty") or "0"):
                raise ValueError("修改后总数量不能低于已成交数量")
        elif action != "cancel":
            raise ValueError("无效的订单操作")
        response = await self.api(profile, "POST /v5/order/" + action, params, scopes)
        observed = await self.find_order(profile, symbol, order_id=order_id, scopes=scopes)
        return {"acknowledged": True, "response": response, "observed": observed,
                "note": "操作已受理；以订单最新状态确认是否完成。"}

    async def process_expiries(self):
        if self.job_lock.locked():
            return
        async with self.job_lock:
            jobs = [j for j in self.store.list("order_expiry") if j["state"] in {"scheduled", "checking", "cancel_unknown", "cancel_requested", "retry"} and j["due_at"] <= time.time()]
            for job in sorted(jobs, key=lambda j: j["due_at"])[:8]:
                profile = self.store.profile(job["profile_id"])
                job["attempts"] += 1
                auth = job.get("authorization")
                ctx = principal_var.set({**auth, "_authorization": AuthorizationTokens(self.store)} if auth else None)
                try:
                    self.permission(profile, None, "bybit:trade")
                    self.permission(profile, None, "bybit:read")
                    enforce(profile, BY_PATH[("POST", "/v5/order/cancel")]["id"], "bybit:trade")
                    found = await self.find_order(profile, job["symbol"], job["order_link_id"])
                    latest = self.store.get("order_expiry", job["id"])
                    if latest and latest["state"] == "completed":
                        continue
                    if found and found.get("orderStatus") in FINAL:
                        job.update(state="completed", final_order_status=found["orderStatus"])
                    elif found and found.get("orderStatus") in ACTIVE:
                        if job["attempts"] >= 12:
                            job.update(state="needs_attention", error="多次核查后仍有未成交委托，请人工确认撤单状态")
                            self.store.put("order_expiry", job["id"], job)
                            continue
                        # A previous cancellation may still be in flight. Re-query
                        # after a bounded interval before another cancel attempt.
                        if job["state"] == "cancel_requested" and time.time() - job.get("cancel_sent_at", 0) < 20:
                            job["due_at"] = time.time() + 5
                        else:
                            await self.api(profile, "POST /v5/order/cancel", {"category": "linear", "symbol": job["symbol"], "orderLinkId": job["order_link_id"]})
                            job.update(state="cancel_requested", cancel_sent_at=time.time(), due_at=time.time() + 5)
                    elif job["attempts"] >= 12:
                        job.update(state="needs_attention", error="未查到订单，保留原编号人工核查；未重发下单请求。")
                    else:
                        job.update(state="checking", due_at=time.time() + 10)
                except PermissionError:
                    job.update(state="needs_attention", error="账户停用、授权 Token 到期 / 撤销或操作权限不足，自动撤单未执行，请人工处理")
                except BybitError as e:
                    job.update(state="cancel_unknown" if e.ambiguous else "retry", error=str(e)[:300], due_at=time.time() + 15)
                    if job.get("attempts", 0) >= 12:
                        job["state"] = "needs_attention"
                except Exception:
                    job.update(state="retry", error="上游暂不可用，稍后先核查订单再处理", due_at=time.time() + 15)
                    if job["attempts"] >= 12:
                        job["state"] = "needs_attention"
                finally:
                    principal_var.reset(ctx)
                job["checked_at"] = time.time()
                latest = self.store.get("order_expiry", job["id"])
                if not latest or latest["state"] != "completed":
                    self.store.put("order_expiry", job["id"], job, 86400 * 7 if job["state"] in {"completed", "disabled"} else 0)

    async def scheduler(self):
        while True:
            try:
                await self.process_expiries()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.audit(None, "expiry.scheduler", "error")
            await asyncio.sleep(3)
