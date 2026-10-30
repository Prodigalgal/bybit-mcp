# GitHub Actions and Argo CD

`main` pushes run tests on a GitHub-hosted ARM64 runner, build and push the image, update only the service directory in a separate GitOps repository, and wait for public revision/digest/readiness/OAuth checks. Pull requests run tests without production secrets. Actions are pinned to commit SHAs.

Configure these repository Secrets:

| Secret | Purpose |
| --- | --- |
| REGISTRY_SERVER | OCI registry hostname |
| REGISTRY_USERNAME | Registry publisher username |
| REGISTRY_PASSWORD | Registry publisher credential |
| IMAGE_REPOSITORY | Full registry image repository, without a tag |
| GITOPS_REPOSITORY | Private deployment configuration repository in owner/name form |
| GITOPS_DEPLOY_KEY | Repository-scoped SSH key with write permission to the GitOps repository |
| DEPLOY_NODE_NAME | Kubernetes worker holding the retained SQLite volume |
| GATEWAY_NAME | Existing Envoy Gateway resource name |
| PUBLIC_URL | Canonical HTTPS application origin |

Argo CD uses its own read-only GitOps repository connection. The runtime references existing cluster Secrets `bybit-mcp-env` and `registry-pull`. Bybit credentials, the admin Token and the encryption key are not supplied to CI.

Configure `deploy/argocd/` for your configuration repository and existing cluster permissions. The runtime template assumes an Envoy Gateway and local retained storage; adapt the gateway reference and storage before activating it. The promotion script substitutes the image repository, worker name and public hostname. It preserves one replica, Recreate and Argo prune/delete protection on data resources.

Concurrent releases are serialized. A source revision superseded by newer main is not promoted. Unrelated GitOps pushes are rebased by reapplying only the service directory mutation. CI checks the published source revision and expected digest after deployment; the initial production acceptance should additionally compare the actual Pod image ID with the promoted digest.

Revert the relevant GitOps release commit to roll back. Review pending expiry-cancellation jobs before a rollback across scheduler or authorization changes, and keep the matching database encryption key.
