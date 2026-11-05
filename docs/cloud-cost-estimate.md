# Cloud cost estimate for `terraform/` (read-only, nothing applied)

Estimated 2026-10-23 from `terraform/main.tf` on `main` and issue #7. **No Azure resources were created.**
Prices are Pay-As-You-Go list prices in USD for region `eastus2`, taken from the
[Azure Retail Prices API](https://prices.azure.com/api/retail/prices) the same day. The Load Balancer
price comes from the [Load Balancer pricing page](https://azure.microsoft.com/pricing/details/load-balancer/) because the API returned no rows for it.
One month is counted as 730 hours.

## What `terraform apply` would create

| # | Resource (terraform) | Type / SKU | Region | Count | Unit price (source) | Monthly at idle |
|---|---|---|---|---|---|---|
| 1 | `azurerm_resource_group.rg` | Resource group | eastus2 | 1 | free | $0.00 |
| 2 | `azurerm_virtual_network.vnet` + 3 subnets | VNet 10.20.0.0/16 | eastus2 | 1 + 3 | free (no peering) | $0.00 |
| 3 | `azurerm_network_security_group.data` + association | NSG | eastus2 | 1 | free | $0.00 |
| 4 | `azurerm_kubernetes_cluster.aks` control plane | AKS, Free tier (no `sku_tier` set) | eastus2 | 1 | $0 (Standard tier would be $0.10/h) | $0.00 |
| 5 | AKS node VMs (`node_count = 3`) | Standard_D2s_v5, Linux | eastus2 | 3 | $0.096/h (API) | **$210.24** ⏱ |
| 6 | AKS node OS disks (default 128 GB managed Premium) | P10 LRS | eastus2 | 3 | $17.92/mo (API) | **$53.76** ⏱ |
| 7 | AKS outbound load balancer (auto-created in node RG) | Standard LB, ≤5 rules | eastus2 | 1 | $0.025/h (pricing page) | **$18.25** ⏱ |
| 8 | AKS outbound public IP (auto-created) | Standard static IPv4 | eastus2 | 1 | $0.005/h (API) | **$3.65** ⏱ |
| 9 | `azurerm_postgresql_flexible_server.db` compute | B_Standard_B1ms (Burstable) | eastus2 | 1 | $0.017/h (API) | **$12.41** ⏱ |
| 10 | Postgres storage | 32 GB | eastus2 | 1 | $0.115/GB-mo (API) | $3.68 |
| 11 | Postgres backup | LRS, ≤ provisioned size | eastus2 | 1 | free up to 100% of storage; then $0.095/GB-mo | $0.00 |
| 12 | `azurerm_redis_cache.cache` | Basic C0 | eastus2 | 1 | $0.022/h (API) | **$16.06** ⏱ |
| 13 | `azurerm_storage_account.objects` | StorageV2 Standard LRS, Hot | eastus2 | 1 | $0.0184/GB-mo; $0.05 per 10K writes; $0.004 per 10K reads (API) | ~$0.02 (1 GB) |

⏱ = fixed hourly cost: billed every hour the resource exists, even with zero traffic. Stopping a demo does **not** stop these charges. Only `terraform destroy` does, or stopping the AKS cluster and the Postgres server.

## Totals

| Scenario | Estimate |
|---|---|
| **Idle, per month** | **$318.07** |
| **Light demo load, per month** (≈1 GB blobs, ~1M storage ops, <100 GB egress, which is free) | **≈ $319–325** |
| **Run 1 day, then `terraform destroy`** | **≈ $10.50** (24 h of items 5–9, 12, plus a prorated share of disks and storage) |

About 83% of the bill is the three AKS nodes and their disks.

## Apply blockers found while reading (it would not apply cleanly as written)
- The Postgres flexible server has no `administrator_login`/password and no Entra auth block. It also sets `public_network_access_enabled = false` without a `delegated_subnet_id` or private endpoint, so the server cannot be reached privately.
- The Redis cache disables public access but defines no private endpoint, so nothing could connect to it. Also, Basic C0 has no SLA.
- Storage account names are global, so `kestrelobjects` may already be taken.
- The `azurerm` provider version `~> 3.110` needs `subscription_id` set via environment variables.

## Cheapest viable alternative

| Option | What changes | Approx. monthly |
|---|---|---|
| A. Scale-to-zero Container Apps (Consumption, no workload profile) + Postgres B1ms + blob storage, with no Redis (use an in-process cache) | The API runs as a container app with min replicas = 0. The monthly free grant (180,000 vCPU-s, 360,000 GiB-s and 2M requests) covers a light demo. The rates past the grant are $0.000024 per vCPU-s active and $0.40 per 1M requests (API). | **≈ $16** (Postgres $12.41 + $3.68; $0 if the account still has the 12-month free B1ms offer) |
| B. Same as A, with SQLite in the container instead of Postgres | No database server | **≈ $0–1** |
| C. Keep AKS but use 1 × Standard_B2s node and drop Redis | Still has fixed hourly costs from the VM, disk, LB and IP | ≈ $70–75 (B2s price not checked here; look it up before use) |

Recommendation: use **Option A** for a demo. The Kubernetes rollout story is already proven locally on kind in CI.

## Budget alert
Pay-As-You-Go has no spending cap, so set an Azure Cost Management budget on the subscription **before** any apply:
- Option A: **$25/month** budget, with alerts at 50%, 80% and 100% (actual) and 100% (forecast).
- If the AKS layout is ever applied as written: **$350/month** budget with the same alert thresholds, plus a calendar reminder to `terraform destroy`.

Budgets only alert. They do not stop spending.
