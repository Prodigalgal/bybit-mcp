# Production runtime template

GitHub Actions tests and builds an ARM64 image, then promotes its immutable digest into this service directory in a separate GitOps repository. Argo CD watches that directory with automatic sync and self healing.

The runtime requires one replica, Recreate and the worker that owns the retained SQLite storage. Namespace, StorageClass, PV and PVC are protected with Prune=false,Delete=false. Preserve those protections during image releases.

Application and registry credentials are existing cluster Secrets, outside Git. Back up SQLite and preserve its matching encryption key before migrations or restores. release.json records the source commit and image digest. Revert the GitOps release commit for rollback, after checking pending scheduled cancellation jobs.

The checked-in runtime uses example image, host and node values. The promotion script substitutes deployment configuration supplied through Actions Secrets; gateway and storage references must match your cluster before activation.
