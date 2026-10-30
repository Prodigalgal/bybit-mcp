# Bybit V5 MCP

A self-hosted Bybit V5 gateway with OAuth-protected Streamable HTTP MCP, an administration console and a USDT trading workbench.

- Catalog of 446 V5 REST operations, scoped routers, common tools and bounded WebSocket snapshots.
- Separate mainnet, Testnet and Demo connections. Demo private requests use the Demo host; unsupported operations never fall back to mainnet.
- Bybit credentials are entered in the admin console and encrypted at rest.
- Admin-generated authorization Tokens can select multiple environments and exact key connections, with per-connection scopes and operation allowlists. Validity can be unlimited or time-limited.
- OAuth requires one authorization Token, shows effective permissions, then asks for consent. PKCE, exact redirects, resource binding, rotating refresh and revocation are enforced. Parent Token restrictions apply to derived grants and scheduled tasks.
- Multi-connection writes require an explicit `connection_id`. Permission reductions apply to existing grants; expanding an OAuth grant requires new consent. Direct Bearer requests use the current backend Token policy.
- Trading supports preview, durable submission identity, limit/market/conditional orders, TP/SL, batch orders, amendments, cancellation and scheduled expiry. Acknowledgement does not prove a fill; reconcile order and execution records.

## Local setup

Use Python 3.12. Install `requirements.txt`, set `PUBLIC_URL`, a randomly generated `ADMIN_TOKEN` of at least 32 characters, `ENCRYPTION_KEY` (a Fernet key), and `DATA_DIR`.

```sh
python -m pip install -r requirements.txt
python -m bybit_mcp.app
```

`.env.example` documents configuration; the application reads process environment variables. Preserve the encryption key with a separate secure backup. `/admin` uses the admin login Token; `/mcp` accepts the authorization Token directly as Bearer, or a derived OAuth access token. The admin login Token, authorization Token and Bybit API credentials serve separate purposes.

For Demo, create an API key inside the Bybit main site's **Demo Trading** account, save a Demo connection in the console, then generate an authorization Token for that connection. Testnet keys and mainnet keys are separate.

## CI and GitOps

GitHub Actions tests and builds a Linux ARM64 image, promotes its immutable digest into a separate GitOps repository, then verifies the deployed revision and OAuth protection. Argo CD performs the deployment. Environment-specific addresses, node names and credentials are configured through Actions Secrets and cluster Secrets, outside this public source.

See [CI/CD configuration](docs/CI_CD.md) and [validation boundaries](docs/ACCEPTANCE.md). Kubernetes templates under `deploy/` contain example values and must be configured for your own cluster, gateway and retained storage. The application uses SQLite and requires a single replica with `Recreate` deployment strategy.

## Validation

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q --tb=short
```

Tests use synthetic credentials and mocked exchange responses. CI never places mainnet or Demo orders. Operational logs, browser screenshots, database backups and private environment configuration are excluded from this repository.

## References

[Bybit V5](https://bybit-exchange.github.io/docs/v5/intro) · [Bybit Demo](https://bybit-exchange.github.io/docs/v5/demo) · [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth) · [MCP authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization)


## Direct Bearer access

Generate an authorization Token in the admin console, then configure the MCP client with the Streamable HTTP URL and header:

```text
Authorization: Bearer bbm_auth_…
```

No OAuth login is needed for this mode. The same per-key environment, scope, operation, expiry and revocation restrictions apply. OAuth remains available for clients using the authorization-code flow. The administrator login Token is reserved for administration.
