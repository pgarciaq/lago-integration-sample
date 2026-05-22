# lago-integration-sample

Sample integration that syncs [Red Hat Cost Management](https://github.com/project-koku/koku) (Project Koku) data to [Lago](https://getlago.com/) for itemized billing.

Designed for **service providers and resellers** who sell cloud infrastructure (AWS accounts, Azure subscriptions, GCP projects, OpenShift clusters/namespaces/VMs) to external customers and need to generate itemized invoices based on actual usage.

## How it works

```
┌─────────────────────┐        ┌──────────────────────────┐        ┌─────────────────┐
│  Cost Management    │        │  lago-integration-sample │        │      Lago       │
│  (Koku) API         │──GET──>│                          │──POST─>│  Events API     │
│                     │        │  1. Fetch daily costs     │        │                 │
│  reports/aws/costs/ │        │  2. Route to customers   │        │  Aggregates     │
│  reports/ocp/costs/ │        │  3. Push as events       │        │  into invoices  │
└─────────────────────┘        └──────────────────────────┘        └─────────────────┘
```

Each customer in Lago receives only the costs for the resources assigned to them via a YAML configuration file. Costs are matched by account, project, cluster, VM name, or tag values — with glob pattern support.

## Prerequisites

- Python 3.11+
- A running Cost Management (Koku) instance
- A running Lago instance (self-hosted or Cloud)

## Quick Start

### 1. Install

```bash
cd lago-integration-sample
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` to define your customers and their resource assignments:

```yaml
lago:
  api_url: "${LAGO_API_URL:http://localhost:3000}"
  api_key: "${LAGO_API_KEY}"

cost_management:
  base_url: "${KOKU_BASE_URL:http://localhost:8000/api/cost-management/v1}"
  identity: "${KOKU_IDENTITY:}"
  org_id: "org_12345"

sync:
  ocp_include_overhead: true

customers:
  - external_id: "customer_acme"
    name: "Acme Corp"
    currency: "USD"
    resources:
      - provider: aws
        filter:
          account: ["123456789012", "234567890123"]
      - provider: openshift
        filter:
          project: ["acme-*"]
          cluster: ["prod-cluster-01"]

  - external_id: "customer_globex"
    name: "Globex Inc"
    currency: "EUR"
    resources:
      - provider: openshift
        filter:
          tag:team: ["globex-engineering"]
      - provider: aws
        filter:
          tag:customer: ["globex"]
```

Environment variables referenced as `${VAR}` or `${VAR:default}` are interpolated at load time. Set secrets as env vars rather than hardcoding them.

### 3. Bootstrap Lago

Run once to create billable metrics, plan, charges, customers, and subscriptions:

```bash
lago-sync bootstrap
```

### 4. Sync costs

Sync yesterday's costs (default):

```bash
lago-sync sync
```

Sync a full finalized month:

```bash
lago-sync sync --month 2024-01
```

Backfill a date range:

```bash
lago-sync sync --start-date 2024-01-01 --end-date 2024-01-31
```

Re-sync (overwrite previously synced data):

```bash
lago-sync sync --month 2024-01 --force
```

### 5. Reconcile

Verify that Cost Management totals match what was sent to Lago:

```bash
lago-sync reconcile --month 2024-01
```

## Customer-to-Resource Mapping

The `config.yaml` file maps Cost Management resources to billable customers. Each customer can have multiple resource blocks, each specifying a provider and a set of dimension filters.

### Supported filter dimensions

| Provider | Dimensions |
|----------|-----------|
| AWS | `account`, `service`, `region`, `tag:<key>` |
| Azure | `subscription_guid`, `service_name`, `resource_location`, `tag:<key>` |
| GCP | `account`, `service`, `region`, `tag:<key>` |
| OpenShift | `cluster`, `project`, `node`, `vm_name`, `tag:<key>` |

### Glob patterns

Filter values support glob patterns:
- `"acme-*"` matches `acme-frontend`, `acme-api`, `acme-db`
- `"prod-cluster-0?"` matches `prod-cluster-01`, `prod-cluster-02`
- `"*"` matches everything

### Billing scenarios

| Scenario | Configuration |
|----------|--------------|
| Sell full AWS accounts | `filter: { account: ["123456789012"] }` |
| Sell OCP namespaces | `filter: { project: ["customer-ns-*"] }` |
| Sell OCP VMs | `filter: { vm_name: ["customer-vm-*"] }` |
| Bill by tag | `filter: { tag:customer: ["acme"] }` |
| Mixed (accounts + namespaces) | Multiple resource blocks per customer |

## How Costs Map to Lago

| Lago Concept | Maps To |
|---|---|
| Customer | A billable entity from `config.yaml` |
| Subscription | One per customer per provider |
| Billable Metric | `aws_daily_cost`, `azure_daily_cost`, `gcp_daily_cost`, `ocp_daily_cost`, `ocp_daily_overhead` |
| Event | One per resource/service/project per day per customer |
| Charge Filter | Dimensions (service, region, project) for invoice line-item detail |

### OpenShift cost breakdown

For OpenShift, two events are generated per project per day:
- **Direct cost** (`ocp_daily_cost`) — infrastructure + cost model usage + markup
- **Distributed overhead** (`ocp_daily_overhead`) — platform, worker, storage, network overhead

This gives invoice recipients visibility into both their direct resource consumption and allocated shared costs.

### AWS cost type

AWS costs use `calculated_amortized_cost` by default, which spreads Reserved Instance and Savings Plan upfront payments across the benefit period.

## Multi-Currency

Each customer in `config.yaml` specifies their billing currency. The Lago customer is created with this currency. Cost Management already applies exchange rates to cost data, so amounts arrive in the correct currency.

## Credits, Refunds, and Adjustments

This integration only pushes cost data. Credits, discounts, and refunds are managed directly in Lago by the service provider's billing team:

- **Credit Notes** — issue against a finalized invoice for refunds or corrections (Lago UI or API)
- **Wallets** — grant prepaid credits that automatically offset future invoices
- **Void + Regenerate** — void an incorrect invoice and regenerate it

These are out-of-band operations that don't involve Cost Management.

## State Tracking and Idempotency

The sync tracks state in a local SQLite database (`~/.lago-sync/state.db`). Already-synced date ranges are skipped unless `--force` is used.

Events use deterministic `transaction_id` values based on org, customer, provider, dimensions, and date. Re-syncing the same data produces identical IDs, so Lago deduplicates automatically.

**Warning:** Do not change `group_by` dimensions (i.e., the filter keys in `config.yaml`) between syncs for the same billing period without using `--force`. Changing dimensions changes transaction IDs and may cause duplicate billing.

## Reconciliation

The `reconcile` command fetches Cost Management totals for a month and compares them against what was routed to each customer:

```
======================================================================
  Reconciliation Report: 2024-01
======================================================================

  [OK]      customer_acme                  | aws        |     $12,345.67
  [OK]      customer_globex                | openshift  |      $4,567.89
  [WARNING] __unmatched__                  | aws        |        $234.56
         $234.56 in costs not matched to any customer

======================================================================
```

Unmatched costs indicate resources in Cost Management that aren't assigned to any customer in your config.

## Scheduling

For production, schedule the sync to run after Cost Management finalizes the month:

```bash
# Sync the previous month on the 2nd of each month
0 6 2 * * cd /path/to/lago-integration-sample && lago-sync sync --month $(date -d "last month" +\%Y-\%m)
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src/ tests/
```

### Project structure

```
lago-integration-sample/
├── pyproject.toml              # Dependencies and build config
├── config.example.yaml         # Template customer-to-resource mapping
├── src/
│   ├── config.py              # YAML config loader with env var interpolation
│   ├── koku_client.py         # Cost Management API client
│   ├── lago_sync.py           # Event mapping with per-customer routing
│   ├── bootstrap.py           # Lago entity provisioning (metrics, plan, charges, customers)
│   ├── reconcile.py           # Reconciliation logic
│   ├── state.py               # SQLite sync state tracker
│   ├── retry.py               # Retry with exponential backoff
│   └── main.py                # CLI (bootstrap, sync, reconcile)
└── tests/
    ├── fixtures/              # Sample Cost Management API responses
    ├── test_config.py
    ├── test_koku_client.py
    ├── test_lago_sync.py
    └── test_reconcile.py
```

## License

Apache-2.0
