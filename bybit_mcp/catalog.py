import json
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
ENDPOINTS = {e["id"]: e for e in CATALOG["endpoints"]}
BY_PATH = {(e["method"], e["path"]): e for e in ENDPOINTS.values()}


def lookup(operation):
    if not isinstance(operation, str):
        raise ValueError("operation must be a string")
    if operation in ENDPOINTS:
        return ENDPOINTS[operation]
    pieces = operation.split(" ", 1)
    if len(pieces) == 2 and (pieces[0].upper(), pieces[1]) in BY_PATH:
        return BY_PATH[(pieces[0].upper(), pieces[1])]
    raise ValueError("Unknown operation. Use bybit_search_endpoints; arbitrary URLs are not accepted.")


def search(query="", group=None, demo_only=False, limit=30):
    words = query.lower().split()
    result = [e for e in ENDPOINTS.values() if all(w in f'{e["id"]} {e["title"]} {e["path"]}'.lower() for w in words)
              and (not group or e["group"] == group) and (not demo_only or e["demo"])]
    return {"total": len(result), "items": [{k: e[k] for k in ["id", "method", "path", "title", "scope", "public", "demo", "docs"]} for e in result[:min(max(limit, 1), 100)]]}
