"""Tests for the Koku API client."""

import json
from datetime import date
from pathlib import Path

import httpx
import respx

from src.config import AppConfig, CostManagementConfig, LagoConfig
from src.koku_client import KokuClient

FIXTURES = Path(__file__).parent / "fixtures"


def _make_config(base_url="http://localhost:8000/api/cost-management/v1"):
    return AppConfig(
        lago=LagoConfig(api_key="test"),
        cost_management=CostManagementConfig(base_url=base_url, identity="", org_id="org_1"),
    )


@respx.mock
def test_fetch_aws_costs():
    """Test that the client correctly fetches and paginates AWS cost data."""
    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())

    respx.get("http://localhost:8000/api/cost-management/v1/reports/aws/costs/").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    config = _make_config()
    client = KokuClient(config)
    data, meta = client.fetch_costs("aws", date(2024, 1, 15), date(2024, 1, 16), ["account", "service"])
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

    config = _make_config()
    client = KokuClient(config)
    data, meta = client.fetch_costs("openshift", date(2024, 1, 15), date(2024, 1, 15), ["cluster", "project"])
    client.close()

    assert len(data) == 1
    assert "clusters" in data[0]


@respx.mock
def test_correct_params_sent():
    """Test that start_date, end_date, and cost_type are sent as top-level params."""
    fixture = {"data": [], "meta": {"count": 0}, "links": {}}

    route = respx.get("http://localhost:8000/api/cost-management/v1/reports/aws/costs/").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    config = _make_config()
    client = KokuClient(config)
    client.fetch_costs("aws", date(2024, 3, 1), date(2024, 3, 31), ["account", "service"])
    client.close()

    request = route.calls[0].request
    params = dict(request.url.params)
    assert params["start_date"] == "2024-03-01"
    assert params["end_date"] == "2024-03-31"
    assert params["filter[resolution]"] == "daily"
    assert params["cost_type"] == "calculated_amortized_cost"
    assert params["group_by[account]"] == "*"
    assert params["group_by[service]"] == "*"


@respx.mock
def test_tag_group_by():
    """Test that tag dimensions are passed correctly."""
    fixture = {"data": [], "meta": {"count": 0}, "links": {}}

    route = respx.get("http://localhost:8000/api/cost-management/v1/reports/aws/costs/").mock(
        return_value=httpx.Response(200, json=fixture)
    )

    config = _make_config()
    client = KokuClient(config)
    client.fetch_costs("aws", date(2024, 1, 1), date(2024, 1, 1), ["account", "tag:team"])
    client.close()

    request = route.calls[0].request
    params = dict(request.url.params)
    assert params["group_by[tag:team]"] == "*"
