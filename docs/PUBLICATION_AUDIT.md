# Public source audit

Publication review: 2026-10-30.

- The public release starts with a clean history and a GitHub noreply author address. The earlier private operational repository remains private.
- Environment files, databases, screenshots, account observations, backups, operator-only helpers and rendered production manifests are excluded from the public tree.
- Local user paths, personal email addresses, server IPs, private repository names and environment-specific host/node/gateway values have been removed from published files. Deployment configuration is supplied separately through Actions Secrets and cluster Secrets.
- Secret scanning uses Gitleaks plus exact comparisons against relevant existing credentials without printing their values. Committed fixtures contain synthetic keys only.
- The automated test suite covers authorization boundaries, trading state and GitOps promotion. Tests do not contact a live trading account.

This audit covers the committed source and reachable history of the public release. Private production evidence and the detailed audit receipt are retained outside this repository.
