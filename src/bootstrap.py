"""One-time provisioning of Lago entities (billable metrics, plan, charges, customers, subscriptions)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from lago_python_client import Client
from lago_python_client.exceptions import LagoApiError
from lago_python_client.models.billable_metric import BillableMetric
from lago_python_client.models.customer import Customer
from lago_python_client.models.plan import Plan
from lago_python_client.models.subscription import Subscription

from src.config import AppConfig

logger = logging.getLogger(__name__)

PLAN_CODE = "cloud_cost_passthrough"

BILLABLE_METRICS = [
    BillableMetric(
        name="AWS Daily Cost",
        code="aws_daily_cost",
        aggregation_type="sum_agg",
        field_name="cost_amount",
        recurring=False,
        description="Daily AWS cloud cost (amortized) from Cost Management",
    ),
    BillableMetric(
        name="Azure Daily Cost",
        code="azure_daily_cost",
        aggregation_type="sum_agg",
        field_name="cost_amount",
        recurring=False,
        description="Daily Azure cloud cost from Cost Management",
    ),
    BillableMetric(
        name="GCP Daily Cost",
        code="gcp_daily_cost",
        aggregation_type="sum_agg",
        field_name="cost_amount",
        recurring=False,
        description="Daily GCP cloud cost from Cost Management",
    ),
    BillableMetric(
        name="OCP Daily Cost",
        code="ocp_daily_cost",
        aggregation_type="sum_agg",
        field_name="cost_amount",
        recurring=False,
        description="Daily OpenShift direct cost from Cost Management",
    ),
    BillableMetric(
        name="OCP Daily Overhead",
        code="ocp_daily_overhead",
        aggregation_type="sum_agg",
        field_name="cost_amount",
        recurring=False,
        description="Daily OpenShift distributed overhead (platform, worker, storage, network)",
    ),
]


def bootstrap_lago(config: AppConfig, update: bool = False):
    """Create all required Lago entities for the integration.

    If update=True, existing charges are deleted and recreated when
    the config (e.g. invoice_group_by) has changed.
    """
    client = Client(
        api_key=config.lago.api_key,
        api_url=config.lago.api_url,
    )

    metric_ids = _create_billable_metrics(client)
    _create_plan(client)
    _create_charges(config, client, metric_ids, update=update)
    _create_customers(config, client)
    _create_subscriptions(config, client)

    logger.info("Bootstrap complete.")


def _create_billable_metrics(client: Client) -> dict[str, str]:
    """Create billable metrics and return a map of code -> lago_id."""
    metric_ids: dict[str, str] = {}
    for metric in BILLABLE_METRICS:
        try:
            result = client.billable_metrics.create(metric)
            metric_ids[metric.code] = result.lago_id
            logger.info("Created billable metric: %s", metric.code)
        except LagoApiError as e:
            if e.status_code == 422:
                logger.info("Billable metric already exists: %s", metric.code)
                # Fetch the existing metric to get its lago_id
                existing = client.billable_metrics.find(metric.code)
                metric_ids[metric.code] = existing.lago_id
            else:
                raise
    return metric_ids


def _create_plan(client: Client):
    """Create the pass-through billing plan."""
    try:
        client.plans.create(
            Plan(
                name="Cloud Cost Pass-Through",
                code=PLAN_CODE,
                interval="monthly",
                amount_cents=0,
                amount_currency="USD",
                pay_in_advance=False,
                description="Pass-through billing plan for cloud costs from Cost Management",
            )
        )
        logger.info("Created plan: %s", PLAN_CODE)
    except LagoApiError as e:
        if e.status_code == 422:
            logger.info("Plan already exists: %s", PLAN_CODE)
        else:
            raise


def _create_charges(config: AppConfig, client: Client, metric_ids: dict[str, str], update: bool = False):
    """Create usage-based charges linking billable metrics to the plan.

    If update=True and a charge already exists, delete it and recreate with
    current config (e.g., updated pricing_group_keys from invoice_group_by).

    Uses the Lago REST API directly since the Python SDK may not expose
    the charge creation endpoint.
    """
    providers_needed = config.providers_needed()
    metrics_to_attach = []

    for metric in BILLABLE_METRICS:
        # Only attach metrics for providers that are actually used
        provider_for_metric = _metric_code_to_provider(metric.code)
        if provider_for_metric and provider_for_metric not in providers_needed:
            continue
        if metric.code == "ocp_daily_overhead" and not config.sync.ocp_include_overhead:
            continue
        if metric.code in metric_ids:
            metrics_to_attach.append((metric.code, metric_ids[metric.code]))

    # Use httpx directly for charge creation (the SDK may not have this endpoint)
    base_url = config.lago.api_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {config.lago.api_key}",
        "Content-Type": "application/json",
    }

    for code, lago_id in metrics_to_attach:
        provider = _metric_code_to_provider(code)
        group_keys = config.sync.get_invoice_group_by(provider) if provider else []

        charge_properties: dict[str, Any] = {"amount": "1"}
        if group_keys:
            charge_properties["pricing_group_keys"] = group_keys

        charge_payload = {
            "charge": {
                "billable_metric_id": lago_id,
                "charge_model": "standard",
                "pay_in_advance": False,
                "invoiceable": True,
                "code": f"{code}_charge",
                "properties": charge_properties,
            }
        }
        try:
            resp = httpx.post(
                f"{base_url}/api/v1/plans/{PLAN_CODE}/charges",
                headers=headers,
                json=charge_payload,
                timeout=30.0,
            )
            if resp.status_code in (200, 201):
                group_desc = f" (grouped by: {', '.join(group_keys)})" if group_keys else ""
                logger.info("Created charge for metric: %s%s", code, group_desc)
            elif resp.status_code == 422:
                if update:
                    _update_existing_charge(base_url, headers, code, charge_payload, group_keys)
                else:
                    logger.info("Charge already exists for metric: %s (use --update to reconfigure)", code)
            else:
                logger.warning("Failed to create charge for %s: %d %s", code, resp.status_code, resp.text)
        except httpx.HTTPError as e:
            logger.warning("HTTP error creating charge for %s: %s", code, e)


def _update_existing_charge(base_url: str, headers: dict, code: str, charge_payload: dict, group_keys: list[str]):
    """Delete an existing charge and recreate it with updated config."""
    charge_code = f"{code}_charge"
    del_resp = httpx.delete(
        f"{base_url}/api/v1/plans/{PLAN_CODE}/charges/{charge_code}",
        headers=headers,
        timeout=30.0,
    )
    if del_resp.status_code in (200, 204):
        create_resp = httpx.post(
            f"{base_url}/api/v1/plans/{PLAN_CODE}/charges",
            headers=headers,
            json=charge_payload,
            timeout=30.0,
        )
        if create_resp.status_code in (200, 201):
            group_desc = f" (grouped by: {', '.join(group_keys)})" if group_keys else ""
            logger.info("Updated charge for metric: %s%s", code, group_desc)
        else:
            logger.error(
                "Deleted old charge for %s but failed to recreate: %d %s",
                code, create_resp.status_code, create_resp.text[:200],
            )
    elif del_resp.status_code == 404:
        logger.info("Charge %s not found for deletion, skipping update", charge_code)
    else:
        logger.warning(
            "Failed to delete charge %s for update: %d %s",
            charge_code, del_resp.status_code, del_resp.text[:200],
        )


def _create_customers(config: AppConfig, client: Client):
    """Create a Lago customer for each entry in the config, including tax/address data."""
    for customer_config in config.customers:
        try:
            customer_kwargs: dict[str, Any] = {
                "external_id": customer_config.external_id,
                "name": customer_config.name,
                "currency": customer_config.currency,
            }

            if customer_config.email:
                customer_kwargs["email"] = customer_config.email
            if customer_config.legal_name:
                customer_kwargs["legal_name"] = customer_config.legal_name
            if customer_config.tax_identification_number:
                customer_kwargs["tax_identification_number"] = customer_config.tax_identification_number
            if customer_config.tax_codes:
                customer_kwargs["tax_codes"] = customer_config.tax_codes

            if customer_config.address:
                addr = customer_config.address
                if addr.address_line1:
                    customer_kwargs["address_line1"] = addr.address_line1
                if addr.address_line2:
                    customer_kwargs["address_line2"] = addr.address_line2
                if addr.city:
                    customer_kwargs["city"] = addr.city
                if addr.state:
                    customer_kwargs["state"] = addr.state
                if addr.zipcode:
                    customer_kwargs["zipcode"] = addr.zipcode
                if addr.country:
                    customer_kwargs["country"] = addr.country

            client.customers.create(Customer(**customer_kwargs))
            logger.info("Created customer: %s (%s)", customer_config.external_id, customer_config.currency)
        except LagoApiError as e:
            if e.status_code == 422:
                logger.info("Customer already exists: %s", customer_config.external_id)
            else:
                raise


def _create_subscriptions(config: AppConfig, client: Client):
    """Create one subscription per customer per provider they use.

    If subscription_at is set on the customer, the subscription starts at that
    date — allowing events with timestamps after that point to be billed.
    Without this, events predating the subscription creation time are silently ignored.
    """
    for customer_config in config.customers:
        customer_providers = {r.provider for r in customer_config.resources}
        for provider in customer_providers:
            sub_id = f"{customer_config.external_id}_{provider}"
            try:
                sub_kwargs: dict[str, Any] = {
                    "external_customer_id": customer_config.external_id,
                    "external_id": sub_id,
                    "plan_code": PLAN_CODE,
                    "billing_time": "calendar",
                }
                if customer_config.subscription_at:
                    sub_kwargs["subscription_at"] = customer_config.subscription_at

                client.subscriptions.create(Subscription(**sub_kwargs))
                start_msg = f" (starts: {customer_config.subscription_at})" if customer_config.subscription_at else ""
                logger.info("Created subscription: %s%s", sub_id, start_msg)
            except LagoApiError as e:
                if e.status_code == 422:
                    logger.info("Subscription already exists: %s", sub_id)
                else:
                    raise


def _metric_code_to_provider(code: str) -> str | None:
    """Map a billable metric code back to its provider."""
    mapping = {
        "aws_daily_cost": "aws",
        "azure_daily_cost": "azure",
        "gcp_daily_cost": "gcp",
        "ocp_daily_cost": "openshift",
        "ocp_daily_overhead": "openshift",
    }
    return mapping.get(code)
