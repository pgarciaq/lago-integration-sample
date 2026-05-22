"""Tests for the reconciliation logic."""

import json
from pathlib import Path

from src.config import CustomerConfig, ResourceFilter
from src.reconcile import _calculate_customer_totals, _extract_meta_total

FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_meta_total():
    """Test extracting the total from a meta object."""
    meta = {
        "total": {
            "cost": {
                "raw": {"value": 2500.00, "units": "USD"},
                "total": {"value": 2750.00, "units": "USD"},
            }
        }
    }
    assert _extract_meta_total(meta) == 2750.00


def test_extract_meta_total_empty():
    """Test extracting total from empty meta."""
    assert _extract_meta_total({}) == 0.0
    assert _extract_meta_total({"total": {}}) == 0.0


def test_calculate_customer_totals_aws():
    """Test per-customer total calculation from AWS data."""
    customers = [
        CustomerConfig(
            external_id="cust_acme",
            name="Acme",
            resources=[ResourceFilter(provider="aws", filter={"account": ["123456789012"]})],
        ),
        CustomerConfig(
            external_id="cust_other",
            name="Other",
            resources=[ResourceFilter(provider="aws", filter={"account": ["999999999999"]})],
        ),
    ]

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    totals = _calculate_customer_totals("aws", data, customers)

    # cust_acme should have: 1320.55 (EC2 day1) + 49.83 (S3 day1) + 1210.0 (EC2 day2) = 2580.38
    assert abs(totals["cust_acme"] - 2580.38) < 0.01
    assert totals["cust_other"] == 0.0


def test_calculate_customer_totals_ocp():
    """Test per-customer total calculation from OCP data."""
    customers = [
        CustomerConfig(
            external_id="cust_acme",
            name="Acme",
            resources=[ResourceFilter(provider="openshift", filter={"project": ["my-app"]})],
        ),
    ]

    fixture = json.loads((FIXTURES / "ocp_costs_response.json").read_text())
    data = fixture["data"]

    totals = _calculate_customer_totals("openshift", data, customers, include_overhead=True)

    # my-app cost.total.value (950.0) + cost.distributed.value (115.0) = 1065.0
    assert abs(totals["cust_acme"] - 1065.0) < 0.01
