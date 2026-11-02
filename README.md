# Bybit V5 MCP

A self-hosted Bybit V5 gateway with OAuth-protected Streamable HTTP MCP, an administration console and a USDT trading workbench.

- Catalog of 446 V5 REST operations, scoped routers, common tools and bounded WebSocket snapshots.
- Separate mainnet, Testnet and Demo connections. Demo private requests use the Demo host; unsupported operations never fall back to mainnet.
- Bybit credentials are entered in the admin console and encrypted at rest.
- Admin-generated authorization Tokens can select multiple environments and exact key connections, with per-connection scopes and operation allowlists. Validity can be unlimited or time-limited.
- OAuth requires one authorization Token, shows effective permissions, then asks for consent. PKCE, exact redirects, resource binding, rotating refresh and revocation are enforced. Parent Token restrictions apply to derived grants and scheduled tasks.
- Multi-connection writes require an explicit `connection_id`. Permission reductions apply to existing grants; expanding an OAuth grant requires new consent. Direct Bearer requests use the current backend Token policy.
- Trading supports preview, durable submission identity, limit/market/conditional orders, TP/SL, batch orders, amendments, cancellation and scheduled expiry. Acknowledgement does not prove a fill; reconcile order and execution records.
- Market responses distinguish `account_environment` from `market_data_environment`. A Demo connection retrieves mainnet public quotes without a mainnet API key. Testnet connections retrieve Testnet quotes. The legacy `environment` field identifies the selected connection environment; keyless connections use `mainnet` while their `account_environment` is `null`.
- A keyless connection is created with `public_only: true`. It stores no Bybit credentials, uses mainnet public data and exposes no private account/trading operations; `account_environment` is `null`. The MCP service still requires its own Bearer/OAuth authorization. API-key connections retain mainnet, Demo and Testnet support and use their registered environment for private operations.
- `bybit_get_market_snapshot` collects multiple symbols and Kline intervals with optional instruments, OI, funding and orderbook data. `bybit_get_open_interest` and `bybit_get_corporate_actions` also provide direct V5 access.

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

## Market research

For keyless access, choose **主网公开数据（免 Key）** when adding a connection, then include that connection in an authorization Token. A Token can also include several API-key connections, each with independent scopes and operation allowlists. Use a connection's display name to identify it through `bybit_list_connections`, then pass its `connection_id`. A common setup is keyless mainnet data as the default read connection, plus separately authorized Demo and mainnet trading keys. Multi-connection writes always require an explicit key connection; no key is substituted after an error.

The admin API also accepts `POST /admin/api/profiles` with `{"name":"主网公开行情","public_only":true}`. No Bybit credentials or private permissions are accepted for this connection. A keyless connection cannot be converted into a trading connection by expanding its scopes; add the corresponding Key connection instead.

Call `bybit_get_market_snapshot` with the following arguments, using the existing Demo connection for mainnet public data:

```json
{
  "category": "linear",
  "symbols": ["MUUSDT", "MSFTUSDT", "AMZNUSDT", "SNDKUSDT"],
  "intervals": ["60", "D"],
  "kline_limit": 60,
  "include": ["instruments", "open_interest", "funding"]
}
```

The tool supports up to eight symbols, three intervals and 200 candles per interval. Full observations are in `structuredContent`; text content contains a short status summary. A failed component is recorded under that symbol's `errors`, while successful components remain available. A snapshot with no successful ticker is an MCP error. Spot snapshots mark OI/funding as inapplicable and omit those requests. Each upstream call retains the connection's operation permissions and rate pacing.

Series are sorted in ascending time order, and prices and quantities retain V5 decimal strings. By default, unfinished candles are excluded using the exchange response timestamp, including calendar month/week boundaries. Set `closed_klines_only` to `false` to include them. Use each series' `next_end_ms` or OI `nextPageCursor` with the original V5 tools to continue history. Contract daily candles use UTC and include weekend trading; they are not US stock regular-session daily closes. Requests run concurrently, so the result is not an atomic exchange snapshot. Each component includes its exchange timestamp.

Corporate actions currently cover TradFi options, with the upstream 14-day retention window. The tool does not supply company financial reports, economic releases or US regular-session stock history; those require separate sources.

## V5 catalog alignment

The catalog records a pinned official documentation revision. Current Markdown request tables take precedence over stale API Explorer types, required fields and enums, including nested order arrays. Remaining Explorer hints are used only for fields still documented. Options retain their conditional `orderLinkId` requirement. Demo batch orders remain restricted to `linear` and `option` as documented by Bybit.

To update the catalog, check out the desired revision of `bybit-exchange/docs` at `.cache/bybit-docs`, then run:

```sh
python scripts/generate_catalog.py --source .cache/bybit-docs
python scripts/check_catalog.py --source .cache/bybit-docs
python -m pytest -q --tb=short
```

`schema_differences` records conflicting Markdown/Explorer constraints for review; it is not evidence of exchange failure. `check_catalog.py --report <path>` writes a detailed report. CI checks out the catalog's pinned official revision and verifies reproducibility before running tests. Endpoint coverage and schema validation do not certify every account/product combination against the live exchange.

## References

[Bybit V5](https://bybit-exchange.github.io/docs/v5/intro) · [Bybit Demo](https://bybit-exchange.github.io/docs/v5/demo) · [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth) · [MCP authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization)


## Direct Bearer access

Generate an authorization Token in the admin console, then configure the MCP client with the Streamable HTTP URL and header:

```text
Authorization: Bearer bbm_auth_…
```

No OAuth login is needed for this mode. The same per-key environment, scope, operation, expiry and revocation restrictions apply. OAuth remains available for clients using the authorization-code flow. The administrator login Token is reserved for administration.
