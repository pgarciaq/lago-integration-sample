# lago-integration-sample

Integration service that syncs [Red Hat Cost Management](https://github.com/project-koku/koku) (Project Koku) data to [Lago](https://getlago.com/) for itemized billing.

## What it does

This service periodically reads cost data from the Cost Management REST API and pushes it as usage events to Lago, which then generates itemized invoices per resource/service/project.

```
┌─────────────────────┐        ┌──────────────────────────┐        ┌─────────────────┐
│  Cost Management    │        │  lago-integration-sample │        │      Lago       │
│  (Koku) API         │──GET──>│                          │──POST─>│  Events API     │
│                     │        │  Fetches daily costs,    │        │                 │
│  reports/aws/costs/ │        │  maps to Lago events,    │        │  Aggregates     │
│  reports/ocp/costs/ │        │  pushes in batches       │        │  into invoices  │
└─────────────────────┘        └──────────────────────────┘        └─────────────────┘
```

### How it maps Cost Management data to Lago

| Lago Concept | Maps To |
|---|---|
| Customer | Koku org (one consolidated invoice per org) |
| Subscription | One per cloud provider (aws, azure, gcp, openshift) |
| Billable Metric | `aws_daily_cost`, `azure_daily_cost`, `gcp_daily_cost`, `ocp_daily_cost`, `ocp_daily_overhead` |
| Event | One per resource/service/project per day |
| Charge Filters | Service, region, project, cluster (for line-item detail on invoices) |

For OpenShift, two events are generated per project per day:
- **Direct cost** (`ocp_daily_cost`) — raw infrastructure + cost model usage + markup
- **Distributed overhead** (`ocp_daily_overhead`) — platform, worker, storage, network, and GPU overhead allocated to the project

---

## Prerequisites

- **Python 3.11+**
- **A running Cost Management (Koku) instance** — either the full docker-compose stack (`make docker-up` from the koku repo) or a remote deployment
- **A running Lago instance** — self-hosted via [docker-compose](https://getlago.com/docs/guide/introduction/welcome) or [Lago Cloud](https://app.getlago.com)

---

## Quick Start

### 1. Install

```bash
cd lago-integration-sample
pip install -e ".[dev]"
```

This installs the `lago-sync` CLI command and all dependencies (`lago-python-client`, `httpx`, `pydantic`).

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your values:

```bash
# Point to your Cost Management API
LAGO_SYNC_KOKU_BASE_URL=http://localhost:8000/api/cost-management/v1

# For local dev with DEVELOPMENT=True in koku, leave empty.
# For cloud/production, set the base64-encoded x-rh-identity header.
LAGO_SYNC_KOKU_IDENTITY=

# Your Lago API key (found in Lago UI → Developers → API Keys)
LAGO_SYNC_LAGO_API_KEY=your_lago_api_key_here

# Lago URL (self-hosted default; use https://api.getlago.com for Cloud)
LAGO_SYNC_LAGO_API_URL=http://localhost:3000

# Your organization identifier (used as the Lago customer external_id)
LAGO_SYNC_ORG_ID=my_org_123

# Which providers to sync
LAGO_SYNC_PROVIDERS=["aws","openshift"]
```

### 3. Bootstrap Lago

Run once to create the required Lago entities (billable metrics, plan, customer, subscriptions):

```bash
lago-sync bootstrap
```

This creates:
- **5 billable metrics** (one per provider + OCP overhead), each using SUM aggregation on `cost_amount`
- **1 plan** ("Cloud Cost Pass-Through") with $0 base fee — costs are entirely usage-based
- **1 customer** (your org)
- **1 subscription per provider** linking the customer to the plan

### 4. Run a sync

Sync yesterday's costs (the typical daily operation):

```bash
lago-sync sync
```

Backfill a specific date range:

```bash
lago-sync sync --start-date 2024-01-01 --end-date 2024-01-31
```

The sync is **idempotent** — re-running for the same dates produces identical `transaction_id` values, so Lago deduplicates them automatically.

---

## Configuration Reference

All settings are read from environment variables prefixed with `LAGO_SYNC_`:

| Variable | Description | Default |
|---|---|---|
| `LAGO_SYNC_KOKU_BASE_URL` | Cost Management API base URL | `http://localhost:8000/api/cost-management/v1` |
| `LAGO_SYNC_KOKU_IDENTITY` | Base64-encoded `x-rh-identity` header | (empty — works with koku dev mode) |
| `LAGO_SYNC_LAGO_API_KEY` | Lago API key | (required) |
| `LAGO_SYNC_LAGO_API_URL` | Lago base URL | `http://localhost:3000` |
| `LAGO_SYNC_ORG_ID` | Koku org ID → Lago customer external_id | (required) |
| `LAGO_SYNC_PROVIDERS` | JSON list of providers to sync | `["aws","azure","gcp","openshift"]` |
| `LAGO_SYNC_AWS_GROUP_BY` | Dimensions to group AWS costs by | `["account","service","region"]` |
| `LAGO_SYNC_AZURE_GROUP_BY` | Dimensions to group Azure costs by | `["subscription_guid","service_name","resource_location"]` |
| `LAGO_SYNC_GCP_GROUP_BY` | Dimensions to group GCP costs by | `["account","service","region"]` |
| `LAGO_SYNC_OCP_GROUP_BY` | Dimensions to group OCP costs by | `["cluster","project"]` |
| `LAGO_SYNC_OCP_INCLUDE_OVERHEAD` | Include distributed overhead as separate line items | `true` |

---

## Example: What ends up in Lago

After a sync, Lago receives events like:

**AWS event** (one per account/service/region/day):
```json
{
  "transaction_id": "aws_123456789012_AmazonEC2_us-east-1_2024-01-15_direct",
  "external_subscription_id": "my_org_123_aws",
  "code": "aws_daily_cost",
  "timestamp": 1705276800,
  "properties": {
    "cost_amount": "1320.55",
    "cost_raw": "1200.50",
    "cost_markup": "120.05",
    "account": "123456789012",
    "service": "AmazonEC2",
    "region": "us-east-1"
  }
}
```

**OCP direct cost event** (one per cluster/project/day):
```json
{
  "transaction_id": "openshift_prod-cluster_my-app_2024-01-15_direct",
  "external_subscription_id": "my_org_123_openshift",
  "code": "ocp_daily_cost",
  "timestamp": 1705276800,
  "properties": {
    "cost_amount": "950.00",
    "cost_raw": "500.00",
    "cost_usage": "400.00",
    "cost_markup": "50.00",
    "cluster": "prod-cluster",
    "project": "my-app"
  }
}
```

**OCP overhead event** (one per cluster/project/day, when overhead > 0):
```json
{
  "transaction_id": "openshift_prod-cluster_my-app_2024-01-15_overhead",
  "external_subscription_id": "my_org_123_openshift",
  "code": "ocp_daily_overhead",
  "timestamp": 1705276800,
  "properties": {
    "cost_amount": "115.00",
    "platform": "75.00",
    "worker": "25.00",
    "network": "10.00",
    "storage": "5.00",
    "cluster": "prod-cluster",
    "project": "my-app"
  }
}
```

Lago aggregates these events per billing period (monthly by default) and generates an invoice with itemized lines per charge filter combination.

---

## Running with Cron / Systemd

For production use, schedule the sync to run daily after Cost Management finishes its nightly processing (typically a few hours after midnight UTC):

**Cron:**
```cron
0 6 * * * cd /path/to/lago-integration-sample && lago-sync sync
```

**Systemd timer:**
```ini
# /etc/systemd/system/lago-sync.service
[Unit]
Description=Sync Cost Management data to Lago

[Service]
Type=oneshot
WorkingDirectory=/path/to/lago-integration-sample
EnvironmentFile=/path/to/lago-integration-sample/.env
ExecStart=/path/to/venv/bin/lago-sync sync
```

```ini
# /etc/systemd/system/lago-sync.timer
[Unit]
Description=Daily Lago sync

[Timer]
OnCalendar=*-*-* 06:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

---

## Development

### Install dev dependencies

```bash
pip install -e ".[dev]"
```

### Run tests

```bash
pytest
```

### Lint

```bash
ruff check src/ tests/
ruff format src/ tests/
```

### Project structure

```
lago-integration-sample/
├── pyproject.toml              # Project metadata and dependencies
├── .env.example                # Template configuration
├── src/
│   ├── config.py              # Settings from environment variables
│   ├── koku_client.py         # Koku Cost Management API client
│   ├── lago_sync.py           # Event mapping and batch push to Lago
│   ├── bootstrap.py           # One-time Lago entity provisioning
│   └── main.py                # CLI entrypoint (bootstrap / sync commands)
└── tests/
    ├── fixtures/              # Sample Koku API responses
    │   ├── aws_costs_response.json
    │   └── ocp_costs_response.json
    ├── test_koku_client.py    # API client tests
    └── test_lago_sync.py      # Event mapping tests
```

---

## Extending

### Adding a new provider

1. Add the report path in `src/koku_client.py` → `PROVIDER_REPORT_PATHS`
2. Add group_by config in `src/config.py`
3. Add the metric code in `src/lago_sync.py` → `PROVIDER_METRIC_CODE`
4. Add a `BillableMetric` in `src/bootstrap.py` → `BILLABLE_METRICS`
5. Add the provider name to your `LAGO_SYNC_PROVIDERS` env var

### Customizing invoice line items

Lago [charge filters](https://getlago.com/docs/guide/plans/charges/charge-filters) control how events are grouped on invoices. After bootstrapping, configure filters in the Lago UI or API on the plan's charges to group by `service`, `region`, `project`, etc.

---

## License

Apache-2.0
