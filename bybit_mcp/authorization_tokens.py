"""Hashed authorization tokens and per-connection policy intersection."""
import math
import json
import time

from .catalog import ENDPOINTS, lookup
from .config import SCOPES
from .db import digest, token

SPECIAL_OPERATIONS = {
    "ws:public": ("bybit:read", "读取公共 WebSocket"),
    "ws:private": ("bybit:read", "读取私有 WebSocket"),
    "ws:trade": ("bybit:trade", "WebSocket 交易（另需对应 REST 操作权限）"),
    "managed:read_orders": ("bybit:read", "读取服务保存的挂单记录"),
}
OPERATION_TITLES = {
    "/v5/market/time": "读取服务器时间", "/v5/market/instruments-info": "读取产品规格", "/v5/market/tickers": "读取最新行情",
    "/v5/market/kline": "读取 K 线", "/v5/market/orderbook": "读取订单簿", "/v5/account/info": "读取账户信息",
    "/v5/account/wallet-balance": "读取钱包余额", "/v5/position/list": "读取仓位", "/v5/order/realtime": "读取当前委托",
    "/v5/order/history": "读取历史订单", "/v5/execution/list": "读取成交记录", "/v5/order/create": "提交订单",
    "/v5/order/amend": "修改订单", "/v5/order/cancel": "撤销订单", "/v5/order/cancel-all": "撤销全部订单",
    "/v5/order/create-batch": "批量下单", "/v5/order/amend-batch": "批量改单", "/v5/order/cancel-batch": "批量撤单",
    "/v5/position/set-leverage": "调整杠杆", "/v5/position/trading-stop": "设置止盈止损与移动止损",
}


class AuthorizationTokens:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def operation_scope(operation):
        return SPECIAL_OPERATIONS[operation][0] if operation in SPECIAL_OPERATIONS else ENDPOINTS[operation]["scope"]

    def normalize(self, data, old=None):
        if not isinstance(data, dict) or set(data) - {"name", "bindings", "expires_at", "default_profile_id"}:
            raise ValueError("Unknown authorization token fields")
        value = {**(old or {}), **data}
        name = value.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Token name is required (up to 100 characters)")
        bindings = value.get("bindings")
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 50:
            raise ValueError("Select 1–50 key connections")
        normalized, seen = [], set()
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) - {"profile_id", "environment", "scopes", "operations"}:
                raise ValueError("Invalid connection policy")
            ident = binding.get("profile_id")
            profile = self.store.profile(ident) if isinstance(ident, str) else None
            if not profile or not profile.get("enabled") or ident in seen:
                raise ValueError("Connection must exist, be enabled and not repeated")
            if binding.get("environment") != profile["environment"]:
                raise ValueError("Environment must match the selected key connection")
            scopes = binding.get("scopes")
            if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes) or not set(scopes) <= set(profile["scopes"]) & set(SCOPES):
                raise ValueError("Token scopes must be within the connection permissions")
            ops = binding.get("operations")
            if ops is not None:
                if not isinstance(ops, list) or not ops or len(ops) > 500 or not all(isinstance(o, str) for o in ops):
                    raise ValueError("Select operations or use all operations within selected scopes")
                ops = sorted({o if o in SPECIAL_OPERATIONS else lookup(o)["id"] for o in ops})
                if any(self.operation_scope(o) not in scopes for o in ops):
                    raise ValueError("Operation exceeds selected scopes")
            normalized.append({"profile_id": ident, "environment": profile["environment"], "scopes": sorted(set(scopes)), "operations": ops})
            seen.add(ident)
        expires = value.get("expires_at")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time()):
            raise ValueError("Expiry must be a future Unix timestamp, or null for unlimited")
        default = value.get("default_profile_id") or None
        if default is not None and default not in seen:
            raise ValueError("Default connection must be one of the allowed connections")
        return {"name": name.strip(), "bindings": normalized, "expires_at": expires, "default_profile_id": default}

    def create(self, data):
        policy = self.normalize(data)
        ident, secret = token(), "bbm_auth_" + token()
        now = time.time()
        value = {**policy, "created_at": now, "updated_at": now, "revoked": False, "secret_hash": digest(secret)}
        self.store.put("authorization_token", ident, value)
        self.store.put("authorization_lookup", digest(secret), {"token_id": ident})
        self.store.audit(None, "admin.create_authorization_token", "ok", ident)
        return {**self.public({"id": ident, **value}), "token": secret}

    def update(self, ident, data):
        old = self.store.get("authorization_token", ident)
        if not old or old["revoked"]:
            raise ValueError("Authorization Token not found or revoked")
        policy = self.normalize(data, old)
        value = {**old, **policy, "updated_at": time.time()}
        with self.store.lock:
            self.store.put("authorization_token", ident, value)
            # Persist permission reductions in existing consent snapshots. Re-adding
            # a permission later must not silently expand an existing OAuth grant.
            for kind in ("family", "code"):
                for grant in self.store.list(kind):
                    if grant.get("parent_token_id") != ident:
                        continue
                    capped = self.effective(grant, require_family=False)
                    grant.update(bindings=capped["bindings"] if capped else [], scopes=capped["scopes"] if capped else [])
                    grant_id = grant.pop("id")
                    with self.store.conn:
                        self.store.conn.execute("UPDATE records SET data=? WHERE kind=? AND id=?", (
                            json.dumps(grant, ensure_ascii=False), kind, grant_id))
        self.store.audit(None, "admin.update_authorization_token", "ok", ident)
        return self.public({"id": ident, **value})

    def revoke(self, ident):
        value = self.store.get("authorization_token", ident)
        if not value:
            raise ValueError("Authorization Token not found")
        self.store.put("authorization_token", ident, {**value, "revoked": True, "updated_at": time.time()})
        self.store.audit(None, "admin.revoke_authorization_token", "ok", ident)

    def active(self, ident):
        value = self.store.get("authorization_token", ident)
        if not value or value["revoked"] or (value.get("expires_at") is not None and value["expires_at"] <= time.time()):
            return None
        return {"id": ident, **value}

    def authenticate(self, secret):
        if not isinstance(secret, str) or not secret.startswith("bbm_auth_") or len(secret) > 150:
            return None
        ref = self.store.get("authorization_lookup", digest(secret))
        return self.active(ref["token_id"]) if ref else None

    def effective(self, grant, require_family=True):
        parent = self.active(grant.get("parent_token_id", ""))
        if not parent:
            return None
        family_bindings = None
        if require_family:
            family = self.store.get("family", grant.get("family", ""))
            if not family or family.get("revoked") or family.get("parent_token_id") != parent["id"]:
                return None
            family_bindings = {b["profile_id"]: b for b in family.get("bindings", [])}
        current = {b["profile_id"]: b for b in parent["bindings"]}
        bindings = []
        for approved in grant.get("bindings", []):
            ident = approved["profile_id"]
            policy, profile = current.get(ident), self.store.profile(ident)
            cap = family_bindings.get(ident) if family_bindings is not None else approved
            if not cap:
                continue
            if not policy or not profile or not profile.get("enabled") or policy["environment"] != approved["environment"] or profile["environment"] != approved["environment"]:
                continue
            scopes = sorted(set(approved["scopes"]) & set(policy["scopes"]) & set(cap["scopes"]) & set(profile["scopes"]) & set(grant["scopes"]))
            restrictions = [set(b["operations"]) for b in (approved, policy, cap) if b["operations"] is not None]
            ops = sorted(set.intersection(*restrictions)) if restrictions else None
            if ops is not None:
                ops = [o for o in ops if self.operation_scope(o) in scopes]
            if scopes and (ops is None or ops):
                bindings.append({"profile_id": ident, "environment": profile["environment"], "scopes": scopes, "operations": ops})
        if not bindings:
            return None
        return {**grant, "bindings": bindings, "scopes": sorted({s for b in bindings for s in b["scopes"]}), "_authorization": self}

    def public(self, value):
        result = {k: v for k, v in value.items() if k != "secret_hash"}
        result["status"] = "revoked" if value.get("revoked") else "expired" if value.get("expires_at") is not None and value["expires_at"] <= time.time() else "active"
        result["bindings"] = [self.describe_binding(b) for b in value["bindings"]]
        return result

    def describe_binding(self, binding):
        profile = self.store.profile(binding["profile_id"])
        return {**binding, "name": profile["name"] if profile else "已删除连接", "key_hint": profile["key_hint"] if profile else "", "enabled": bool(profile and profile.get("enabled"))}

    def operation_catalog(self):
        return [{"id": e["id"], "title": OPERATION_TITLES.get(e["path"], e["title"]), "scope": e["scope"], "method": e["method"], "path": e["path"], "demo": e["demo"]} for e in ENDPOINTS.values()] + [
            {"id": k, "title": v[1], "scope": v[0], "method": "SPECIAL", "path": k, "demo": k != "ws:trade"} for k, v in SPECIAL_OPERATIONS.items()]
