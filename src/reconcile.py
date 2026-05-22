"""Reconciliation: compare Cost Management totals with Lago usage."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from src.config import AppConfig, CustomerConfig
from src.koku_client import KokuClient

logger = logging.getLogger(__name__)


def reconcile_month(config: AppConfig, month: str) -> list[dict[str, Any]]:
    """Compare Cost Management totals vs synced event totals for a month.

    Args:
        config: Application configuration.
        month: Month to reconcile in YYYY-MM format.

    Returns:
        List of reconciliation results per customer per provider.
    """
    start = date.fromisoformat(f"{month}-01")
    # Last day of month
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    from datetime import timedelta
    end = end - timedelta(days=1)

    koku = KokuClient(config)
    results: list[dict[str, Any]] = []

    for provider in config.providers_needed():
        group_by = config.group_by_for_provider(provider)
        customers = config.customers_for_provider(provider)

        try:
            data, meta = koku.fetch_costs(provider, start, end, group_by)
        except Exception:
            logger.exception("Failed to fetch costs for %s", provider)
            continue

        # Calculate per-customer totals from the data
        customer_totals = _calculate_customer_totals(provider, data, customers)

        # Get the overall total from meta
        meta_total = _extract_meta_total(meta)

        for customer in customers:
            customer_total = customer_totals.get(customer.external_id, 0.0)
            results.append({
                "customer_id": customer.external_id,
                "provider": provider,
                "month": month,
                "cost_management_total": customer_total,
                "status": "ok" if customer_total > 0 else "no_data",
            })

        # Check for unmatched costs (costs that don't belong to any customer)
        total_matched = sum(customer_totals.values())
        unmatched = meta_total - total_matched
        if abs(unmatched) > 0.01:
            results.append({
                "customer_id": "__unmatched__",
                "provider": provider,
                "month": month,
                "cost_management_total": unmatched,
                "status": "warning",
                "message": f"${unmatched:.2f} in costs not matched to any customer",
            })

    koku.close()
    return results


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
