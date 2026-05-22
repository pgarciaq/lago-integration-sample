"""Client for the Koku Cost Management report API."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from src.config import AppConfig

logger = logging.getLogger(__name__)

PROVIDER_REPORT_PATHS = {
    "aws": "reports/aws/costs/",
    "azure": "reports/azure/costs/",
    "gcp": "reports/gcp/costs/",
    "openshift": "reports/openshift/costs/",
}

PROVIDER_COST_TYPE = {
    "aws": "calculated_amortized_cost",
}


class KokuClient:
    """Fetches cost report data from the Koku API."""

    def __init__(self, config: AppConfig):
        self.base_url = config.cost_management.base_url.rstrip("/")
        self.identity = config.cost_management.identity
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=self._build_headers(),
            timeout=60.0,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.identity:
            headers["x-rh-identity"] = self.identity
        return headers

    def fetch_costs(
        self,
        provider: str,
        start_date: date,
        end_date: date,
        group_by: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch daily cost data for a provider, handling pagination.

        Returns (data_list, meta) where data_list is the full list of
        time-bucketed data objects and meta contains totals.
        """
        path = PROVIDER_REPORT_PATHS[provider]
        params = self._build_params(provider, start_date, end_date, group_by)
        all_data: list[dict[str, Any]] = []
        meta: dict[str, Any] = {}
        offset = 0
        limit = 100

        while True:
            params["offset"] = str(offset)
            params["limit"] = str(limit)
            resp = self._client.get(path, params=params)
            resp.raise_for_status()
            body = resp.json()

            data = body.get("data", [])
            all_data.extend(data)
            meta = body.get("meta", {})

            total_count = meta.get("count", 0)
            offset += limit
            if offset >= total_count:
                break

        logger.info("Fetched %d time buckets for %s (%s to %s)", len(all_data), provider, start_date, end_date)
        return all_data, meta

    def _build_params(
        self, provider: str, start_date: date, end_date: date, group_by: list[str]
    ) -> dict[str, str]:
        params: dict[str, str] = {
            "filter[resolution]": "daily",
            "filter[start_date]": start_date.isoformat(),
            "filter[end_date]": end_date.isoformat(),
        }
        # AWS uses amortized cost
        if provider in PROVIDER_COST_TYPE:
            params["cost_type"] = PROVIDER_COST_TYPE[provider]

        for dim in group_by:
            # Tag dimensions use group_by[tag:key_name]=* syntax
            params[f"group_by[{dim}]"] = "*"
        return params

    def close(self):
        self._client.close()
