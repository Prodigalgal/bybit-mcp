"""Generate a reviewable V5 catalog from Bybit's official docs repository."""
import argparse
import json
import re
import subprocess
from pathlib import Path

import yaml

DEMO = {
    "/v5/order/" + x for x in ["create", "amend", "cancel", "realtime", "cancel-all", "history", "create-batch", "amend-batch", "cancel-batch"]
} | {"/v5/execution/list"} | {
    "/v5/position/" + x for x in ["list", "set-leverage", "switch-mode", "trading-stop", "set-auto-add-margin", "add-margin", "closed-pnl"]
} | {
    "/v5/account/" + x for x in ["wallet-balance", "borrow-history", "set-collateral-switch", "collateral-info", "info", "transaction-log", "set-margin-mode", "set-hedging-mode", "demo-apply-money"]
} | {"/v5/asset/coin-greeks", "/v5/asset/delivery-record", "/v5/asset/settlement-record",
     "/v5/spot-margin-trade/switch-mode", "/v5/spot-margin-trade/set-leverage", "/v5/spot-margin-trade/status"}


def classify(method, path):
    public = path.startswith("/v5/market/") or path in {"/v5/announcements/index", "/v5/system/status"}
    if method == "GET" or public:
        return public, "bybit:read"
    if path.startswith("/v5/user/") or "/api/" in path or "/apikey" in path:
        return False, "bybit:credentials"
    if path.startswith(("/v5/order/", "/v5/position/", "/v5/spread/order/", "/v5/rfq/", "/v5/grid/", "/v5/dca/", "/v5/fgridbot/", "/v5/fcombobot/", "/v5/fmartingalebot/", "/v5/strategy/", "/v5/alpha/")):
        return False, "bybit:trade"
    if any(x in path for x in ["/withdraw", "/transfer", "/loan", "-loan/", "/lending", "/convert", "/earn/", "/fiat/", "/pay/", "demo-apply-money", "/purchase", "/redeem", "/borrow", "/repay", "/exchange"]):
        return False, "bybit:funds"
    return False, "bybit:account"


def clean_schema(s):
    if not isinstance(s, dict):
        return {}
    s = {k: v for k, v in s.items() if k in {"type", "properties", "required", "items", "enum", "description", "default", "minimum", "maximum", "minLength", "maxLength", "oneOf", "anyOf", "additionalProperties"}}
    # Official explorer YAML contains a few typos/non-JSON-Schema property flags.
    if "type" in s and s["type"] not in {"object", "string", "integer", "number", "boolean", "array", "null"}:
        s["type"] = {"intger": "integer", "int": "integer", "sting": "string"}.get(s["type"], "string")
    if "required" in s and not isinstance(s["required"], list):
        s.pop("required")
    if "required" in s:
        s["required"] = [{"buyLevarge": "buyLeverage", "sellLevarge": "sellLeverage"}.get(name, name) for name in s["required"]]
    if "description" in s and not isinstance(s["description"], str):
        s.pop("description")
    if s.get("type") == "array" and "enum" in s.get("items", {}):
        s = {**s, "type": "string", "enum": [str(x) for x in s["items"]["enum"]]}
        s.pop("items", None)
    if "properties" in s:
        s["properties"] = {k: clean_schema(v) for k, v in s["properties"].items()}
    if "items" in s:
        s["items"] = clean_schema(s["items"])
    return s


def md_parameters(text):
    part = re.split(r"^#{3,5} Request Parameters.*$", text, maxsplit=1, flags=re.M)
    if len(part) < 2:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    part = re.split(r"^#{3,5} (?:Response|Request Example|HTTP Request)", part[1], flags=re.M)[0]
    props, required = {}, []
    for line in part.splitlines():
        if not line.startswith("|"):
            continue
        cells = [x.strip() for x in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        name = cells[0].replace("`", "").replace("**", "").strip()
        name = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", name)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name in {"Parameter", "Name"}:
            continue
        typ = re.sub(r"[^a-z]", "", cells[2].lower())
        if typ.startswith(("array", "list")):
            typ = "array"
        typ = {"int": "integer", "long": "integer", "bool": "boolean"}.get(typ, typ)
        if typ not in {"string", "integer", "number", "boolean", "array", "object"}:
            typ = "string"
        spec = {"type": typ, "description": re.sub(r"<[^>]+>", " ", "|".join(cells[3:]))[:1800]}
        if typ == "array":
            spec["items"] = {}
        props[name] = spec
        if cells[1].replace("*", "").lower() == "true":
            required.append(name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": True}


def generate(root):
    endpoints = {}
    priority = {}
    for file in sorted((root / "docs/v5").rglob("*.mdx")):
        if "abandon" in file.parts or "websocket" in file.parts:
            continue
        text = file.read_text(encoding="utf-8")
        matches = re.findall(r'<APIEndpoint\s+method="(GET|POST|DELETE|PUT|PATCH)"\s+url="(/v5/[^"?]+)"', text)
        if not matches:
            matches = re.findall(r'\b(GET|POST|DELETE|PUT|PATCH)\s+(/v5/[\w/-]+)(?:\s+HTTP|\s*`)', text)
        title = re.search(r"^title:\s*(.+)$", text, re.M)
        doc = str(file.relative_to(root / "docs")).replace("\\", "/").removesuffix(".mdx")
        for method, path in matches:
            score = 100 if file.parent.name == path.split("/")[2] else 20 if len(matches) == 1 else 1
            if priority.get((method, path), -1) > score:
                continue
            priority[(method, path)] = score
            public, scope = classify(method, path)
            op = method.lower() + "_" + path.removeprefix("/v5/").replace("/", "_").replace("-", "_")
            endpoints[(method, path)] = {
                "id": op, "method": method, "path": path,
                "title": title[1].strip('"\'') if title else op,
                "group": path.split("/")[2], "public": public, "scope": scope,
                "demo": path.startswith("/v5/market/") or path in DEMO,
                "docs": "https://bybit-exchange.github.io/docs/" + doc,
                "schema": md_parameters(text[text.find('url="' + path + '"'):]) if len(matches) > 1 else md_parameters(text), "schema_source": "markdown",
            }
    for file in sorted((root / "yml-folder/v5").glob("*.yaml")):
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        for path, methods in data.get("paths", {}).items():
            if not path.startswith("/v5/"):
                continue
            for method, spec in methods.items():
                method = method.upper()
                item = endpoints.get((method, path))
                if not item:
                    continue  # only current documented endpoints
                schema = clean_schema(spec.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}))
                if not schema:
                    schema = {"type": "object", "properties": {}, "required": []}
                    for p in spec.get("parameters", []):
                        if p.get("in") != "query":
                            continue
                        schema["properties"][p["name"]] = clean_schema(p.get("schema", {}))
                        if p.get("required"):
                            schema["required"].append(p["name"])
                # Preserve new parameters added to Markdown after the explorer YAML.
                merged = item["schema"]["properties"] | schema.get("properties", {})
                schema.update(properties=merged, additionalProperties=True)
                item.update(schema=schema, schema_source="openapi+markdown")
    # The Demo page uses an HTML table for nested fields, outside the explorer YAML.
    if ("POST", "/v5/account/demo-apply-money") in endpoints:
        endpoints[("POST", "/v5/account/demo-apply-money")]["schema"] = {
            "type": "object", "properties": {
                "adjustType": {"type": "integer", "enum": [0, 1]},
                "utaDemoApplyMoney": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                    "type": "object", "properties": {"coin": {"type": "string"}, "amountStr": {"type": "string"}}, "required": ["coin", "amountStr"]}}},
            "required": ["utaDemoApplyMoney"], "additionalProperties": True}
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return {"source": "https://github.com/bybit-exchange/docs", "revision": revision,
            "count": len(endpoints), "endpoints": sorted(endpoints.values(), key=lambda e: (e["group"], e["path"], e["method"]))}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", default=".cache/bybit-docs")
    p.add_argument("--output", default="bybit_mcp/catalog.json")
    a = p.parse_args()
    data = generate(Path(a.source))
    Path(a.output).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": data["count"], "demo": sum(x["demo"] for x in data["endpoints"]), "revision": data["revision"]}))
