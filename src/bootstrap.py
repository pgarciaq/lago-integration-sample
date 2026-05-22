"""One-time provisioning of Lago entities (billable metrics, plans, customers, subscriptions)."""

from __future__ import annotations

import logging

from lago_python_client import Client
from lago_python_client.exceptions import LagoApiError
from lago_python_client.models.billable_metric import BillableMetric
from lago_python_client.models.customer import Customer
from lago_python_client.models.plan import Plan
from lago_python_client.models.subscription import Subscription

from src.config import settings

logger = logging.getLogger(__name__)

BILLABLE_METRICS = [
    BillableMetric(
        name="AWS Daily Cost",
        code="aws_daily_cost",
        aggregation_type="sum_agg",
        field_name="cost_amount",
        recurring=False,
        description="Daily AWS cloud cost from Cost Management",
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


def bootstrap_lago(lago_api_key: str | None = None, lago_api_url: str | None = None):
    """Create all required Lago entities for the integration."""
    client = Client(
        api_key=lago_api_key or settings.lago_api_key,
        api_url=lago_api_url or settings.lago_api_url,
    )

    _create_billable_metrics(client)
    _create_plan(client)
    _create_customer(client)
    _create_subscriptions(client)

    logger.info("Bootstrap complete.")


def _create_billable_metrics(client: Client):
    for metric in BILLABLE_METRICS:
        try:
            client.billable_metrics.create(metric)
            logger.info("Created billable metric: %s", metric.code)
        except LagoApiError as e:
            if e.status_code == 422:
                logger.info("Billable metric already exists: %s", metric.code)
            else:
                raise


def _create_plan(client: Client):
    """Create a single 'Cloud Cost Pass-Through' plan with charges for all metrics."""
    plan_code = "cloud_cost_passthrough"
    try:
        client.plans.create(
            Plan(
                name="Cloud Cost Pass-Through",
                code=plan_code,
                interval="monthly",
                amount_cents=0,
                amount_currency="USD",
                pay_in_advance=False,
                description="Pass-through billing plan for cloud costs from Cost Management",
            )
        )
        logger.info("Created plan: %s", plan_code)
    except LagoApiError as e:
        if e.status_code == 422:
            logger.info("Plan already exists: %s", plan_code)
        else:
            raise


def _create_customer(client: Client):
    """Create a Lago customer for the Koku org."""
    external_id = settings.org_id
    if not external_id:
        logger.warning("No org_id configured; skipping customer creation.")
        return

    try:
        client.customers.create(
            Customer(
                external_id=external_id,
                name=f"Org {external_id}",
                currency="USD",
            )
        )
        logger.info("Created customer: %s", external_id)
    except LagoApiError as e:
        if e.status_code == 422:
            logger.info("Customer already exists: %s", external_id)
        else:
            raise


def _create_subscriptions(client: Client):
    """Create one subscription per enabled provider."""
    if not settings.org_id:
        logger.warning("No org_id configured; skipping subscription creation.")
        return

    plan_code = "cloud_cost_passthrough"
    for provider in settings.providers:
        sub_id = f"{settings.org_id}_{provider}"
        try:
            client.subscriptions.create(
                Subscription(
                    external_customer_id=settings.org_id,
                    external_id=sub_id,
                    plan_code=plan_code,
                    billing_time="calendar",
                )
            )
            logger.info("Created subscription: %s", sub_id)
        except LagoApiError as e:
            if e.status_code == 422:
                logger.info("Subscription already exists: %s", sub_id)
            else:
                raise
