"""Tests for the Lago sync event mapping logic."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from lago_python_client.exceptions import LagoApiError

from src.config import AppConfig, CostManagementConfig, CustomerConfig, LagoConfig, ResourceFilter, SyncConfig
from src.lago_sync import LagoSync, SyncResult

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
    sync._force_resend = False
    sync._resend_suffix = ""
    return sync


def _extract(sync, provider, data, customers):
    """Helper that calls _extract_events with a fresh SyncResult."""
    result = SyncResult(provider=provider)
    events = sync._extract_events(provider, data, customers, result)
    return events, result


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

    events, result = _extract(sync, "aws", data, customers)

    # Account 123456789012 matches: day1 has EC2+S3, day2 has EC2 = 3 events
    assert len(events) == 3
    assert result.leaves_matched == 3
    assert result.leaves_unmatched == 0

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

    events, result = _extract(sync, "aws", data, customers)
    assert len(events) == 0
    assert result.leaves_matched == 0
    assert result.leaves_unmatched == 3


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

    events, result = _extract(sync, "openshift", data, customers)

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

    events, result = _extract(sync, "openshift", data, customers)
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

    events, result = _extract(sync, "openshift", data, customers)

    # Both "my-app" and "monitoring" match "m*" pattern
    # 2 projects x (1 direct + 1 overhead) = 4 events
    assert len(events) == 4
    assert result.leaves_matched == 2


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

    events_first, _ = _extract(sync, "aws", data, customers)
    events_second, _ = _extract(sync, "aws", data, customers)

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
            resources=[ResourceFilter(provider="aws", filter={"account": ["123456789012"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    fixture = json.loads((FIXTURES / "aws_costs_response.json").read_text())
    data = fixture["data"]

    events, result = _extract(sync, "aws", data, customers)

    # 3 leaf items x 2 customers = 6 events
    assert len(events) == 6
    cust_a_events = [e for e in events if "cust_a" in e.transaction_id]
    cust_b_events = [e for e in events if "cust_b" in e.transaction_id]
    assert len(cust_a_events) == 3
    assert len(cust_b_events) == 3


def test_sync_provider_dry_run():
    """Test that dry_run=True does not call the Lago client."""
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

    result = sync.sync_provider("aws", data, customers, dry_run=True)

    assert result.events_sent == 3
    assert result.batches_succeeded == 0  # No actual batches sent
    sync.client.events.batch_create.assert_not_called()


def test_partial_batch_failure():
    """Test that a failed batch doesn't prevent subsequent batches from sending."""
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

    # Make batch_create fail on first call, succeed on subsequent
    sync.client.events.batch_create.side_effect = [Exception("API down"), None]

    # With batch size 100, all 3 events fit in one batch, so this tests 1-batch failure
    result = sync.sync_provider("aws", data, customers, dry_run=False)

    assert result.batches_failed == 1
    assert result.events_failed == 3
    assert len(result.errors) == 1


# --- Tests for new features ---


def test_parse_batch_error_all_duplicates():
    """Test that all-duplicate 422 is parsed as (N, 0) - all idempotent."""
    sync = _make_sync(_make_config([
        CustomerConfig(external_id="x", name="X", resources=[ResourceFilter(provider="aws", filter={"account": ["1"]})])
    ]))
    error = LagoApiError(
        status_code=422, url="test", detail=None,
        response={
            "status": 422, "error": "Unprocessable Entity", "code": "validation_errors",
            "error_details": {
                "0": {"transaction_id": ["value_already_exist"]},
                "1": {"transaction_id": ["value_already_exist"]},
                "2": {"transaction_id": ["value_already_exist"]},
            }
        }
    )
    result = sync._parse_batch_error(error)
    assert result == (3, 0)


def test_parse_batch_error_mixed():
    """Test mixed errors: some duplicates, some real failures."""
    sync = _make_sync(_make_config([
        CustomerConfig(external_id="x", name="X", resources=[ResourceFilter(provider="aws", filter={"account": ["1"]})])
    ]))
    error = LagoApiError(
        status_code=422, url="test", detail=None,
        response={
            "status": 422, "error": "Unprocessable Entity", "code": "validation_errors",
            "error_details": {
                "0": {"transaction_id": ["value_already_exist"]},
                "1": {"code": ["invalid_value"]},
                "2": {"transaction_id": ["value_already_exist"]},
            }
        }
    )
    result = sync._parse_batch_error(error)
    assert result == (2, 1)


def test_parse_batch_error_non_422():
    """Test that non-dedup errors return None."""
    sync = _make_sync(_make_config([
        CustomerConfig(external_id="x", name="X", resources=[ResourceFilter(provider="aws", filter={"account": ["1"]})])
    ]))
    error = Exception("Connection refused")
    result = sync._parse_batch_error(error)
    assert result is None


def test_zero_cost_events_skipped():
    """Test that events with cost_amount < 0.001 are not generated."""
    customers = [
        CustomerConfig(
            external_id="cust_a",
            name="A",
            resources=[ResourceFilter(provider="openshift", filter={"project": ["*"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    data = [
        {
            "date": "2024-01-15",
            "projects": [
                {
                    "project": "active-project",
                    "values": [{"project": "active-project", "cost": {"total": {"value": 50.0, "units": "USD"}, "raw": {"value": 50.0}, "markup": {"value": 0.0}, "usage": {"value": 0.0}}}]
                },
                {
                    "project": "no-project",
                    "values": [{"project": "no-project", "cost": {"total": {"value": 0.0, "units": "USD"}, "raw": {"value": 0.0}, "markup": {"value": 0.0}, "usage": {"value": 0.0}}}]
                },
            ],
        }
    ]
    events, result = _extract(sync, "openshift", data, customers)

    assert len(events) == 1
    assert events[0].properties["cost_amount"] == "50.0"
    assert result.events_skipped_zero == 1


def test_currency_mismatch_detected():
    """Test that currency mismatch between Koku and customer config is flagged."""
    customers = [
        CustomerConfig(
            external_id="cust_eur",
            name="Euro Customer",
            currency="EUR",
            resources=[ResourceFilter(provider="aws", filter={"account": ["123"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    data = [
        {
            "date": "2024-01-15",
            "accounts": [
                {
                    "account": "123",
                    "values": [{"account": "123", "cost": {"total": {"value": 100.0, "units": "USD"}, "raw": {"value": 100.0}, "markup": {"value": 0.0}, "usage": {"value": 0.0}}}]
                },
            ],
        }
    ]
    events, result = _extract(sync, "aws", data, customers)

    assert len(result.errors) == 1
    assert "Currency mismatch" in result.errors[0]
    assert "USD" in result.errors[0]
    assert "EUR" in result.errors[0]


def test_stable_dim_key_normalization():
    """Test that dimension key normalization strips whitespace but preserves case."""
    sync = _make_sync(_make_config([
        CustomerConfig(external_id="x", name="X", resources=[ResourceFilter(provider="aws", filter={"account": ["1"]})])
    ]))
    assert sync._stable_dim_key({"project": "Frontend"}) == "Frontend"
    assert sync._stable_dim_key({"project": "  Frontend  "}) == "Frontend"
    assert sync._stable_dim_key({"project": "frontend"}) == "frontend"
    assert sync._stable_dim_key({}) == "total"
    assert sync._stable_dim_key({"a": "X", "b": "Y"}) == "X_Y"


def test_correction_events_generated_for_changed_costs():
    """Test that delta correction events are produced when costs change."""
    customers = [
        CustomerConfig(
            external_id="cust_a",
            name="A",
            resources=[ResourceFilter(provider="aws", filter={"account": ["123"]})],
        ),
    ]
    config = _make_config(customers)
    sync = _make_sync(config)

    data = [
        {
            "date": "2024-01-15",
            "accounts": [
                {
                    "account": "123",
                    "values": [{"account": "123", "cost": {"total": {"value": 200.0, "units": "USD"}, "raw": {"value": 200.0}, "markup": {"value": 0.0}, "usage": {"value": 0.0}}}]
                },
            ],
        }
    ]

    # Mock state that has an old cost for this event
    mock_state = MagicMock()
    # Simulate: the event was previously stored with cost $150
    result = SyncResult(provider="aws")
    events = sync._extract_events("aws", data, customers, result)

    # Build event_records as _correct_changed_events now expects them
    event_records = [
        (e.transaction_id, e.properties.get("cost_amount", "0"), e.properties)
        for e in events
    ]

    # Simulate find_changed_events returning the txn_id with old=150, new=200
    txn_id = events[0].transaction_id
    mock_state.find_changed_events.return_value = [(txn_id, "150.0", "200.0")]

    sync._correct_changed_events(events, event_records, result, mock_state)

    # A correction event should have been appended
    correction_events = [e for e in events if "_correction_" in e.transaction_id]
    assert len(correction_events) == 1
    assert float(correction_events[0].properties["cost_amount"]) == 50.0  # delta: 200 - 150
    assert correction_events[0].properties["_correction"] == "true"
    assert result.events_corrected == 1
