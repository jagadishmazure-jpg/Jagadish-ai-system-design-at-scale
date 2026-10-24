# terraform/ — Azure layout (described, never applied)

**Applying this costs money and needs Jagadish's explicit yes. Nothing here has been applied.**
CI runs only `terraform fmt -check` and `terraform init -backend=false && terraform validate` — no credentials, no plan, no apply.

| Resource | Mirrors (local) |
|---|---|
| VNet with `edge` / `app` / `data` subnets, NSG allowing only app → data on 5432/6379/9000 | compose networks `edge`, `app`, `data` |
| AKS (3 nodes) | `k8s/` manifests, tested on kind |
| PostgreSQL Flexible Server, private | `postgres` service |
| Azure Cache for Redis, private | `redis` service |
| Storage account (private blobs) | `minio` |
