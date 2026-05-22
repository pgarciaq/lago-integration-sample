"""Tests for the Lago sync event mapping logic."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from src.config import AppConfig, CostManagementConfig, CustomerConfig, LagoConfig, ResourceFilter, SyncConfig
from src.lago_sync import LagoSync

FIXTURES = Path(__file__).parent / "fixtures"


def _make_config(customers, ocp_include_overhead=True):
    return AppConfig(
        lago=LagoConfig(api_key="test_key", api_url="http://localhost:3000"),
        cost_management=CostManagementConfig(org_id="org_12345"),
        sync=SyncConfig(ocp_include_overhead=ocp_include_overhead),
        customers=customers,
    )


def _make_sync(config):
    sync = LagoSync.__new__(LagoSync)
    sync.config = config
    sync.org_id = config.cost_management.org_id
    sync.client = MagicMock()
    return sync


def test_extract_aws_events_routed_to_customer():
    """Test that AWS cost data is routed to the correct customer."""
    customers = [
        CustomerConfig(
            external_id="cust_acme",
            name="Acme",
            resources=[ResourceFilter(provider="aws", filter={"account": ["123456789012"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    events = sync._extract_events("aws", data, customers)

    # Account 123456789012 matches: day1 has EC2+S3, day2 has EC2 = 3 events
    assert len(events) == 3

    ec2_event = events[0]
    assert ec2_event.code == "aws_daily_cost"
    assert ec2_event.external_subscription_id == "cust_acme_aws"
    assert "org_12345" in ec2_event.transaction_id
    assert "cust_acme" in ec2_event.transaction_id
    assert "direct" in ec2_event.transaction_id


def test_unmatched_costs_not_routed():
    """Test that costs for accounts not in config produce no events."""
    customers = [
        CustomerConfig(
            external_id="cust_other",
            name="Other",
            resources=[ResourceFilter(provider="aws", filter={"account": ["999999999999"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    events = sync._extract_events("aws", data, customers)
    assert len(events) == 0


def test_ocp_events_with_overhead():
    """Test that OCP data produces both direct and overhead events."""
    customers = [
        CustomerConfig(
            external_id="cust_acme",
            name="Acme",
            resources=[ResourceFilter(provider="openshift", filter={"project": ["my-app"]})],
        ),
    ]
    config = _make_config(customers, ocp_include_overhead=True)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "ocp_costs_response.json").read_text())
    data = fixture["data"]

    events = sync._extract_events("openshift", data, customers)

    # my-app matches: 1 direct + 1 overhead = 2 events
    assert len(events) == 2
    assert events[0].code == "ocp_daily_cost"
    assert events[1].code == "ocp_daily_overhead"
    assert "direct" in events[0].transaction_id
    assert "overhead" in events[1].transaction_id


def test_ocp_events_without_overhead():
    """Test that disabling overhead produces only direct events."""
    customers = [
        CustomerConfig(
            external_id="cust_acme",
            name="Acme",
            resources=[ResourceFilter(provider="openshift", filter={"project": ["my-app"]})],
        ),
    ]
    config = _make_config(customers, ocp_include_overhead=False)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "ocp_costs_response.json").read_text())
    data = fixture["data"]

    events = sync._extract_events("openshift", data, customers)
    assert len(events) == 1
    assert events[0].code == "ocp_daily_cost"


def test_glob_pattern_matching():
    """Test that glob patterns in filters work for project matching."""
    customers = [
        CustomerConfig(
            external_id="cust_acme",
            name="Acme",
            resources=[ResourceFilter(provider="openshift", filter={"project": ["m*"]})],
        ),
    ]
    config = _make_config(customers, ocp_include_overhead=True)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "ocp_costs_response.json").read_text())
    data = fixture["data"]

    events = sync._extract_events("openshift", data, customers)

    # Both "my-app" and "monitoring" match "m*" pattern
    # 2 projects x (1 direct + 1 overhead) = 4 events
    assert len(events) == 4


def test_deduplication_deterministic():
    """Test that re-extracting the same data produces identical transaction IDs."""
    customers = [
        CustomerConfig(
            external_id="cust_acme",
            name="Acme",
            resources=[ResourceFilter(provider="aws", filter={"account": ["123456789012"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    events_first = sync._extract_events("aws", data, customers)
    events_second = sync._extract_events("aws", data, customers)

    txn_ids_first = [e.transaction_id for e in events_first]
    txn_ids_second = [e.transaction_id for e in events_second]
    assert txn_ids_first == txn_ids_second


def test_multiple_customers_same_provider():
    """Test that costs can be routed to multiple customers if filters overlap."""
    customers = [
        CustomerConfig(
            external_id="cust_a",
            name="A",
            resources=[ResourceFilter(provider="aws", filter={"account": ["123456789012"]})],
        ),
        CustomerConfig(
            external_id="cust_b",
            name="B",
            # Also matches the same account (shared cost scenario)
            resources=[ResourceFilter(provider="aws", filter={"account": ["123456789012"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    events = sync._extract_events("aws", data, customers)

    # 3 leaf items x 2 customers = 6 events
    assert len(events) == 6
    cust_a_events = [e for e in events if "cust_a" in e.transaction_id]
    cust_b_events = [e for e in events if "cust_b" in e.transaction_id]
    assert len(cust_a_events) == 3
    assert len(cust_b_events) == 3
