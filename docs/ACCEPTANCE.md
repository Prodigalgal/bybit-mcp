# Validation boundaries

Automated tests cover OAuth discovery, PKCE and code replay, refresh reuse, parent-Token expiry and revocation, per-environment/key operation policies, scope reduction, multi-connection selection, admin authentication, encrypted credential storage, REST/WebSocket authorization, order preview and submission identity, reconciliation, expiry jobs and protected GitOps promotion.

Market snapshot tests cover public mainnet routing from Demo without decrypting Bybit credentials, Testnet source labeling, concurrent multi-symbol collection, decimal precision, closed candle filtering, calendar boundaries, partial failures, per-component operation policies and revocation during collection. WebSocket trading tests cover rejected acknowledgements, per-order batch failures and unknown outcomes without resubmission. Catalog tests cover current batch product types, hedge position indices, optional defaults and conditional option IDs; CI also checks generation against the pinned official docs checkout.

Keyless connection tests verify no stored Bybit credentials, public-only authorization, account/trading/private-stream denial and consent labeling. A mixed Token test selects keyless data, Demo and mainnet Keys independently, checks private request hosts, rejects unspecified multi-connection writes and enforces per-Key permission reductions. These trading-path checks use mock exchange responses only.

All committed test fixtures use synthetic keys and mocked exchange responses. Passing these tests does not certify every catalogued V5 endpoint against a live account.

Production acceptance records, account observations, host details, screenshots and database backups are retained privately and are not included in this public repository. GitHub CI performs public protocol and revision checks only; it does not receive Bybit credentials or place orders.

Before production use, verify the exact account environment and instrument, the deployed digest, OAuth redirects and revocation behavior, persistence and restore procedures, and the available upstream permissions of each saved key.
