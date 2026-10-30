# Validation boundaries

Automated tests cover OAuth discovery, PKCE and code replay, refresh reuse, parent-Token expiry and revocation, per-environment/key operation policies, scope reduction, multi-connection selection, admin authentication, encrypted credential storage, REST/WebSocket authorization, order preview and submission identity, reconciliation, expiry jobs and protected GitOps promotion.

All committed test fixtures use synthetic keys and mocked exchange responses. Passing these tests does not certify every catalogued V5 endpoint against a live account.

Production acceptance records, account observations, host details, screenshots and database backups are retained privately and are not included in this public repository. GitHub CI performs public protocol and revision checks only; it does not receive Bybit credentials or place orders.

Before production use, verify the exact account environment and instrument, the deployed digest, OAuth redirects and revocation behavior, persistence and restore procedures, and the available upstream permissions of each saved key.
