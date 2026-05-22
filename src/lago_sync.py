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
    events_skipped_zero: int = 0
    events_corrected: int = 0
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
        if self.events_skipped_zero > 0:
            logger.info(
                "Skipped %d zero-cost events for %s (no billing impact).",
                self.events_skipped_zero, self.provider,
            )
        if self.events_corrected > 0:
            logger.warning(
                "%d events for %s had cost changes detected and were corrected.",
                self.events_corrected, self.provider,
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

    def __init__(self, config: AppConfig, force_resend: bool = False):
        self.config = config
        self.client = Client(
            api_key=config.lago.api_key,
            api_url=config.lago.api_url,
        )
        self.org_id = config.cost_management.org_id
        self._force_resend = force_resend
        self._resend_suffix = f"_r{int(datetime.now(tz=timezone.utc).timestamp())}" if force_resend else ""

    def sync_provider(
        self,
        provider: str,
        data: list[dict[str, Any]],
        customers: list[CustomerConfig],
        dry_run: bool = False,
        state: Any = None,
    ) -> SyncResult:
        """Convert Koku report data into Lago events routed to appropriate customers.

        If `state` (SyncState) is provided, detects and corrects events whose
        cost values have changed since the last sync (Cost Management reprocessing).

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
            if state and not self._force_resend:
                event_records = [
                    (e.transaction_id, e.properties.get("cost_amount", "0"), e.properties)
                    for e in events
                ]
                self._correct_changed_events(events, event_records, result, state)
            self._push_events(events, result, state)

        result.log_summary()
        return result

    def _correct_changed_events(self, events: list[Event], event_records: list[tuple[str, str, dict]], result: SyncResult, state: Any):
        """Detect events whose costs changed and inject correction (delta) events.

        Instead of deprecating the old event (which risks data loss if the new push
        fails), we push an additive correction event with the delta:
            correction_amount = new_cost - old_cost

        This ensures:
        - If correction succeeds: invoice total = old + delta = new (correct)
        - If correction fails: invoice total = old (slightly wrong, but not missing)
        - Original billing data is NEVER destroyed
        """
        changed = state.find_changed_events(event_records)
        if not changed:
            return

        logger.warning(
            "Detected %d events with changed costs (Cost Management reprocessed data). "
            "Pushing correction (delta) events.",
            len(changed),
        )

        correction_ts = int(datetime.now(tz=timezone.utc).timestamp())

        for txn_id, old_cost, new_cost in changed:
            try:
                delta = float(new_cost) - float(old_cost)
            except (ValueError, TypeError):
                continue

            if abs(delta) < 0.001:
                continue

            correction_txn_id = f"{txn_id}_correction_{correction_ts}"
            original_event = next((e for e in events if e.transaction_id == txn_id), None)
            if not original_event:
                continue

            correction_event = Event(
                transaction_id=correction_txn_id,
                external_subscription_id=original_event.external_subscription_id,
                code=original_event.code,
                timestamp=original_event.timestamp,
                properties={
                    **original_event.properties,
                    "cost_amount": str(delta),
                    "_correction": "true",
                    "_original_txn_id": txn_id,
                    "_old_cost": old_cost,
                    "_new_cost": new_cost,
                },
            )
            events.append(correction_event)
            result.events_corrected += 1

            logger.info(
                "Correction event for %s: delta=%+.4f (was $%s, now $%s)",
                txn_id[:50], delta, old_cost, new_cost,
            )

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

        # Handle tag-grouped responses: keys like "tag:team" containing lists of children
        for key in list(node.keys()):
            if key.startswith("tag:") and isinstance(node[key], list):
                for child in node[key]:
                    child_dims = {**dimensions}
                    if isinstance(child, dict):
                        tag_val = child.get(key, "")
                        if tag_val:
                            child_dims[key] = str(tag_val)
                        for k, v in child.items():
                            if k.startswith("tag:") and isinstance(v, str) and k != key:
                                child_dims[k] = v
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
                self._check_currency(leaf, customer, result)
                matched = True
                events.extend(
                    self._leaf_to_events(leaf, provider, bucket_date, dimensions, customer, result)
                )

        if matched:
            result.leaves_matched += 1
        else:
            result.leaves_unmatched += 1

    def _check_currency(self, leaf: dict[str, Any], customer: CustomerConfig, result: SyncResult):
        """Validate that Cost Management's currency matches the customer's Lago currency."""
        cost = leaf.get("cost", {})
        total_field = cost.get("total", {})
        if isinstance(total_field, dict):
            source_currency = total_field.get("units", "")
            if source_currency and source_currency.upper() != customer.currency.upper():
                msg = (
                    f"Currency mismatch for customer '{customer.external_id}': "
                    f"Cost Management reports {source_currency} but customer is configured "
                    f"as {customer.currency}. Amounts will be incorrect on the invoice."
                )
                if msg not in result.errors:
                    result.errors.append(msg)
                    logger.error(msg)

    def _leaf_to_events(
        self,
        leaf: dict[str, Any],
        provider: str,
        bucket_date: str,
        dimensions: dict[str, str],
        customer: CustomerConfig,
        result: SyncResult,
    ) -> list[Event]:
        """Convert a single leaf cost object into one or two Lago Events for a customer."""
        events: list[Event] = []
        timestamp = self._date_to_unix(bucket_date)
        metric_code = PROVIDER_METRIC_CODE[provider]
        subscription_id = f"{customer.external_id}_{provider}"

        dim_key = self._stable_dim_key(dimensions)

        cost = leaf.get("cost", {})
        cost_total = self._extract_value(cost, "total")

        if abs(cost_total) < 0.001:
            result.events_skipped_zero += 1
            return events

        properties: dict[str, str] = {"cost_amount": str(cost_total)}
        properties["cost_raw"] = str(self._extract_value(cost, "raw"))
        properties["cost_markup"] = str(self._extract_value(cost, "markup"))
        properties["cost_usage"] = str(self._extract_value(cost, "usage"))
        properties.update(dimensions)

        txn_id = f"{self.org_id}_{customer.external_id}_{provider}_{dim_key}_{bucket_date}_direct{self._resend_suffix}"
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
            if distributed and abs(distributed) >= 0.001:
                overhead_props: dict[str, str] = {"cost_amount": str(distributed)}
                overhead_props["platform"] = str(self._extract_value(cost, "platform_distributed"))
                overhead_props["worker"] = str(self._extract_value(cost, "worker_unallocated_distributed"))
                overhead_props["network"] = str(self._extract_value(cost, "network_unattributed_distributed"))
                overhead_props["storage"] = str(self._extract_value(cost, "storage_unattributed_distributed"))
                overhead_props.update(dimensions)

                overhead_txn_id = (
                    f"{self.org_id}_{customer.external_id}_{provider}_{dim_key}_{bucket_date}_overhead{self._resend_suffix}"
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

    def _push_events(self, events: list[Event], result: SyncResult, state: Any = None):
        """Send events to Lago in batches, continuing on partial failures.

        Handles Lago's batch semantics: if a batch is rejected due to duplicate
        transaction_ids, those events already exist — this is treated as success
        (idempotent retry). Only non-duplicate errors are reported as failures.

        Streams cost records to state DB after each successful batch to keep
        memory usage flat for large syncs.
        """
        for i in range(0, len(events), BATCH_SIZE):
            batch = events[i : i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            batch_succeeded = False
            try:
                self._send_batch(batch)
                result.events_sent += len(batch)
                result.batches_succeeded += 1
                batch_succeeded = True
            except Exception as e:
                error_details = self._parse_batch_error(e)
                if error_details is not None:
                    dupes, real_failures = error_details
                    if real_failures == 0:
                        result.events_sent += len(batch)
                        result.batches_succeeded += 1
                        batch_succeeded = True
                        logger.debug(
                            "Batch %d: all %d events already exist (idempotent).",
                            batch_num, dupes,
                        )
                    else:
                        result.events_failed += real_failures
                        result.events_sent += dupes
                        result.batches_failed += 1
                        result.errors.append(
                            f"Batch {batch_num}: {real_failures} real failures, {dupes} duplicates (OK)"
                        )
                        logger.warning(
                            "Batch %d: %d events failed (%d were duplicates, already billed). "
                            "Continuing with next batch.",
                            batch_num, real_failures, dupes,
                        )
                else:
                    result.events_failed += len(batch)
                    result.batches_failed += 1
                    result.errors.append(f"Batch {batch_num}: {e}")
                    logger.error(
                        "Batch %d failed (%d events): %s. Continuing with next batch.",
                        batch_num, len(batch), e,
                    )

            if batch_succeeded and state:
                batch_records = [
                    (e.transaction_id, e.properties.get("cost_amount", "0"), e.properties)
                    for e in batch
                ]
                state.store_event_costs_batch(batch_records)

    @staticmethod
    def _parse_batch_error(error: Exception) -> tuple[int, int] | None:
        """Parse a Lago batch 422 error to distinguish duplicates from real failures.

        Returns (duplicate_count, real_failure_count) or None if the error
        isn't a parseable batch validation error.
        """
        error_data = None
        if hasattr(error, "response") and isinstance(error.response, dict):
            error_data = error.response
        else:
            error_str = str(error)
            if "value_already_exist" not in error_str:
                return None
            try:
                import json
                import ast
                try:
                    error_data = json.loads(error_str)
                except (json.JSONDecodeError, ValueError):
                    error_data = ast.literal_eval(error_str)
            except (ValueError, SyntaxError):
                return None

        if not error_data or not isinstance(error_data, dict):
            return None

        details = error_data.get("error_details", {})
        if not details:
            return None

        dupes = 0
        real_failures = 0
        for _idx, errs in details.items():
            if isinstance(errs, dict) and errs.get("transaction_id") == ["value_already_exist"]:
                dupes += 1
            else:
                real_failures += 1
        return (dupes, real_failures)

    @with_retry
    def _send_batch(self, batch: list[Event]):
        """Send a single batch to Lago with retry on transient errors."""
        self.client.events.batch_create(BatchEvent(events=batch))

    @staticmethod
    def _stable_dim_key(dimensions: dict[str, str]) -> str:
        """Create a stable, normalized key from dimension values.

        Strips whitespace to prevent transaction_id instability from trailing
        spaces. Case is preserved to maintain backward compatibility with
        existing events in Lago.
        """
        if not dimensions:
            return "total"
        normalized = sorted(str(v).strip() for v in dimensions.values())
        return "_".join(normalized)

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
