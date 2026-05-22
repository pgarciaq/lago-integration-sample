"""Client for the Koku Cost Management report API."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

PROVIDER_REPORT_PATHS = {
    "aws": "reports/aws/costs/",
    "azure": "reports/azure/costs/",
    "gcp": "reports/gcp/costs/",
    "openshift": "reports/openshift/costs/",
}

PROVIDER_GROUP_BY_MAP = {
    "aws": lambda s: s.aws_group_by,
    "azure": lambda s: s.azure_group_by,
    "gcp": lambda s: s.gcp_group_by,
    "openshift": lambda s: s.ocp_group_by,
}


class KokuClient:
    """Fetches cost report data from the Koku API."""

    def __init__(self, base_url: str | None = None, identity: str | None = None):
        self.base_url = (base_url or settings.koku_base_url).rstrip("/")
        self.identity = identity or settings.koku_identity
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
        group_by: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch daily cost data for a provider, handling pagination.

        Returns the full list of time-bucketed data objects from the response.
        """
        path = PROVIDER_REPORT_PATHS[provider]
        if group_by is None:
            group_by = PROVIDER_GROUP_BY_MAP[provider](settings)

        params = self._build_params(start_date, end_date, group_by)
        all_data: list[dict[str, Any]] = []
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
        return all_data

    def _build_params(self, start_date: date, end_date: date, group_by: list[str]) -> dict[str, str]:
        params: dict[str, str] = {
            "filter[resolution]": "daily",
            "filter[time_scope_value]": "-10",
            "filter[time_scope_units]": "day",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        for dim in group_by:
            params[f"group_by[{dim}]"] = "*"
        return params

    def close(self):
        self._client.close()
