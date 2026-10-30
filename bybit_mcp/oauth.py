import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import SCOPES
from .db import digest, token
from .authorization_tokens import AuthorizationTokens


def oauth_error(error, description, status=400):
    return JSONResponse({"error": error, "error_description": description}, status_code=status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def redirect_uri_ok(uri):
    try:
        p = urlparse(uri)
        host = (p.hostname or "").encode("idna").decode()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                return False
        return not re.search(r"[\s<>;]", uri) and not p.fragment and not p.username and not p.password and bool(p.hostname) and p.port != 0 and (
            p.scheme == "https" or (p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}))
    except (ValueError, TypeError, UnicodeError):
        return False


def add_query(url, **values):
    p = urlparse(url)
    return urlunparse(p._replace(query=urlencode([*parse_qsl(p.query), *[(k, v) for k, v in values.items() if v is not None]])))


class OAuth:
    def __init__(self, settings, store, client, templates):
        self.settings, self.store, self.client, self.templates = settings, store, client, templates
        self.tokens = AuthorizationTokens(store)
        self.router = APIRouter()
        self.register_routes()

    def challenge(self, scopes=None, error=None, description=None):
        parts = [f'Bearer resource_metadata="{self.settings.public_url}/.well-known/oauth-protected-resource/mcp"']
        if scopes:
            parts.append(f'scope="{" ".join(scopes)}"')
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
        return ", ".join(parts)

    def principal(self, bearer):
        grant = self.store.get("access", digest(bearer)) if bearer else None
        if not grant or grant.get("audience") != self.settings.resource or grant.get("issuer") != self.settings.public_url:
            return None
        family = self.store.get("family", grant["family"])
        if not family or family.get("revoked"):
            return None
        return self.tokens.effective(grant)

    async def get_client(self, ident):
        registered = self.store.get("client", ident)
        if registered:
            return registered
        p = urlparse(ident)
        allowed = set(os.environ.get("OAUTH_METADATA_HOSTS", "chatgpt.com").split(","))
        if p.scheme != "https" or p.hostname not in allowed or p.port not in {None, 443} or p.username or p.password or p.fragment or p.query or not p.path.endswith(".json"):
            return None
        cached = self.store.get("cimd", ident)
        if cached:
            return cached
        try:
            async with httpx.AsyncClient(timeout=8, proxy=self.settings.proxy, trust_env=False, follow_redirects=False) as c:
                async with c.stream("GET", ident, headers={"Accept": "application/json"}) as r:
                    if r.status_code != 200:
                        return None
                    raw = b""
                    async for chunk in r.aiter_bytes():
                        raw += chunk
                        if len(raw) > 32768:
                            return None
            doc = json.loads(raw)
            redirects = doc.get("redirect_uris", [])
            methods = doc.get("token_endpoint_auth_methods_supported", [doc.get("token_endpoint_auth_method", "none")])
            if doc.get("client_id") != ident or not redirects or not all(redirect_uri_ok(u) for u in redirects) or "none" not in methods:
                return None
            client = {"client_id": ident, "client_name": str(doc.get("client_name", "MCP Client"))[:120], "redirect_uris": redirects,
                      "token_endpoint_auth_method": "none", "cimd": True}
            self.store.put("cimd", ident, client, 3600)
            return client
        except (ValueError, httpx.HTTPError, TypeError):
            return None

    def authenticate_client(self, request, form, client):
        method = client.get("token_endpoint_auth_method", "none")
        auth = request.headers.get("authorization", "")
        supplied = form.get("client_secret", "")
        ident = form.get("client_id", "")
        if auth.startswith("Basic "):
            try:
                from urllib.parse import unquote_plus
                user, password = base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
                ident, supplied = unquote_plus(user), unquote_plus(password)
            except (ValueError, UnicodeError):
                return False
            if method != "client_secret_basic":
                return False
        elif method == "client_secret_basic":
            return False
        if ident != client["client_id"]:
            return False
        if method == "none":
            return not supplied and not auth
        return bool(supplied) and hmac.compare_digest(digest(supplied), client.get("secret_hash", ""))

    def issue(self, profile, client_id, scopes, resource, family=None, *, parent_token_id, bindings, default_profile_id=None, multiple_connections=None):
        family = family or token()
        policy = {"parent_token_id": parent_token_id, "bindings": bindings, "scopes": scopes,
                  "default_profile_id": default_profile_id, "multiple_connections": len(bindings) > 1 if multiple_connections is None else multiple_connections}
        previous = self.store.get("family", family)
        effective = self.tokens.effective({**policy, "family": family}, require_family=previous is not None)
        if not effective:
            raise ValueError("Authorization Token is no longer valid")
        policy.update(bindings=effective["bindings"], scopes=effective["scopes"])
        scopes = policy["scopes"]
        self.store.put("family", family, {**policy, "client_id": client_id, "revoked": False, "created_at": previous.get("created_at", time.time()) if previous else time.time()}, 86400 * 31)
        base = {"profile": profile, "client_id": client_id, "scopes": scopes,
                **policy, "audience": resource, "issuer": self.settings.public_url, "family": family, "created_at": time.time()}
        access, refresh = token(), token()
        self.store.put("access", digest(access), base, 1200)
        self.store.put("refresh", digest(refresh), {**base, "used": False}, 86400 * 30)
        return {"access_token": access, "token_type": "Bearer", "expires_in": 1200,
                "refresh_token": refresh, "scope": " ".join(scopes)}

    def register_routes(self):
        r = self.router

        @r.get("/.well-known/oauth-protected-resource")
        @r.get("/.well-known/oauth-protected-resource/mcp")
        async def resource_metadata():
            return {"resource": self.settings.resource, "authorization_servers": [self.settings.public_url],
                    "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"],
                    "resource_name": "Bybit V5 MCP", "resource_documentation": self.settings.public_url + "/docs"}

        @r.get("/.well-known/oauth-authorization-server")
        async def server_metadata():
            u = self.settings.public_url
            return {"issuer": u, "authorization_endpoint": u + "/oauth/authorize", "token_endpoint": u + "/oauth/token",
                    "registration_endpoint": u + "/oauth/register", "revocation_endpoint": u + "/oauth/revoke",
                    "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                    "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                    "code_challenge_methods_supported": ["S256"], "scopes_supported": list(SCOPES),
                    "client_id_metadata_document_supported": True, "authorization_response_iss_parameter_supported": True}

        @r.post("/oauth/register")
        async def register(request: Request):
            try:
                body = await request.json()
            except ValueError:
                return oauth_error("invalid_client_metadata", "JSON required")
            if not isinstance(body, dict):
                return oauth_error("invalid_client_metadata", "JSON object required")
            redirects = body.get("redirect_uris", [])
            method = body.get("token_endpoint_auth_method", "none")
            if not isinstance(redirects, list) or not 1 <= len(redirects) <= 10 or not all(isinstance(u, str) and len(u) < 2048 and redirect_uri_ok(u) for u in redirects):
                return oauth_error("invalid_redirect_uri", "Exact HTTPS or loopback redirect URIs required")
            if method not in {"none", "client_secret_post", "client_secret_basic"} or body.get("grant_types", ["authorization_code", "refresh_token"]) not in (["authorization_code"], ["authorization_code", "refresh_token"], ["refresh_token", "authorization_code"]):
                return oauth_error("invalid_client_metadata", "Unsupported authentication method or grants")
            ident, secret = token(), token() if method != "none" else None
            public = {"client_id": ident, "client_name": str(body.get("client_name", "MCP Client"))[:120], "redirect_uris": redirects,
                      "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
                      "response_types": ["code"], "client_id_issued_at": int(time.time())}
            self.store.put("client", ident, {**public, "secret_hash": digest(secret) if secret else ""}, 86400 * 365)
            if secret:
                public.update(client_secret=secret, client_secret_expires_at=0)
            return JSONResponse(public, status_code=201, headers={"Cache-Control": "no-store"})

        def consent_page(request, flow_id, flow, error=None, status=200):
            verified = None
            if flow.get("parent_token_id"):
                policy = self.tokens.effective(flow, require_family=False)
                parent = self.tokens.active(flow["parent_token_id"])
                if policy and parent:
                    verified = {"name": parent["name"], "expires_at": parent["expires_at"],
                                "bindings": [self.tokens.describe_binding(b) for b in policy["bindings"]]}
                    operations = self.tokens.operation_catalog()
                    for binding in verified["bindings"]:
                        binding["available_operations"] = [o for o in operations if o["scope"] in binding["scopes"]
                            and (binding["operations"] is None or o["id"] in binding["operations"])
                            and (binding["environment"] != "demo" or o["demo"])]
            response = self.templates.TemplateResponse(request=request, name="authorize.html", context={
                "flow_id": flow_id, "flow": flow, "scope_labels": SCOPES, "verified": verified, "error": error}, status_code=status)
            # Browsers apply form-action to the final OAuth callback after a POST redirect.
            callback = urlparse(flow["redirect_uri"])
            callback_host = callback.hostname.encode("idna").decode()
            callback_origin = callback.scheme + "://" + ("[" + callback_host + "]" if ":" in callback_host else callback_host) + (":" + str(callback.port) if callback.port else "")
            response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' " + callback_origin)
            return response

        @r.get("/oauth/authorize", response_class=HTMLResponse)
        async def authorize(request: Request):
            q = request.query_params
            client = await self.get_client(q.get("client_id", ""))
            redirect = q.get("redirect_uri", "")
            if not client or redirect not in client["redirect_uris"] or not redirect_uri_ok(redirect):
                return oauth_error("invalid_request", "Unknown client or redirect URI")
            def reject(code, description):
                return RedirectResponse(add_query(redirect, error=code, error_description=description, state=q.get("state"), iss=self.settings.public_url), 302)
            if q.get("response_type") != "code":
                return reject("unsupported_response_type", "Only authorization code flow is supported")
            if q.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", q.get("code_challenge", "")):
                return reject("invalid_request", "PKCE S256 is required")
            if q.get("resource") != self.settings.resource:
                return reject("invalid_target", "The resource must equal the canonical MCP URL")
            scopes = sorted(set(q.get("scope", "bybit:read").split()))
            if not scopes or any(s not in SCOPES for s in scopes):
                return reject("invalid_scope", "Unknown or empty scopes")
            flow_id, browser = token(), token()
            flow = {"client_id": client["client_id"], "client_name": client.get("client_name", "MCP Client"), "redirect_uri": redirect,
                    "state": q.get("state"), "challenge": q["code_challenge"], "resource": q["resource"], "scopes": scopes,
                    "csrf": token(), "browser_hash": digest(browser), "expires_at": time.time() + 600}
            self.store.put("flow", digest(flow_id), flow, 600)
            response = consent_page(request, flow_id, flow)
            response.set_cookie("oauth_browser", browser, httponly=True, secure=self.settings.public_url.startswith("https"), samesite="lax", max_age=600, path="/oauth")
            return response

        @r.post("/oauth/authorize", response_class=HTMLResponse)
        async def consent(request: Request):
            form = await request.form()
            flow_id = str(form.get("flow_id", ""))
            flow = self.store.get("flow", digest(flow_id))
            if not flow or not hmac.compare_digest(digest(request.cookies.get("oauth_browser", "")), flow["browser_hash"]) or not hmac.compare_digest(str(form.get("csrf", "")), flow["csrf"]):
                return oauth_error("invalid_request", "Authorization session expired; reconnect")
            if request.headers.get("origin") not in {None, self.settings.public_url}:
                return oauth_error("invalid_request", "Invalid origin")
            decision = form.get("decision")
            if decision == "deny":
                self.store.delete("flow", digest(flow_id))
                response = RedirectResponse(add_query(flow["redirect_uri"], error="access_denied", state=flow["state"], iss=self.settings.public_url), 303)
                response.delete_cookie("oauth_browser", path="/oauth")
                return response
            if decision == "verify":
                parent = self.tokens.authenticate(str(form.get("authorization_token", "")).strip())
                if not parent:
                    return consent_page(request, flow_id, flow, "Token 无效、已到期或已撤销。", 400)
                candidate = {**flow, "parent_token_id": parent["id"], "bindings": parent["bindings"],
                             "default_profile_id": parent["default_profile_id"], "multiple_connections": len(parent["bindings"]) > 1}
                effective = self.tokens.effective(candidate, require_family=False)
                if not effective:
                    return consent_page(request, flow_id, flow, "Token 没有可用于本次客户端请求的权限或连接。", 400)
                flow = {k: v for k, v in effective.items() if not k.startswith("_")}
                flow["verified"] = True
                self.store.put("flow", digest(flow_id), flow, max(1, flow["expires_at"] - time.time()))
                return consent_page(request, flow_id, flow)
            if decision != "allow" or not flow.get("verified"):
                return consent_page(request, flow_id, flow, "请先验证后台生成的授权 Token。", 400)
            effective = self.tokens.effective(flow, require_family=False)
            if not effective:
                return consent_page(request, flow_id, flow, "Token 已到期、已撤销或连接已禁用，请重新开始授权。", 400)
            if effective["bindings"] != flow["bindings"]:
                flow = {k: v for k, v in effective.items() if not k.startswith("_")}
                self.store.put("flow", digest(flow_id), flow, max(1, flow["expires_at"] - time.time()))
                return consent_page(request, flow_id, flow, "后台已缩减权限，请核对更新后的范围并再次批准。", 409)
            if not self.store.get("flow", digest(flow_id), consume=True):
                return oauth_error("invalid_request", "Authorization session already used")
            code = token()
            self.store.put("code", digest(code), flow, 300)
            self.store.audit(None, "oauth.authorize", "ok", flow["parent_token_id"])
            response = RedirectResponse(add_query(flow["redirect_uri"], code=code, state=flow["state"], iss=self.settings.public_url), 303)
            response.delete_cookie("oauth_browser", path="/oauth")
            return response

        async def form_client(request):
            form = await request.form()
            ident = form.get("client_id", "")
            if request.headers.get("authorization", "").startswith("Basic "):
                try:
                    from urllib.parse import unquote_plus
                    ident = unquote_plus(base64.b64decode(request.headers["authorization"][6:], validate=True).decode().split(":", 1)[0])
                except (ValueError, UnicodeError):
                    return form, None
            return form, await self.get_client(str(ident))

        @r.post("/oauth/token")
        async def exchange(request: Request):
            form, client = await form_client(request)
            if not client or not self.authenticate_client(request, form, client):
                return oauth_error("invalid_client", "Client authentication failed", 401)
            grant_type = form.get("grant_type")
            if grant_type == "authorization_code":
                hashed = digest(str(form.get("code", "")))
                grant = self.store.get("code", hashed)
                verifier = str(form.get("code_verifier", ""))
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if not grant or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not hmac.compare_digest(challenge, grant["challenge"]) or grant["client_id"] != client["client_id"] or grant["redirect_uri"] != form.get("redirect_uri"):
                    return oauth_error("invalid_grant", "Invalid code, verifier, client or redirect URI")
                if form.get("resource", grant["resource"]) != grant["resource"]:
                    return oauth_error("invalid_target", "Resource does not match authorization")
                if not self.tokens.effective(grant, require_family=False) or not self.store.get("code", hashed, consume=True):
                    return oauth_error("invalid_grant", "Authorization Token is expired or revoked, or code already used")
                result = self.issue(grant.get("default_profile_id"), client["client_id"], grant["scopes"], grant["resource"],
                                    **{k: grant[k] for k in ("parent_token_id", "bindings", "default_profile_id", "multiple_connections")})
            elif grant_type == "refresh_token":
                hashed = digest(str(form.get("refresh_token", "")))
                grant = self.store.get("refresh", hashed)
                if not grant or grant["client_id"] != client["client_id"]:
                    return oauth_error("invalid_grant", "Invalid refresh token")
                family = self.store.get("family", grant["family"])
                if not family or family.get("revoked"):
                    return oauth_error("invalid_grant", "Grant has been revoked")
                if grant["used"]:
                    self.store.put("family", grant["family"], {**family, "revoked": True}, 86400 * 31)
                    return oauth_error("invalid_grant", "Refresh token reuse detected; grant revoked")
                if form.get("resource", grant["audience"]) != grant["audience"]:
                    return oauth_error("invalid_target", "Resource does not match authorization")
                current = self.tokens.effective(grant)
                if not current:
                    return oauth_error("invalid_grant", "Parent Authorization Token is expired or revoked")
                scopes = sorted(set(str(form.get("scope", " ".join(current["scopes"]))).split()))
                if not scopes or not set(scopes) <= set(current["scopes"]):
                    return oauth_error("invalid_scope", "Refresh cannot increase authorized scope")
                with self.store.lock:
                    latest = self.store.get("refresh", hashed)
                    if not latest or latest["used"]:
                        self.store.put("family", grant["family"], {**family, "revoked": True}, 86400 * 31)
                        return oauth_error("invalid_grant", "Refresh token reuse detected; grant revoked")
                    self.store.put("refresh", hashed, {**grant, "used": True}, 86400 * 30)
                    result = self.issue(grant.get("profile"), client["client_id"], scopes, grant["audience"], grant["family"],
                                        **{k: grant[k] for k in ("parent_token_id", "bindings", "default_profile_id", "multiple_connections")})
            else:
                return oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token")
            return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

        @r.post("/oauth/revoke")
        async def revoke(request: Request):
            form, client = await form_client(request)
            if not client or not self.authenticate_client(request, form, client):
                return oauth_error("invalid_client", "Client authentication failed", 401)
            hashed = digest(str(form.get("token", "")))
            grant = self.store.get("refresh", hashed) or self.store.get("access", hashed)
            if grant and grant["client_id"] == client["client_id"]:
                family = self.store.get("family", grant["family"])
                if family:
                    self.store.put("family", grant["family"], {**family, "revoked": True}, 86400 * 31)
            return JSONResponse({}, headers={"Cache-Control": "no-store"})
