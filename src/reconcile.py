"""Reconciliation: compare Cost Management totals with Lago usage."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from lago_python_client import Client
from lago_python_client.exceptions import LagoApiError

from src.config import AppConfig, CustomerConfig
from src.koku_client import KokuClient

logger = logging.getLogger(__name__)


def reconcile_month(config: AppConfig, month: str) -> list[dict[str, Any]]:
    """Compare Cost Management totals vs Lago past usage for a month.

    Args:
        config: Application configuration.
        month: Month to reconcile in YYYY-MM format.

    Returns:
        List of reconciliation results per customer per provider.
    """
    start = date.fromisoformat(f"{month}-01")
    if start.month == 12:
        end = date(start.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)

    koku = KokuClient(config)
    lago_client = Client(api_key=config.lago.api_key, api_url=config.lago.api_url)
    results: list[dict[str, Any]] = []

    for provider in config.providers_needed():
        group_by = config.group_by_for_provider(provider)
        customers = config.customers_for_provider(provider)

        try:
            data, meta = koku.fetch_costs(provider, start, end, group_by)
        except Exception:
            logger.exception("Failed to fetch costs for %s", provider)
            continue

        # Calculate per-customer totals from Cost Management data
        cm_customer_totals = _calculate_customer_totals(provider, data, customers)

        # Get Lago usage per customer for comparison
        lago_customer_totals = _get_lago_usage(lago_client, customers, provider, month)

        meta_total = _extract_meta_total(meta)

        for customer in customers:
            cm_total = cm_customer_totals.get(customer.external_id, 0.0)
            lago_total = lago_customer_totals.get(customer.external_id)
            delta = None

            status = "ok"
            message = None

            if cm_total == 0:
                status = "no_data"
                message = "No cost data in Cost Management for this period"
            elif lago_total is None:
                status = "lago_unavailable"
                message = "Could not retrieve Lago usage (check API key/connectivity)"
            elif abs(cm_total - lago_total) > 0.01:
                status = "mismatch"
                delta = lago_total - cm_total
                message = f"Delta: ${delta:+.2f} (Lago has {'more' if delta > 0 else 'less'} than Cost Management)"

            result_entry = {
                "customer_id": customer.external_id,
                "provider": provider,
                "month": month,
                "cost_management_total": cm_total,
                "lago_total": lago_total,
                "status": status,
            }
            if delta is not None:
                result_entry["delta"] = delta
            if message:
                result_entry["message"] = message
            results.append(result_entry)

        # Check for unmatched costs
        total_matched = sum(cm_customer_totals.values())
        unmatched = meta_total - total_matched
        if abs(unmatched) > 0.01:
            results.append({
                "customer_id": "__unmatched__",
                "provider": provider,
                "month": month,
                "cost_management_total": unmatched,
                "lago_total": None,
                "status": "warning",
                "message": f"${unmatched:.2f} in costs not matched to any customer filter",
            })

    koku.close()
    return results


def _get_lago_usage(
    client: Client, customers: list[CustomerConfig], provider: str, month: str
) -> dict[str, float | None]:
    """Query Lago past_usage for each customer's subscription for the given month.

    Returns customer_id -> total_amount (or None if query failed).
    """
    totals: dict[str, float | None] = {}

    for customer in customers:
        subscription_id = f"{customer.external_id}_{provider}"
        try:
            usage = client.customers.past_usage(
                customer.external_id,
                external_subscription_id=subscription_id,
                options={"page": 1, "per_page": 12},
            )
            # past_usage returns usage periods; find the one matching our month
            amount = _find_period_amount(usage, month)
            totals[customer.external_id] = amount
        except LagoApiError as e:
            if e.status_code == 404:
                # Customer or subscription doesn't exist in Lago yet
                totals[customer.external_id] = 0.0
            else:
                logger.warning(
                    "Failed to query Lago usage for %s/%s: %s",
                    customer.external_id, subscription_id, e,
                )
                totals[customer.external_id] = None
        except Exception as e:
            logger.warning("Error querying Lago for %s: %s", customer.external_id, e)
            totals[customer.external_id] = None

    return totals


def _find_period_amount(usage_response: Any, month: str) -> float:
    """Extract the total amount for a given month from Lago's past_usage response."""
    # The response structure varies by SDK version; handle both list and object forms
    periods = []
    if hasattr(usage_response, "usage_periods"):
        periods = usage_response.usage_periods or []
    elif isinstance(usage_response, dict):
        periods = usage_response.get("usage_periods", [])
    elif isinstance(usage_response, list):
        periods = usage_response

    for period in periods:
        # Match by checking if from_datetime starts with our month
        from_dt = None
        if hasattr(period, "from_datetime"):
            from_dt = period.from_datetime
        elif isinstance(period, dict):
            from_dt = period.get("from_datetime", "")

        if from_dt and str(from_dt).startswith(month):
            if hasattr(period, "total_amount_cents"):
                return float(period.total_amount_cents or 0) / 100.0
            elif isinstance(period, dict):
                return float(period.get("total_amount_cents", 0)) / 100.0

    return 0.0


def _calculate_customer_totals(
    provider: str, data: list[dict[str, Any]], customers: list[CustomerConfig]
) -> dict[str, float]:
    """Walk the response tree and accumulate cost totals per customer."""
    from src.lago_sync import DIMENSION_PLURAL_KEYS, LagoSync

    totals: dict[str, float] = {c.external_id: 0.0 for c in customers}

    def _walk(node: dict, dims: dict):
        if "values" in node:
            for leaf in node["values"]:
                leaf_dims = {**dims}
                for singular in DIMENSION_PLURAL_KEYS:
                    if singular in leaf and singular not in leaf_dims:
                        val = leaf[singular]
                        if isinstance(val, str):
                            leaf_dims[singular] = val
                for key, val in leaf.items():
                    if key.startswith("tag:") and isinstance(val, str):
                        leaf_dims[key] = val

                cost = leaf.get("cost", {})
                cost_total = LagoSync._extract_value(cost, "total")

                for customer in customers:
                    if customer.matches_leaf(provider, leaf_dims):
                        totals[customer.external_id] += cost_total
            return

        for singular, plural in DIMENSION_PLURAL_KEYS.items():
            if plural in node:
                for child in node[plural]:
                    child_dims = {**dims}
                    if singular in child:
                        child_dims[singular] = str(child[singular])
                    for key, val in child.items():
                        if key.startswith("tag:") and isinstance(val, str):
                            child_dims[key] = val
                    _walk(child, child_dims)
                return

    for time_bucket in data:
        _walk(time_bucket, {})

    return totals


def _extract_meta_total(meta: dict[str, Any]) -> float:
    """Extract the total cost from the meta.total object."""
    total = meta.get("total", {})
    cost = total.get("cost", {})
    total_field = cost.get("total", {})
    if isinstance(total_field, dict):
        return float(total_field.get("value", 0) or 0)
    return 0.0
