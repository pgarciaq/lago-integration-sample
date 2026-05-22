"""One-time provisioning of Lago entities (billable metrics, plan, charges, customers, subscriptions)."""

from __future__ import annotations

import logging

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


def bootstrap_lago(config: AppConfig):
    """Create all required Lago entities for the integration."""
    client = Client(
        api_key=config.lago.api_key,
        api_url=config.lago.api_url,
    )

    metric_ids = _create_billable_metrics(client)
    _create_plan(client)
    _create_charges(config, client, metric_ids)
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


def _create_charges(config: AppConfig, client: Client, metric_ids: dict[str, str]):
    """Create usage-based charges linking billable metrics to the plan.

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
        charge_payload = {
            "charge": {
                "billable_metric_id": lago_id,
                "charge_model": "standard",
                "pay_in_advance": False,
                "invoiceable": True,
                "properties": {"amount": "1"},
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
                logger.info("Created charge for metric: %s", code)
            elif resp.status_code == 422:
                logger.info("Charge already exists for metric: %s", code)
            else:
                logger.warning("Failed to create charge for %s: %d %s", code, resp.status_code, resp.text)
        except httpx.HTTPError as e:
            logger.warning("HTTP error creating charge for %s: %s", code, e)


def _create_customers(config: AppConfig, client: Client):
    """Create a Lago customer for each entry in the config."""
    for customer_config in config.customers:
        try:
            client.customers.create(
                Customer(
                    external_id=customer_config.external_id,
                    name=customer_config.name,
                    currency=customer_config.currency,
                )
            )
            logger.info("Created customer: %s (%s)", customer_config.external_id, customer_config.currency)
        except LagoApiError as e:
            if e.status_code == 422:
                logger.info("Customer already exists: %s", customer_config.external_id)
            else:
                raise


def _create_subscriptions(config: AppConfig, client: Client):
    """Create one subscription per customer per provider they use."""
    for customer_config in config.customers:
        customer_providers = {r.provider for r in customer_config.resources}
        for provider in customer_providers:
            sub_id = f"{customer_config.external_id}_{provider}"
            try:
                client.subscriptions.create(
                    Subscription(
                        external_customer_id=customer_config.external_id,
                        external_id=sub_id,
                        plan_code=PLAN_CODE,
                        billing_time="calendar",
                    )
                )
                logger.info("Created subscription: %s", sub_id)
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
