"""Maps Koku report data to Lago usage events, routing to the correct customer."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from lago_python_client import Client
from lago_python_client.models.event import BatchEvent, Event

from src.config import AppConfig, CustomerConfig
from src.retry import with_retry

logger = logging.getLogger(__name__)

BATCH_SIZE = 100

PROVIDER_METRIC_CODE = {
    "aws": "aws_daily_cost",
    "azure": "azure_daily_cost",
    "gcp": "gcp_daily_cost",
    "openshift": "ocp_daily_cost",
}

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


@dataclass
class SyncResult:
    """Tracks sync statistics for observability."""

    provider: str
    events_sent: int = 0
    events_failed: int = 0
    leaves_matched: int = 0
    leaves_unmatched: int = 0
    batches_succeeded: int = 0
    batches_failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_leaves(self) -> int:
        return self.leaves_matched + self.leaves_unmatched

    def log_summary(self):
        logger.info(
            "Sync result for %s: %d events sent, %d failed | "
            "%d/%d leaves matched | %d/%d batches succeeded",
            self.provider,
            self.events_sent,
            self.events_failed,
            self.leaves_matched,
            self.total_leaves,
            self.batches_succeeded,
            self.batches_succeeded + self.batches_failed,
        )
        if self.leaves_unmatched > 0:
            logger.warning(
                "%d cost items for %s did not match any customer filter. "
                "These costs will NOT appear on any invoice. "
                "Check your config.yaml resource filters.",
                self.leaves_unmatched,
                self.provider,
            )
        for error in self.errors:
            logger.error("  Batch error: %s", error)


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
        self,
        provider: str,
        data: list[dict[str, Any]],
        customers: list[CustomerConfig],
        dry_run: bool = False,
    ) -> SyncResult:
        """Convert Koku report data into Lago events routed to appropriate customers.

        Returns a SyncResult with detailed statistics.
        """
        result = SyncResult(provider=provider)
        events = self._extract_events(provider, data, customers, result)

        if dry_run:
            result.events_sent = len(events)
            logger.info("[DRY RUN] Would send %d events for %s", len(events), provider)
            for event in events[:5]:
                logger.info(
                    "  [DRY RUN] %s -> %s | %s | cost_amount=%s",
                    event.code,
                    event.external_subscription_id,
                    event.transaction_id[:60],
                    event.properties.get("cost_amount", "?"),
                )
            if len(events) > 5:
                logger.info("  [DRY RUN] ... and %d more events", len(events) - 5)
        else:
            self._push_events(events, result)

        result.log_summary()
        return result

    def _extract_events(
        self,
        provider: str,
        data: list[dict[str, Any]],
        customers: list[CustomerConfig],
        result: SyncResult,
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
                result=result,
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
        result: SyncResult,
    ):
        """Recursively descend the Koku nested response until we reach 'values' arrays."""
        if "values" in node:
            for leaf in node["values"]:
                leaf_dims = {**dimensions}
                for singular in DIMENSION_PLURAL_KEYS:
                    if singular in leaf and singular not in leaf_dims:
                        val = leaf[singular]
                        if isinstance(val, str):
                            leaf_dims[singular] = val
                for key, val in leaf.items():
                    if key.startswith("tag:") and isinstance(val, str):
                        leaf_dims[key] = val

                self._route_leaf(leaf, provider, bucket_date, leaf_dims, customers, events, result)
            return

        for singular, plural in DIMENSION_PLURAL_KEYS.items():
            if plural in node:
                for child in node[plural]:
                    child_dims = {**dimensions}
                    if singular in child:
                        child_dims[singular] = str(child[singular])
                    for key, val in child.items():
                        if key.startswith("tag:") and isinstance(val, str):
                            child_dims[key] = val
                    self._walk_tree(child, provider, bucket_date, child_dims, customers, events, result)
                return

    def _route_leaf(
        self,
        leaf: dict[str, Any],
        provider: str,
        bucket_date: str,
        dimensions: dict[str, str],
        customers: list[CustomerConfig],
        events: list[Event],
        result: SyncResult,
    ):
        """Match a leaf to customers and generate events for each match."""
        matched = False
        for customer in customers:
            if customer.matches_leaf(provider, dimensions):
                matched = True
                events.extend(
                    self._leaf_to_events(leaf, provider, bucket_date, dimensions, customer)
                )

        if matched:
            result.leaves_matched += 1
        else:
            result.leaves_unmatched += 1

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

        dim_key = "_".join(str(v) for v in sorted(dimensions.values())) if dimensions else "total"

        cost = leaf.get("cost", {})
        cost_total = self._extract_value(cost, "total")

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

    def _push_events(self, events: list[Event], result: SyncResult):
        """Send events to Lago in batches, continuing on partial failures."""
        for i in range(0, len(events), BATCH_SIZE):
            batch = events[i : i + BATCH_SIZE]
            try:
                self._send_batch(batch)
                result.events_sent += len(batch)
                result.batches_succeeded += 1
            except Exception as e:
                result.events_failed += len(batch)
                result.batches_failed += 1
                result.errors.append(f"Batch {i // BATCH_SIZE + 1}: {e}")
                logger.error(
                    "Batch %d failed (%d events lost): %s. Continuing with next batch.",
                    i // BATCH_SIZE + 1,
                    len(batch),
                    e,
                )

    @with_retry
    def _send_batch(self, batch: list[Event]):
        """Send a single batch to Lago with retry on transient errors."""
        self.client.events.batch_create(BatchEvent(events=batch))

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
