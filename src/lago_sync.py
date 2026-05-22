"""Maps Koku report data to Lago usage events, routing to the correct customer."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from lago_python_client import Client
from lago_python_client.models.event import BatchEvent, Event

from src.config import AppConfig, CustomerConfig

logger = logging.getLogger(__name__)

BATCH_SIZE = 100

PROVIDER_METRIC_CODE = {
    "aws": "aws_daily_cost",
    "azure": "azure_daily_cost",
    "gcp": "gcp_daily_cost",
    "openshift": "ocp_daily_cost",
}

# Plural key names used by Koku's nested response format
DIMENSION_PLURAL_KEYS = {
    "account": "accounts",
    "service": "services",
    "region": "regions",
    "subscription_guid": "subscription_guids",
    "service_name": "service_names",
    "resource_location": "resource_locations",
    "cluster": "clusters",
    "project": "projects",
    "node": "nodes",
    "az": "azs",
    "product_family": "product_families",
    "vm_name": "vm_names",
}


class LagoSync:
    """Transforms Koku cost data into Lago events routed per customer."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.client = Client(
            api_key=config.lago.api_key,
            api_url=config.lago.api_url,
        )
        self.org_id = config.cost_management.org_id

    def sync_provider(
        self, provider: str, data: list[dict[str, Any]], customers: list[CustomerConfig]
    ) -> int:
        """Convert Koku report data into Lago events routed to appropriate customers.

        Returns the total number of events sent.
        """
        events = self._extract_events(provider, data, customers)
        self._push_events(events)
        logger.info("Synced %d events for provider %s", len(events), provider)
        return len(events)

    def _extract_events(
        self, provider: str, data: list[dict[str, Any]], customers: list[CustomerConfig]
    ) -> list[Event]:
        """Walk the nested Koku response tree and produce events per leaf per customer."""
        events: list[Event] = []

        for time_bucket in data:
            bucket_date = time_bucket.get("date", "")
            self._walk_tree(
                node=time_bucket,
                provider=provider,
                bucket_date=bucket_date,
                dimensions={},
                customers=customers,
                events=events,
            )
        return events

    def _walk_tree(
        self,
        node: dict[str, Any],
        provider: str,
        bucket_date: str,
        dimensions: dict[str, str],
        customers: list[CustomerConfig],
        events: list[Event],
    ):
        """Recursively descend the Koku nested response until we reach 'values' arrays."""
        if "values" in node:
            for leaf in node["values"]:
                leaf_dims = {**dimensions}
                # Capture any additional dimension values from the leaf itself
                for singular in DIMENSION_PLURAL_KEYS:
                    if singular in leaf and singular not in leaf_dims:
                        val = leaf[singular]
                        if isinstance(val, str):
                            leaf_dims[singular] = val
                # Also capture tag values from leaf (tag:key format)
                for key, val in leaf.items():
                    if key.startswith("tag:") and isinstance(val, str):
                        leaf_dims[key] = val

                self._route_leaf(leaf, provider, bucket_date, leaf_dims, customers, events)
            return

        # Look for plural dimension keys to descend into
        for singular, plural in DIMENSION_PLURAL_KEYS.items():
            if plural in node:
                for child in node[plural]:
                    child_dims = {**dimensions}
                    if singular in child:
                        child_dims[singular] = str(child[singular])
                    # Also capture tag values at this nesting level
                    for key, val in child.items():
                        if key.startswith("tag:") and isinstance(val, str):
                            child_dims[key] = val
                    self._walk_tree(child, provider, bucket_date, child_dims, customers, events)
                return

    def _route_leaf(
        self,
        leaf: dict[str, Any],
        provider: str,
        bucket_date: str,
        dimensions: dict[str, str],
        customers: list[CustomerConfig],
        events: list[Event],
    ):
        """Match a leaf to customers and generate events for each match."""
        for customer in customers:
            if customer.matches_leaf(provider, dimensions):
                events.extend(
                    self._leaf_to_events(leaf, provider, bucket_date, dimensions, customer)
                )

    def _leaf_to_events(
        self,
        leaf: dict[str, Any],
        provider: str,
        bucket_date: str,
        dimensions: dict[str, str],
        customer: CustomerConfig,
    ) -> list[Event]:
        """Convert a single leaf cost object into one or two Lago Events for a customer."""
        events: list[Event] = []
        timestamp = self._date_to_unix(bucket_date)
        metric_code = PROVIDER_METRIC_CODE[provider]
        subscription_id = f"{customer.external_id}_{provider}"

        # Build dimension string for deduplication
        dim_key = "_".join(str(v) for v in sorted(dimensions.values())) if dimensions else "total"

        # Extract cost values
        cost = leaf.get("cost", {})
        cost_total = self._extract_value(cost, "total")

        # Direct cost event
        properties: dict[str, str] = {"cost_amount": str(cost_total)}
        properties["cost_raw"] = str(self._extract_value(cost, "raw"))
        properties["cost_markup"] = str(self._extract_value(cost, "markup"))
        properties["cost_usage"] = str(self._extract_value(cost, "usage"))
        properties.update(dimensions)

        txn_id = f"{self.org_id}_{customer.external_id}_{provider}_{dim_key}_{bucket_date}_direct"
        events.append(
            Event(
                transaction_id=txn_id,
                external_subscription_id=subscription_id,
                code=metric_code,
                timestamp=timestamp,
                properties=properties,
            )
        )

        # OCP distributed overhead as separate event
        if provider == "openshift" and self.config.sync.ocp_include_overhead:
            distributed = self._extract_value(cost, "distributed")
            if distributed and distributed > 0:
                overhead_props: dict[str, str] = {"cost_amount": str(distributed)}
                overhead_props["platform"] = str(self._extract_value(cost, "platform_distributed"))
                overhead_props["worker"] = str(self._extract_value(cost, "worker_unallocated_distributed"))
                overhead_props["network"] = str(self._extract_value(cost, "network_unattributed_distributed"))
                overhead_props["storage"] = str(self._extract_value(cost, "storage_unattributed_distributed"))
                overhead_props.update(dimensions)

                overhead_txn_id = (
                    f"{self.org_id}_{customer.external_id}_{provider}_{dim_key}_{bucket_date}_overhead"
                )
                events.append(
                    Event(
                        transaction_id=overhead_txn_id,
                        external_subscription_id=subscription_id,
                        code="ocp_daily_overhead",
                        timestamp=timestamp,
                        properties=overhead_props,
                    )
                )

        return events

    def _push_events(self, events: list[Event]):
        """Send events to Lago in batches of BATCH_SIZE."""
        for i in range(0, len(events), BATCH_SIZE):
            batch = events[i : i + BATCH_SIZE]
            self.client.events.batch_create(BatchEvent(events=batch))
            logger.debug("Pushed batch of %d events (offset %d)", len(batch), i)

    @staticmethod
    def _extract_value(cost_obj: dict[str, Any], key: str) -> float:
        """Extract a numeric value from a nested cost field like cost.total.value."""
        field = cost_obj.get(key, {})
        if isinstance(field, dict):
            return float(field.get("value", 0) or 0)
        return 0.0

    @staticmethod
    def _date_to_unix(date_str: str) -> int:
        """Convert YYYY-MM-DD or YYYY-MM to Unix timestamp."""
        try:
            if len(date_str) == 10:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            else:
                dt = datetime.strptime(date_str, "%Y-%m")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            return int(datetime.now(tz=timezone.utc).timestamp())
