# lago-integration-sample

Integration service that syncs [Red Hat Cost Management](https://github.com/project-koku/koku) (Project Koku) data to [Lago](https://getlago.com/) for itemized billing.

## What it does

This service periodically reads cost data from the Cost Management REST API and pushes it as usage events to Lago, which then generates itemized invoices per resource/service/project.

```
Cost Management API ──> lago-integration-sample ──> Lago Events API ──> Invoices
```

### Data mapping

| Lago Concept | Maps To |
|---|---|
| Customer | Koku org (one consolidated invoice per org) |
| Subscription | One per cloud provider (aws, azure, gcp, openshift) |
| Billable Metric | `aws_daily_cost`, `azure_daily_cost`, `gcp_daily_cost`, `ocp_daily_cost`, `ocp_daily_overhead` |
| Event | One per resource/service/project per day (direct cost + overhead for OCP) |

## Setup

### Prerequisites

- Python 3.11+
- A running Cost Management (Koku) instance
- A running Lago instance (self-hosted or Cloud)

### Install

```bash
pip install -e ".[dev]"
```

### Configure

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Key variables:

| Variable | Description | Default |
|---|---|---|
| `LAGO_SYNC_KOKU_BASE_URL` | Cost Management API base URL | `http://localhost:8000/api/cost-management/v1` |
| `LAGO_SYNC_KOKU_IDENTITY` | Base64-encoded `x-rh-identity` header | (empty — uses dev mode) |
| `LAGO_SYNC_LAGO_API_KEY` | Lago API key | (required) |
| `LAGO_SYNC_LAGO_API_URL` | Lago API URL | `http://localhost:3000` |
| `LAGO_SYNC_ORG_ID` | Koku org ID (becomes Lago customer external_id) | (required) |
| `LAGO_SYNC_PROVIDERS` | Comma-separated list of providers to sync | `aws,azure,gcp,openshift` |

## Usage

### 1. Bootstrap Lago entities

Run once to create billable metrics, plans, customer, and subscriptions:

```bash
lago-sync bootstrap
```

### 2. Run a sync

Sync yesterday's costs (default):

```bash
lago-sync sync
```

Backfill a date range:

```bash
lago-sync sync --start-date 2024-01-01 --end-date 2024-01-31
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache-2.0
