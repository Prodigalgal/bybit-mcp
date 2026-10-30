import asyncio
import hmac
import json
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.routing import Route

from .bybit import BybitClient, BybitError
from .assets import install_assets
from .catalog import CATALOG, ENDPOINTS, lookup, search
from .config import SCOPES, Settings
from .db import Store, digest, token
from .mcp_server import ProtectedMCP, create_mcp
from .oauth import OAuth
from .trading import Trading
from .trading_api import install_trading_api

ROOT = Path(__file__).parent


def create_app(settings=None, transport=None):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir / "bybit-mcp.db", settings.encryption_key)
    client = BybitClient(settings, store, transport)
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    templates.env.globals["outbound_ip"] = os.environ.get("BYBIT_OUTBOUND_IPV4", "")
    oauth = OAuth(settings, store, client, templates)
    trading = Trading(client, store)
    server, manager, get_tools = create_mcp(settings, client, oauth, trading)

    @asynccontextmanager
    async def lifespan(app):
        store.cleanup()
        async def housekeeping():
            while True:
                await asyncio.sleep(60)
                store.cleanup()
        task = asyncio.create_task(housekeeping())
        expiry_task = asyncio.create_task(trading.scheduler())
        try:
            async with manager.run():
                yield
        finally:
            task.cancel()
            expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(asyncio.CancelledError):
                await expiry_task
            await client.close()
            store.conn.close()

    app = FastAPI(title="Bybit V5 MCP", version="0.3.1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store, app.state.bybit, app.state.oauth = settings, store, client, oauth
    app.state.mcp, app.state.get_tools = server, get_tools
    app.state.trading = trading
    install_assets(app, templates, ROOT / "static")
    app.include_router(oauth.router)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    app.router.routes.append(Route("/mcp", ProtectedMCP(manager, oauth), methods=["GET", "POST", "DELETE"]))
    budgets = defaultdict(deque)

    @app.middleware("http")
    async def security_headers(request, call_next):
        path = request.url.path
        length = request.headers.get("content-length", "0")
        if length.isdigit() and int(length) > 524288:
            return JSONResponse({"error": "Request too large"}, 413)
        if request.method == "POST" and (path.startswith("/oauth/") or path == "/admin/login"):
            ip = request.client.host if request.client else "unknown"
            key = (ip, path)
            now = time.monotonic()
            window = budgets[key]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= (12 if path in {"/admin/login", "/oauth/register", "/oauth/authorize"} else 60):
                return JSONResponse({"error": "rate_limited"}, 429, headers={"Retry-After": "60"})
            window.append(now)
            if len(budgets) > 4096:
                for k in list(budgets):
                    if not budgets[k] or now - budgets[k][-1] > 60:
                        budgets.pop(k, None)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",
                                 "X-Frame-Options": "DENY", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                                 })
        if path.startswith(("/admin", "/oauth")):
            response.headers["Cache-Control"] = "no-store"
        if settings.public_url.startswith("https"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def admin_session(request):
        session = store.get("admin", digest(request.cookies.get("admin_session", "")))
        return session if session and session.get("token_version") == digest(settings.admin_token) else None

    def admin(request, mutate=False):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:], settings.admin_token):
            return {"csrf": None}
        session = admin_session(request)
        if not session:
            raise HTTPException(401, "Admin token required")
        if mutate and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
            raise HTTPException(403, "Invalid CSRF token")
        return session

    async def json_object(request):
        try:
            data = await request.json()
        except ValueError as e:
            raise ValueError("Invalid JSON body") from e
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    install_trading_api(app, admin, json_object, trading, store)

    @app.get("/admin/api/authorization-tokens")
    async def authorization_tokens(request: Request):
        admin(request)
        return {"items": [oauth.tokens.public(p) for p in store.list("authorization_token")], "operations": oauth.tokens.operation_catalog()}

    @app.post("/admin/api/authorization-tokens")
    async def create_authorization_token(request: Request):
        admin(request, True)
        return JSONResponse(oauth.tokens.create(await json_object(request)), status_code=201, headers={"Cache-Control": "no-store"})

    @app.patch("/admin/api/authorization-tokens/{ident}")
    async def update_authorization_token(ident: str, request: Request):
        admin(request, True)
        return oauth.tokens.update(ident, await json_object(request))

    @app.delete("/admin/api/authorization-tokens/{ident}")
    async def revoke_authorization_token(ident: str, request: Request):
        admin(request, True)
        oauth.tokens.revoke(ident)
        return {"ok": True}

    @app.exception_handler(BybitError)
    async def bybit_error(request, exc):
        return JSONResponse({"error": str(exc), "code": exc.code, "outcome_unknown": exc.ambiguous,
                             "order_links": getattr(exc, "order_links", [])}, 502)

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return JSONResponse({"error": str(exc)[:600]}, 400)

    @app.exception_handler(PermissionError)
    async def permission_error(request, exc):
        return JSONResponse({"error": str(exc)}, 403)

    @app.get("/healthz")
    async def health():
        return JSONResponse({"status": "ok", "service": "bybit-v5-mcp", "version": "0.3.1",
                             "revision": os.environ.get("SOURCE_COMMIT", "local"), "image_digest": os.environ.get("IMAGE_DIGEST", "")}, headers={"Cache-Control": "no-store"})

    @app.get("/readyz")
    async def ready():
        store.conn.execute("SELECT 1").fetchone()
        return {"status": "ready", "endpoints": CATALOG["count"]}

    @app.get("/")
    async def root():
        return RedirectResponse("/admin", 302)

    @app.get("/docs", response_class=HTMLResponse)
    async def docs(request: Request):
        return templates.TemplateResponse(request=request, name="docs.html", context={"url": settings.public_url, "count": CATALOG["count"], "scopes": SCOPES})

    @app.get("/admin", response_class=HTMLResponse)
    async def console(request: Request):
        session = admin_session(request)
        return templates.TemplateResponse(request=request, name="admin.html" if session else "login.html", context={"error": None, "csrf": session["csrf"] if session else ""})

    @app.post("/admin/login")
    async def login(request: Request):
        origin = request.headers.get("origin")
        if origin and origin != settings.public_url:
            raise HTTPException(403, "Invalid origin")
        form = await request.form()
        if not hmac.compare_digest(str(form.get("token", "")), settings.admin_token):
            return templates.TemplateResponse(request=request, name="login.html", context={"error": "Token 不正确，请重新输入。"}, status_code=401)
        ident = token()
        store.put("admin", digest(ident), {"csrf": token(), "token_version": digest(settings.admin_token)}, 8 * 3600)
        response = RedirectResponse("/admin", 303)
        response.set_cookie("admin_session", ident, httponly=True, secure=settings.public_url.startswith("https"), samesite="strict", path="/admin", max_age=8 * 3600)
        return response

    @app.post("/admin/api/logout")
    async def logout(request: Request):
        admin(request, True)
        store.delete("admin", digest(request.cookies.get("admin_session", "")))
        response = JSONResponse({"ok": True})
        response.delete_cookie("admin_session", path="/admin")
        return response

    @app.get("/admin/api/status")
    async def status(request: Request):
        admin(request)
        profiles = store.list("profile")
        tools = await get_tools()
        return {"version": "0.3.1", "public_url": settings.public_url, "mcp_url": settings.resource,
                "catalog": {"count": CATALOG["count"], "demo_count": sum(e["demo"] for e in ENDPOINTS.values()),
                            "source": CATALOG["source"], "revision": CATALOG["revision"],
                            "rich_schemas": sum(e["schema_source"] == "openapi+markdown" for e in ENDPOINTS.values()),
                            "groups": sorted(set(e["group"] for e in ENDPOINTS.values()))},
                "tools": len(tools), "full_endpoint_tools": settings.full_tools, "profiles": len(profiles),
                "grants": len([f for f in store.list("family") if not f.get("revoked")]), "scopes": SCOPES,
                "verification": "公共连通性与协议检查独立验证；私有接口需用户自己的环境密钥。目录收录不代表逐接口实测。"}

    @app.get("/admin/api/profiles")
    async def profiles(request: Request):
        admin(request)
        return {"items": [store.public_profile(p) for p in store.list("profile")]}

    @app.post("/admin/api/profiles")
    async def add_profile(request: Request):
        admin(request, True)
        data = await json_object(request)
        env = data.get("environment", "demo")
        scopes = data.get("scopes", ["bybit:read", "bybit:trade"])
        if not scopes or not set(scopes) <= set(SCOPES):
            raise ValueError("Invalid scopes")
        key, secret = str(data.get("api_key", "")).strip(), str(data.get("api_secret", "")).strip()
        await client.verify_credentials(env, key, secret)
        profile = store.save_profile(str(data.get("name", env)), env, key, secret, scopes, env == "demo" or bool(data.get("usdt_only", True)))
        store.audit(profile["id"], "admin.add_connection", "ok")
        return store.public_profile(profile)

    @app.patch("/admin/api/profiles/{ident}")
    async def edit_profile(ident: str, request: Request):
        admin(request, True)
        profile = store.get("profile", ident)
        if not profile:
            raise HTTPException(404, "Connection not found")
        data = await json_object(request)
        for k in ["enabled", "usdt_only"]:
            if k in data:
                if not isinstance(data[k], bool):
                    raise ValueError(k + " must be boolean")
                profile[k] = data[k]
        if profile["environment"] == "demo":
            profile["usdt_only"] = True
        if "name" in data:
            profile["name"] = str(data["name"])[:100]
        if "scopes" in data:
            if not set(data["scopes"]) <= set(SCOPES):
                raise ValueError("Invalid scopes")
            profile["scopes"] = data["scopes"]
        store.put("profile", ident, profile)
        store.audit(ident, "admin.update_connection", "ok")
        return store.public_profile({"id": ident, **profile})

    @app.delete("/admin/api/profiles/{ident}")
    async def delete_profile(ident: str, request: Request):
        admin(request, True)
        store.delete("profile", ident)
        for family in store.list("family"):
            if family.get("profile") == ident:
                store.put("family", family["id"], {**family, "revoked": True}, 86400 * 31)
        store.audit(ident, "admin.delete_connection", "ok")
        return {"ok": True}

    @app.post("/admin/api/profiles/{ident}/test")
    async def test_profile(ident: str, request: Request):
        admin(request, True)
        profile = store.profile(ident)
        if not profile:
            raise HTTPException(404, "Connection not found")
        return await client.request(profile, lookup("GET /v5/account/info"), {})

    @app.get("/admin/api/catalog")
    async def catalog(request: Request, query: str = "", group: str | None = None, demo_only: bool = False, limit: int = 100):
        admin(request)
        return search(query, group, demo_only, limit)

    @app.get("/admin/api/endpoint/{operation}")
    async def endpoint(operation: str, request: Request):
        admin(request)
        return lookup(operation)

    @app.post("/admin/api/execute")
    async def execute(request: Request):
        admin(request, True)
        data = await json_object(request)
        profile = store.profile(data.get("profile_id", ""))
        if not profile:
            raise HTTPException(404, "请选择一个已验证的账户连接")
        e = lookup(data.get("operation", ""))
        if e["method"] != "GET" and data.get("confirm_environment") != profile["environment"]:
            raise ValueError("请确认本次操作所使用的账户环境")
        return await client.request(profile, e, data.get("params", {}))

    @app.get("/admin/api/grants")
    async def grants(request: Request):
        admin(request)
        return {"items": [{**{k: f.get(k) for k in ["id", "profile", "client_id", "revoked", "parent_token_id", "created_at"]},
                           "bindings": [oauth.tokens.describe_binding(b) for b in f.get("bindings", [])],
                           "active": bool(oauth.tokens.effective(f))} for f in store.list("family")]}

    @app.delete("/admin/api/grants/{ident}")
    async def revoke_grant(ident: str, request: Request):
        admin(request, True)
        family = store.get("family", ident)
        if not family:
            raise HTTPException(404, "Grant not found")
        store.put("family", ident, {**family, "revoked": True}, 86400 * 31)
        store.audit(family.get("profile"), "admin.revoke_oauth", "ok")
        return {"ok": True}

    @app.get("/admin/api/audit")
    async def audit(request: Request):
        admin(request)
        return {"items": store.audits()}

    @app.get("/admin/api/secret-results")
    async def secret_results(request: Request):
        admin(request)
        return {"items": [{k: item[k] for k in ["id", "profile", "operation", "created_at"]} for item in store.list("secret_result")]}

    @app.get("/admin/api/secret-results/{ident}")
    async def secret_result(ident: str, request: Request):
        admin(request)
        item = store.get("secret_result", ident, consume=True)
        if not item:
            raise HTTPException(404, "Result expired or already downloaded")
        store.audit(item["profile"], "admin.download_new_credentials", "ok")
        return Response(store.decrypt(item["encrypted"]), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="bybit-new-credentials.json"', "Cache-Control": "no-store"})

    return app


def main():
    import uvicorn
    uvicorn.run("bybit_mcp.app:create_app", factory=True, host="0.0.0.0", port=8000, proxy_headers=False)


if __name__ == "__main__":
    main()
