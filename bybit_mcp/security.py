"""Request-local authorization, shared by REST, WebSocket and durable jobs."""
from contextvars import ContextVar

principal_var = ContextVar("bybit_principal", default=None)


def enforce(profile, operation=None, scope=None):
    principal = principal_var.get()
    if principal is None:  # Admin routes and internal credential verification use owner authorization.
        return
    service = principal["_authorization"]
    current = service.effective(principal)
    if not current:
        raise PermissionError("Authorization Token expired, revoked, or no longer permits this connection")
    binding = next((b for b in current["bindings"] if b["profile_id"] == profile["id"]
                    and b["environment"] == profile["environment"]), None)
    if not binding:
        raise PermissionError("This connection/environment is not authorized")
    if scope and scope not in binding["scopes"]:
        raise PermissionError("Required scope for this connection: " + scope)
    if operation and binding["operations"] is not None and operation not in binding["operations"]:
        raise PermissionError("Operation not authorized for this connection: " + operation)


def job_authorization(profile):
    principal = principal_var.get()
    if principal is None:
        return None
    return {k: principal[k] for k in ("parent_token_id", "family", "bindings", "scopes", "default_profile_id", "multiple_connections")}
