# Lago ↔ Cost Management Integration

Syncs cost data from **Red Hat Cost Management** (Project Koku) to **Lago** for generating itemized invoices. Designed for service providers that bill multiple customers for shared cloud and OpenShift infrastructure.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         lago-sync CLI                                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  config.yaml ─── defines customers + resource filters               │
│       │                                                             │
│       ▼                                                             │
│  ┌─────────────┐   HTTP/REST    ┌───────────────────────────────┐  │
│  │ koku_client │ ◄──────────────│ Cost Management API           │  │
│  │  (httpx)    │                │ /api/cost-management/v1/      │  │
│  └──────┬──────┘                │ reports/{aws,azure,gcp,ocp}/  │  │
│         │                       └───────────────────────────────┘  │
│         │ cost data (JSON)                                          │
│         ▼                                                           │
│  ┌─────────────┐                                                   │
│  │  lago_sync  │ ── routes costs to customers using filters         │
│  │             │ ── generates Lago Event per cost line item          │
│  └──────┬──────┘                                                   │
│         │                                                           │
│         │ BatchEvent                                                │
│         ▼                                                           │
│  ┌─────────────┐   HTTP/REST    ┌───────────────────────────────┐  │
│  │ Lago SDK    │ ──────────────►│ Lago API                      │  │
│  │             │                │ /api/v1/events/batch           │  │
│  └─────────────┘                └───────────────────────────────┘  │
│                                                                     │
│  ┌─────────────┐                                                   │
│  │  state.db   │ ── SQLite: tracks what has been synced             │
│  └─────────────┘                                                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/pgarciaq/lago-integration-sample.git
cd lago-integration-sample
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your credentials and customer definitions
```

### 3. Bootstrap Lago

Creates billable metrics, plan, charges, customers, and subscriptions in Lago:

```bash
lago-sync bootstrap
```

### 4. Sync cost data

```bash
# Sync a specific month (most common)
lago-sync sync --month 2024-01

# Sync yesterday (default behavior for daily cron jobs)
lago-sync sync

# Preview what would be sent without pushing (USE THIS FIRST)
lago-sync sync --month 2024-01 --dry-run

# Force re-sync of previously synced data
lago-sync sync --month 2024-01 --force
```

### 5. Reconcile

Compare Cost Management totals against Lago's recorded usage:

```bash
lago-sync reconcile --month 2024-01
```

---

## How It Works: End-to-End Data Flow

Understanding the **full lifecycle** is essential for troubleshooting.

### Phase 1: Cost Management processes cloud bills

Before this integration can do anything, Cost Management must have **processed and summarized** data for the target period:

1. Cloud provider sends billing data (AWS CUR, Azure export, GCP BigQuery)
2. Or: OpenShift cluster operator uploads usage reports
3. Koku (Cost Management backend) downloads, converts to Parquet, runs through Trino/PostgreSQL summarization pipeline
4. Summarized data becomes available via the REST API

**Important**: Data appears in the API 24-48 hours after the usage date. End-of-month data may continue reprocessing for 2-3 days into the next month as cloud providers finalize bills.

### Phase 2: lago-sync fetches and routes

1. `lago-sync` calls the Cost Management report API with daily resolution
2. The API returns a nested JSON tree grouped by dimensions (account, service, cluster, project, etc.)
3. `lago-sync` walks the tree, and for each leaf cost item:
   - Checks which customer's resource filters match the item's dimensions
   - Generates a Lago `Event` with a deterministic `transaction_id`
   - Multiple customers can match the same item (shared resources)
   - Items matching **no** customer are logged as warnings

### Phase 3: Lago processes events into invoices

After events are pushed to Lago:

1. Events accumulate in the current **billing period** (monthly, aligned to calendar)
2. At month-end (or manually), Lago **finalizes** the billing period
3. A **draft invoice** is generated per customer containing line items from the billable metrics
4. The invoice can be reviewed, then **finalized** to send to the customer
5. Lago can generate PDF invoices, send emails, and track payment

**The integration handles Phase 2 only.** Phase 1 is Cost Management's responsibility, Phase 3 is Lago's.

---

## Configuration Reference

### `config.yaml` structure

```yaml
# ─── Lago connection ───────────────────────────────────────────────
lago:
  api_url: "http://localhost:3000"      # Lago API base URL
  api_key: "${LAGO_API_KEY}"            # API key (Settings → Developers → API Keys)

# ─── Cost Management connection ────────────────────────────────────
cost_management:
  base_url: "http://localhost:8000/api/cost-management/v1"
  identity: "${KOKU_IDENTITY}"          # base64-encoded x-rh-identity header
  org_id: "my-org"                      # Unique org identifier for state tracking

# ─── Sync behavior ────────────────────────────────────────────────
sync:
  ocp_include_overhead: true            # Include distributed platform/worker costs

  # Controls what appears as separate line items on the Lago invoice.
  # Each unique combination of these properties becomes one fee (line item).
  invoice_group_by:
    aws: ["account", "service"]         # One line per AWS account + service
    azure: ["subscription_guid", "service_name"]
    gcp: ["account", "service"]
    openshift: ["project", "cluster"]   # One line per namespace + cluster

# ─── Customer definitions ──────────────────────────────────────────
customers:
  - external_id: "customer_acme"        # Becomes the Lago customer ID
    name: "Acme Corp"                   # Display name on invoices
    currency: "USD"                     # ISO 4217 currency code
    resources:                          # What this customer is billed for:
      - provider: aws
        filter:
          account: ["123456789012"]     # AWS account IDs
      - provider: openshift
        filter:
          project: ["acme-*"]           # Glob patterns supported
          cluster: ["prod-cluster-01"]

  - external_id: "customer_globex"
    name: "Globex Corp"
    currency: "EUR"
    resources:
      - provider: aws
        filter:
          account: ["987654321098"]
      - provider: azure
        filter:
          subscription_guid: ["sub-abc-123"]
```

### Environment variable interpolation

Any string value in `config.yaml` supports `${VAR}` or `${VAR:default}` syntax:

```yaml
lago:
  api_key: "${LAGO_API_KEY}"            # Required: fails if not set
  api_url: "${LAGO_URL:http://localhost:3000}"  # Uses default if not set
```

### Supported filter dimensions

| Provider   | Dimension              | Example values                                |
|------------|------------------------|-----------------------------------------------|
| `aws`      | `account`              | `["123456789012"]`                            |
| `aws`      | `service`              | `["AmazonEC2", "AmazonS3"]`                  |
| `aws`      | `region`               | `["us-east-1", "eu-west-*"]`                 |
| `azure`    | `subscription_guid`    | `["sub-abc-123"]`                             |
| `azure`    | `service_name`         | `["Virtual Machines"]`                        |
| `azure`    | `resource_location`    | `["eastus", "westeurope"]`                    |
| `gcp`      | `account`              | `["my-project-id"]`                           |
| `gcp`      | `service`              | `["Cloud Storage"]`                           |
| `gcp`      | `region`               | `["us-central1"]`                             |
| `openshift`| `cluster`              | `["prod-cluster-01"]`                         |
| `openshift`| `project`              | `["team-a-*", "shared-services"]`             |
| `openshift`| `node`                 | `["worker-*"]`                                |

**Glob patterns**: Filter values support `*` (any characters) and `?` (single character) matching via Python's `fnmatch`. This lets you match namespaces like `team-a-*` without listing every project.

---

## Cost Management Data Model (for Lago consultants)

If you know Lago but not Cost Management, this section explains the data source.

### What Cost Management tracks

Cost Management aggregates cloud spending from multiple sources:

| Provider   | Data source                    | Key identifier          |
|------------|--------------------------------|-------------------------|
| AWS        | Cost and Usage Report (CUR)    | Account ID              |
| Azure      | Azure Cost Management export   | Subscription GUID       |
| GCP        | BigQuery billing export        | Project ID              |
| OpenShift  | Operator-reported metrics      | Cluster ID + Namespace  |

### The Report API response structure

When `lago-sync` calls `/api/cost-management/v1/reports/aws/costs/`, it gets:

```json
{
  "data": [
    {
      "date": "2024-01-15",
      "accounts": [
        {
          "account": "123456789012",
          "services": [
            {
              "service": "AmazonEC2",
              "values": [
                {
                  "account": "123456789012",
                  "service": "AmazonEC2",
                  "cost": {
                    "raw": {"value": 150.00, "units": "USD"},
                    "markup": {"value": 0.00, "units": "USD"},
                    "usage": {"value": 0.00, "units": "USD"},
                    "total": {"value": 150.00, "units": "USD"}
                  }
                }
              ]
            }
          ]
        }
      ]
    }
  ],
  "meta": { "count": 1, "total": { "cost": { "total": {"value": 150.00} } } }
}
```

The response is **nested by group_by dimensions**. `lago-sync` walks this tree recursively.

### Cost types

| Field                          | Meaning                                                       |
|--------------------------------|---------------------------------------------------------------|
| `cost.raw`                     | The base cost before any markup                               |
| `cost.markup`                  | Markup applied by cost models (% configured in CM)            |
| `cost.usage`                   | Usage-based cost model rates (OCP only)                       |
| `cost.total`                   | raw + markup + usage                                          |
| `cost.platform_distributed`    | OCP: platform overhead allocated to project                   |
| `cost.worker_unallocated_distributed` | OCP: unallocated worker cost distributed to project  |

### AWS: `calculated_amortized_cost`

For AWS, this integration requests `cost_type=calculated_amortized_cost`. This means:
- Reserved Instance upfront payments are spread across the reservation period
- Savings Plan discounts are amortized
- You get the "true economic cost" rather than the cash-flow timing of payments

### The `x-rh-identity` header

Cost Management uses a base64-encoded JSON header for authentication:

```json
{
  "identity": {
    "org_id": "12345",
    "type": "User",
    "user": {
      "username": "billing-integration",
      "email": "billing@example.com",
      "is_org_admin": true
    }
  }
}
```

Encode it: `echo '{"identity": {...}}' | base64 -w0`

In production (console.redhat.com), this is provided by the platform's authentication layer. For local development, the Koku `DEVELOPMENT=True` mode accepts any well-formed identity.

---

## Lago Billing Model (for Cost Management consultants)

If you know Cost Management but not Lago, this section explains the billing destination.

### Core Lago concepts

| Concept              | Purpose                                                     |
|----------------------|-------------------------------------------------------------|
| **Billable Metric**  | Defines what is being measured (e.g., "aws_daily_cost")     |
| **Plan**             | Groups charges together into a pricing structure             |
| **Charge**           | Links a metric to a plan with a pricing model               |
| **Customer**         | The entity being billed                                     |
| **Subscription**     | Connects a customer to a plan (activates billing)           |
| **Event**            | A usage data point (what this integration pushes)           |

### How billing flows

```
Events arrive → accumulate in current billing period → period closes → draft invoice → finalize → send
```

1. **Events** are metered in real-time as they arrive
2. At the end of the **billing period** (monthly), Lago calculates totals
3. A **draft invoice** is generated — you can review it in the Lago UI
4. **Finalizing** the invoice locks it and triggers downstream actions (PDF, email, webhook)

### What this integration creates in Lago

| Entity               | Code/ID pattern                    | Example                    |
|----------------------|------------------------------------|----------------------------|
| Billable Metric      | `{provider}_daily_cost`            | `aws_daily_cost`           |
| Billable Metric      | `ocp_daily_overhead`               | (OpenShift only)           |
| Plan                 | `cloud_cost_passthrough`           | One plan for all           |
| Charge               | 1:1 per metric, standard model    | amount = "1" (passthrough) |
| Customer             | `{customer.external_id}`           | `customer_acme`            |
| Subscription         | `{customer_id}_{provider}`         | `customer_acme_aws`        |

### The "passthrough" charge model

Charges are configured with `charge_model: standard` and `amount: 1`. This means:
- 1 unit of the metric costs $1 (or €1, etc.)
- The `cost_amount` property in events contains the actual dollar value
- `sum_agg` on `cost_amount` gives the total cost
- Effective rate: 1:1 pass-through of Cost Management costs to invoice

### Invoice itemization (`pricing_group_keys`)

By default, charges are created with `pricing_group_keys` that produce **per-dimension line items** on the invoice. For example, with the default OpenShift grouping (`["project", "cluster"]`), an invoice looks like:

```
OCP Daily Cost (project=frontend, cluster=prod-01) ......... $  420.00
OCP Daily Cost (project=backend, cluster=prod-01) .......... $  890.00
OCP Daily Cost (project=monitoring, cluster=prod-01) ....... $  150.00
OCP Daily Overhead (project=frontend, cluster=prod-01) ..... $   63.00
OCP Daily Overhead (project=backend, cluster=prod-01) ...... $  133.50
─────────────────────────────────────────────────────────────────────
Total                                                        $1,656.50
```

For AWS with default grouping (`["account", "service"]`):

```
AWS Daily Cost (account=123456789012, service=AmazonEC2) ... $2,340.00
AWS Daily Cost (account=123456789012, service=AmazonS3) .... $   89.50
AWS Daily Cost (account=123456789012, service=AmazonRDS) ... $  620.00
─────────────────────────────────────────────────────────────────────
Total                                                        $3,049.50
```

**Customizing granularity** via `config.yaml`:

```yaml
sync:
  invoice_group_by:
    # More granular: add region
    aws: ["account", "service", "region"]
    # Less granular: only by project
    openshift: ["project"]
    # No itemization: single line item total
    gcp: []
```

If you change `invoice_group_by`, you must re-run `lago-sync bootstrap` to update the charges in Lago. Existing charges from a previous bootstrap will need to be deleted first (in the Lago UI or via API), since the charge endpoint returns 422 for duplicates.

### Deduplication via `transaction_id`

Every event has a deterministic `transaction_id`:
```
{org_id}_{customer_id}_{provider}_{dimension_key}_{date}_{type}
```

If you re-push the same data, Lago deduplicates by `transaction_id`. This means:
- Safe to re-run `lago-sync sync --force` without double-billing
- Changing `group_by` dimensions will change dimension keys → new transaction IDs → **potential duplicates**

**If you change group_by dimensions**: Delete the state database (`~/.lago-sync/state.db`) and ensure the previous billing period has been finalized before re-syncing.

### Multi-currency

Each customer can have a different currency (set in `config.yaml`). Lago handles currency at the customer level. The amounts from Cost Management are always in USD; if a customer uses EUR, you should configure exchange rates in Lago or handle conversion externally.

---

## Operational Guide

### Scheduling

**Daily sync (cron):**
```bash
# Sync yesterday's data at 6 AM (give CM time to process)
0 6 * * * cd /path/to/lago-integration-sample && lago-sync sync 2>&1 >> /var/log/lago-sync.log
```

**Monthly full sync (for finalization):**
```bash
# On the 3rd of each month, sync the previous month completely
0 8 3 * * cd /path/to/lago-integration-sample && lago-sync sync --month $(date -d "last month" +\%Y-\%m) --force 2>&1 >> /var/log/lago-sync.log
```

**Why the 3rd?** Cloud providers (especially AWS) may finalize CUR data 1-2 days into the next month. Running on the 3rd gives you more complete data.

**systemd timer:**
```ini
# /etc/systemd/system/lago-sync.timer
[Unit]
Description=Daily Lago sync

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### State database

Sync state is stored in `~/.lago-sync/state.db` (SQLite). To inspect:

```bash
sqlite3 ~/.lago-sync/state.db "SELECT * FROM sync_log ORDER BY sync_date DESC LIMIT 20;"
```

To reset state (forces full re-sync):
```bash
rm ~/.lago-sync/state.db
```

### Reconciliation workflow

Run reconciliation after each monthly sync, before finalizing invoices in Lago:

```bash
$ lago-sync reconcile --month 2024-01

======================================================================
  Reconciliation Report: 2024-01
======================================================================

  [     OK] customer_acme                  | aws        | $     4,523.17
  [     OK] customer_acme                  | openshift  | $     1,205.44
  [     OK] customer_globex                | aws        | $     8,901.22
  [WARNING] __unmatched__                  | aws        | $       342.55
            $342.55 in costs not matched to any customer filter

======================================================================
```

**What the statuses mean:**
- `OK` — Cost Management and Lago totals match within $0.01
- `MISMATCH` — Totals differ (investigate before finalizing)
- `WARNING` — Costs exist that no customer filter matches
- `NO DATA` — No cost data in Cost Management for this period

### Handling mismatches

1. **Small deltas ($0.01-$1.00)**: Usually floating-point rounding. Safe to ignore.
2. **Large deltas**: Check if:
   - Cost Management reprocessed data after the sync ran
   - A filter in config.yaml is too broad/narrow
   - The `group_by` dimensions changed since last sync
3. **Unmatched costs**: Add the missing accounts/projects to a customer's filter in `config.yaml`, then re-sync with `--force`.

---

## Troubleshooting

### "No data returned from Cost Management"

**Symptoms:** Sync completes with 0 events, no errors.

**Causes:**
1. Data hasn't been processed yet for that date range
2. Wrong `base_url` or `identity` in config.yaml
3. The org doesn't have any sources configured in Cost Management

**Diagnosis:**
```bash
# Test the API directly
curl -s -H "x-rh-identity: $(echo $KOKU_IDENTITY)" \
  "http://localhost:8000/api/cost-management/v1/reports/aws/costs/?filter[start_date]=2024-01-01&filter[end_date]=2024-01-31" \
  | python -m json.tool | head -50
```

If the response has `"data": []`, the issue is on the Cost Management side (data not yet ingested).

### "X cost items did not match any customer filter"

**Symptoms:** Warning in logs about unmatched leaves.

**Causes:**
1. The account/project/cluster in Cost Management isn't listed in any customer's `filter`
2. A glob pattern doesn't match the actual dimension values

**Diagnosis:**
```bash
# Run in dry-run mode to see what dimensions exist in the data
lago-sync sync --month 2024-01 --dry-run 2>&1 | grep "DRY RUN"

# Inspect the raw API response to see actual dimension values
curl -s -H "x-rh-identity: $(echo $KOKU_IDENTITY)" \
  "http://localhost:8000/api/cost-management/v1/reports/aws/costs/?filter[start_date]=2024-01-01&filter[end_date]=2024-01-31&group_by[account]=*" \
  | python -m json.tool
```

### "Failed to create charge" during bootstrap

**Symptoms:** Warning about 4xx errors during `lago-sync bootstrap`.

**Causes:**
1. Lago's charge creation endpoint URL has changed between versions
2. The plan doesn't exist yet (bootstrap creates it in order, but may have failed earlier)

**Fix:**
- Check Lago version compatibility
- Verify the plan exists: `curl -H "Authorization: Bearer $LAGO_API_KEY" http://localhost:3000/api/v1/plans/cloud_cost_passthrough`
- Create charges manually in the Lago UI if the API endpoint differs

### "Batch X failed (Y events lost)"

**Symptoms:** Some events fail to push, others succeed.

**Causes:**
1. Transient Lago API error (will retry 3x automatically)
2. Lago rate limiting (429)
3. Network connectivity issue

**Fix:**
- Check Lago server health
- Re-run with `--force` to retry failed dates
- If persistent, check `lago-sync` logs for the specific error code

### "Configuration error: lago.api_key is required"

**Symptoms:** CLI exits immediately with a config error.

**Fix:**
- Set the environment variable: `export LAGO_API_KEY=your_key_here`
- Or set it directly in config.yaml (not recommended for production)
- Find your API key in Lago: Settings → Developers → API Keys

### State database locked

**Symptoms:** `sqlite3.OperationalError: database is locked`

**Cause:** Another `lago-sync` process is running concurrently.

**Fix:**
- Ensure only one instance runs at a time (use `flock` in cron)
- Example: `flock -n /tmp/lago-sync.lock lago-sync sync --month 2024-01`

---

## Extending the Integration

### Adding a new provider

1. Add the provider to `PROVIDER_REPORT_PATHS` in `koku_client.py`
2. Add the metric code to `PROVIDER_METRIC_CODE` in `lago_sync.py`
3. Add a `BillableMetric` definition in `bootstrap.py`
4. Add default group_by dimensions in `_default_group_by()` in `config.py`
5. Run `lago-sync bootstrap` to create the new metric/charge
6. Add customer filter entries in `config.yaml`

### Customizing invoice line items

The billable metrics use `sum_agg` on `cost_amount`. To create more granular invoices:

1. Create additional billable metrics (e.g., `aws_ec2_cost`, `aws_s3_cost`)
2. Map them in `PROVIDER_METRIC_CODE` or add conditional logic in `_leaf_to_events()`
3. Each metric becomes a separate line item on the Lago invoice

### Adding tag-based billing

Cost Management supports grouping by tags. To bill by tags:

1. Add `tag:your_tag_key` to a customer's filter dimensions in config.yaml:
   ```yaml
   resources:
     - provider: aws
       filter:
         "tag:environment": ["production"]
   ```
2. The integration will include tag dimensions in `group_by` and match on them

### Handling credits and refunds

This integration **does not** handle credits or refunds. Those are billing adjustments that happen in Lago:

1. **Credit notes**: Create manually in Lago UI when a refund is needed
2. **Coupons**: Use Lago's coupon feature for recurring discounts
3. **Adjustments**: Edit draft invoices before finalizing

If Cost Management shows negative cost values (rare, but possible with RI refunds), they will flow through as negative `cost_amount` events, reducing the invoice total naturally.

---

## Development

### Project structure

```
lago-integration-sample/
├── src/
│   ├── main.py          # CLI entrypoint and orchestration
│   ├── config.py        # YAML config loading with validation
│   ├── koku_client.py   # Cost Management API client (with retry)
│   ├── lago_sync.py     # Event generation and customer routing
│   ├── bootstrap.py     # Lago entity provisioning
│   ├── reconcile.py     # Koku vs Lago comparison
│   ├── retry.py         # Tenacity-based retry decorator
│   └── state.py         # SQLite sync state tracking
├── tests/
│   ├── test_config.py
│   ├── test_koku_client.py
│   ├── test_lago_sync.py
│   └── test_reconcile.py
├── config.example.yaml  # Configuration template
├── pyproject.toml       # Dependencies and build config
└── README.md            # This file
```

### Running tests

```bash
pip install -e ".[dev]"
pytest -v
```

### Linting

```bash
ruff check src/ tests/
ruff format src/ tests/
```

### Local development setup

To develop against local instances:

1. **Cost Management**: `cd ~/dev/koku && make docker-up-min` (starts Koku API on `localhost:8000`)
2. **Lago**: Follow [Lago's Docker setup](https://docs.getlago.com/guide/self-hosted/docker) (starts on `localhost:3000`)
3. **Ingest test data**: Use Koku's `nise` tool to generate and ingest sample cost data
4. **Run the integration**: `lago-sync bootstrap && lago-sync sync --month 2024-01 --dry-run`

---

## FAQ

**Q: Can two customers match the same cost item?**
A: Yes. If multiple customer filters match the same leaf item, events are generated for both. This results in the cost appearing on both invoices. Design your filters to be mutually exclusive unless shared billing is intentional.

**Q: What happens if Cost Management reprocesses data after I've synced?**
A: Re-run with `--force`. Lago deduplicates by `transaction_id`, so identical items won't duplicate. If values changed, the new events will have the same `transaction_id` and Lago will use the latest value.

**Q: How do I handle a customer leaving mid-month?**
A: Remove the customer from `config.yaml`. Already-pushed events for the current period will still appear on their invoice. Terminate their subscription in Lago after the final invoice is generated.

**Q: Can I sync multiple months at once?**
A: Use `--start-date` and `--end-date`:
```bash
lago-sync sync --start-date 2024-01-01 --end-date 2024-03-31 --force
```

**Q: What's the performance impact on Cost Management?**
A: Minimal. The report API is designed for dashboard use (many concurrent requests). A monthly sync for ~10 customers makes ~40 API calls total (one per provider per request). Use `filter[resolution]=daily` as configured.

**Q: Does this work with the on-prem version of Cost Management?**
A: Yes, as long as the report API is accessible. Set `base_url` to your on-prem instance URL. The data model is the same; only Trino-backed queries are skipped on-prem (transparent to the API consumer).

---

## Security Considerations

- **Never commit `config.yaml`** with real credentials. Use environment variables for secrets.
- The `x-rh-identity` header grants full read access to the org's cost data. Treat it as a secret.
- `LAGO_API_KEY` has full access to your Lago instance. Use a dedicated key for this integration.
- In production, use a secrets manager (Vault, K8s Secrets, AWS Secrets Manager) for credentials.
- The state database (`~/.lago-sync/state.db`) contains no secrets, but does reveal which customers/providers are being billed.
