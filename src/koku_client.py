"""Client for the Koku Cost Management report API."""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import httpx

from src.config import AppConfig
from src.retry import with_retry

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
        self._cm_config = config.cost_management
        self._token: str | None = None
        self._token_expires_at: float = 0
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Accept": "application/json"},
            timeout=60.0,
        )

    def _get_auth_headers(self) -> dict[str, str]:
        """Get authentication headers, refreshing OAuth2 token if needed."""
        if self._cm_config.client_id and self._cm_config.client_secret:
            return {"Authorization": f"Bearer {self._get_oauth_token()}"}
        elif self._cm_config.identity:
            return {"x-rh-identity": self._cm_config.identity}
        return {}

    def _get_oauth_token(self) -> str:
        """Get a valid OAuth2 token, refreshing if expired."""
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        logger.debug("Refreshing OAuth2 token from %s", self._cm_config.token_url)
        resp = httpx.post(
            self._cm_config.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._cm_config.client_id,
                "client_secret": self._cm_config.client_secret,
                "scope": "api.console",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        token_data = resp.json()
        self._token = token_data["access_token"]
        self._token_expires_at = time.time() + token_data.get("expires_in", 900)
        logger.info("OAuth2 token acquired (expires in %ds)", token_data.get("expires_in", 900))
        return self._token

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

            body = self._fetch_page(path, params)

            data = body.get("data", [])
            all_data.extend(data)
            meta = body.get("meta", {})

            total_count = meta.get("count", 0)
            offset += limit
            if offset >= total_count:
                break

        logger.info("Fetched %d time buckets for %s (%s to %s)", len(all_data), provider, start_date, end_date)
        return all_data, meta

    @with_retry
    def _fetch_page(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Fetch a single page from the API, with retry on transient errors."""
        self._client.headers.update(self._get_auth_headers())
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _build_params(
        self, provider: str, start_date: date, end_date: date, group_by: list[str]
    ) -> dict[str, str]:
        params: dict[str, str] = {
            "filter[resolution]": "daily",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if provider in PROVIDER_COST_TYPE:
            params["cost_type"] = PROVIDER_COST_TYPE[provider]

        for dim in group_by:
            params[f"group_by[{dim}]"] = "*"
        return params

    def close(self):
        self._client.close()
