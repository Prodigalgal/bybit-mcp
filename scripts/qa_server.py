"""Local browser integration fixture at 127.0.0.1:8001; no real exchange calls."""
import os
from pathlib import Path

import httpx
import uvicorn

from bybit_mcp.app import create_app
from bybit_mcp.config import Settings
from tests.trading_fixtures import Exchange

root = Path(__file__).resolve().parents[1]
config = dict(line.split("=", 1) for line in (root / ".private/local.env").read_text().splitlines() if "=" in line)
settings = Settings("http://127.0.0.1:8001", config["ADMIN_TOKEN"], config["ENCRYPTION_KEY"], root / ".private/browser-qa-data")
exchange = Exchange()
exchange.position_size = "1"
app = create_app(settings, httpx.MockTransport(exchange.handle))
with app.state.store.lock, app.state.store.conn:
    app.state.store.conn.execute("DELETE FROM records WHERE kind IN ('order_preview','order_submission','order_expiry')")
app.state.store.save_profile("UI Demo QA", "demo", "ui-qa-key", "ui-qa-secret", ["bybit:read", "bybit:trade", "bybit:funds"], True)
app.state.store.save_profile("UI Testnet QA", "testnet", "ui-qa-testnet-key", "ui-qa-secret", ["bybit:read", "bybit:trade"], True)

@app.get("/qa/stats")
async def fixture_stats():
    return {"orders": len(exchange.orders), "placement_calls": sum(r.url.path in {"/v5/order/create", "/v5/order/create-batch"} for r in exchange.calls),
            "cancel_calls": sum(r.url.path in {"/v5/order/cancel", "/v5/order/cancel-batch"} for r in exchange.calls),
            "active": sum(o["orderStatus"] in {"New", "Untriggered", "PartiallyFilled"} for o in exchange.orders.values())}

uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
