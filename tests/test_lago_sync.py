"""Tests for the Lago sync event mapping logic."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.lago_sync import LagoSync

FIXTURES = Path(__file__).parent / "fixtures"


@patch("src.lago_sync.settings")
def test_extract_aws_events(mock_settings):
    """Test that AWS cost data is correctly mapped to Lago events."""
    mock_settings.org_id = "org_12345"
    mock_settings.ocp_include_overhead = True
    mock_settings.lago_api_key = "test_key"
    mock_settings.lago_api_url = "http://localhost:3000"

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    sync = LagoSync.__new__(LagoSync)
    sync.org_id = "org_12345"
    sync.client = MagicMock()

    events = sync._extract_events("aws", data)

    # 2 days: day 1 has 2 services (EC2 + S3), day 2 has 1 service (EC2) = 3 direct events
    assert len(events) == 3

    ec2_event = events[0]
    assert ec2_event.code == "aws_daily_cost"
    assert ec2_event.external_subscription_id == "org_12345_aws"
    assert ec2_event.properties["cost_amount"] == "1320.55"
    assert ec2_event.properties["account"] == "123456789012"
    assert ec2_event.properties["service"] == "AmazonEC2"
    assert "direct" in ec2_event.transaction_id

    s3_event = events[1]
    assert s3_event.properties["cost_amount"] == "49.83"
    assert s3_event.properties["service"] == "AmazonS3"


@patch("src.lago_sync.settings")
def test_extract_ocp_events_with_overhead(mock_settings):
    """Test that OCP data produces both direct and overhead events."""
    mock_settings.org_id = "org_12345"
    mock_settings.ocp_include_overhead = True
    mock_settings.lago_api_key = "test_key"
    mock_settings.lago_api_url = "http://localhost:3000"

    fixture = json.loads((FIXTURES / "ocp_costs_response.json").read_text())
    data = fixture["data"]

    sync = LagoSync.__new__(LagoSync)
    sync.org_id = "org_12345"
    sync.client = MagicMock()

    events = sync._extract_events("openshift", data)

    # 2 projects, each with direct + overhead = 4 events
    assert len(events) == 4

    # First project: my-app
    direct_event = events[0]
    assert direct_event.code == "ocp_daily_cost"
    assert direct_event.properties["cost_amount"] == "950.0"
    assert direct_event.properties["project"] == "my-app"
    assert direct_event.properties["cluster"] == "prod-cluster-abc"
    assert "direct" in direct_event.transaction_id

    overhead_event = events[1]
    assert overhead_event.code == "ocp_daily_overhead"
    assert overhead_event.properties["cost_amount"] == "115.0"
    assert overhead_event.properties["platform"] == "75.0"
    assert overhead_event.properties["worker"] == "25.0"
    assert "overhead" in overhead_event.transaction_id

    # Second project: monitoring
    monitoring_direct = events[2]
    assert monitoring_direct.properties["project"] == "monitoring"
    assert monitoring_direct.properties["cost_amount"] == "180.0"

    monitoring_overhead = events[3]
    assert monitoring_overhead.properties["cost_amount"] == "30.0"


@patch("src.lago_sync.settings")
def test_extract_ocp_events_without_overhead(mock_settings):
    """Test that disabling overhead skips the overhead events."""
    mock_settings.org_id = "org_12345"
    mock_settings.ocp_include_overhead = False
    mock_settings.lago_api_key = "test_key"
    mock_settings.lago_api_url = "http://localhost:3000"

    fixture = json.loads((FIXTURES / "ocp_costs_response.json").read_text())
    data = fixture["data"]

    sync = LagoSync.__new__(LagoSync)
    sync.org_id = "org_12345"
    sync.client = MagicMock()

    events = sync._extract_events("openshift", data)

    # 2 projects, direct only = 2 events
    assert len(events) == 2
    assert all(e.code == "ocp_daily_cost" for e in events)


@patch("src.lago_sync.settings")
def test_deduplication_via_transaction_id(mock_settings):
    """Test that re-extracting the same data produces identical transaction IDs."""
    mock_settings.org_id = "org_12345"
    mock_settings.ocp_include_overhead = False
    mock_settings.lago_api_key = "test_key"
    mock_settings.lago_api_url = "http://localhost:3000"

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    sync = LagoSync.__new__(LagoSync)
    sync.org_id = "org_12345"
    sync.client = MagicMock()

    events_first = sync._extract_events("aws", data)
    events_second = sync._extract_events("aws", data)

    txn_ids_first = [e.transaction_id for e in events_first]
    txn_ids_second = [e.transaction_id for e in events_second]
    assert txn_ids_first == txn_ids_second
