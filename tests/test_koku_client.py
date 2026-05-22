"""Tests for the Koku API client."""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from src.koku_client import KokuClient

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
def test_fetch_aws_costs():
    """Test that the client correctly fetches and paginates AWS cost data."""
    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())

    respx.get("http://localhost:8000/api/cost-management/v1/reports/aws/costs/").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    with patch("src.koku_client.settings") as mock_settings:
        mock_settings.koku_base_url = "http://localhost:8000/api/cost-management/v1"
        mock_settings.koku_identity = ""
        mock_settings.aws_group_by = ["account", "service"]

        client = KokuClient(
            base_url="http://localhost:8000/api/cost-management/v1",
            identity="",
        )
        data = client.fetch_costs("aws", date(2024, 1, 15), date(2024, 1, 16))
        client.close()

    assert len(data) == 2
    assert data[0]["date"] == "2024-01-15"
    assert data[1]["date"] == "2024-01-16"
    assert "accounts" in data[0]


@respx.mock
def test_fetch_ocp_costs():
    """Test fetching OCP cost data with cluster/project grouping."""
    fixture = json.loads((FIXTURES / "ocp_costs_response.json").read_text())

    respx.get("http://localhost:8000/api/cost-management/v1/reports/openshift/costs/").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    with patch("src.koku_client.settings") as mock_settings:
        mock_settings.koku_base_url = "http://localhost:8000/api/cost-management/v1"
        mock_settings.koku_identity = ""
        mock_settings.ocp_group_by = ["cluster", "project"]

        client = KokuClient(
            base_url="http://localhost:8000/api/cost-management/v1",
            identity="",
        )
        data = client.fetch_costs("openshift", date(2024, 1, 15), date(2024, 1, 15))
        client.close()

    assert len(data) == 1
    assert "clusters" in data[0]
