"""Tests for the YAML configuration loader."""

from pathlib import Path

import pytest

from src.config import ConfigError, CustomerConfig, ResourceFilter, load_config


def _write_config(tmp_path: Path, content: str) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(content)
    return config_file


def test_load_basic_config(tmp_path):
    """Test loading a minimal config file."""
    cfg = _write_config(tmp_path, """
lago:
  api_url: "http://lago:3000"
  api_key: "test_key"

cost_management:
  base_url: "http://koku:8000/api/cost-management/v1"
  org_id: "org_99"
  identity: "dGVzdA=="

customers:
  - external_id: "cust_a"
    name: "Customer A"
    currency: "USD"
    resources:
      - provider: aws
        filter:
          account: ["111111111111"]
""")
    config = load_config(cfg)

    assert config.lago.api_url == "http://lago:3000"
    assert config.lago.api_key == "test_key"
    assert config.cost_management.org_id == "org_99"
    assert len(config.customers) == 1
    assert config.customers[0].external_id == "cust_a"
    assert config.customers[0].currency == "USD"
    assert len(config.customers[0].resources) == 1
    assert config.customers[0].resources[0].provider == "aws"


def test_env_var_interpolation(tmp_path, monkeypatch):
    """Test that ${VAR} patterns are replaced from environment."""
    monkeypatch.setenv("TEST_LAGO_KEY", "secret_abc")
    monkeypatch.setenv("TEST_KOKU_URL", "http://prod-koku:8000/api/cost-management/v1")

    cfg = _write_config(tmp_path, """
lago:
  api_key: "${TEST_LAGO_KEY}"

cost_management:
  base_url: "${TEST_KOKU_URL}"
  org_id: "org_1"
  identity: "dGVzdA=="

customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter:
          account: ["111"]
""")
    config = load_config(cfg)

    assert config.lago.api_key == "secret_abc"
    assert config.cost_management.base_url == "http://prod-koku:8000/api/cost-management/v1"


def test_env_var_default(tmp_path, monkeypatch):
    """Test that ${VAR:default} uses the default when VAR is unset."""
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)

    cfg = _write_config(tmp_path, """
lago:
  api_url: "${NONEXISTENT_VAR:http://fallback:3000}"
  api_key: "key"

cost_management:
  org_id: "org_1"
  identity: "dGVzdA=="

customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter:
          account: ["111"]
""")
    config = load_config(cfg)
    assert config.lago.api_url == "http://fallback:3000"


def test_providers_needed(tmp_path):
    """Test that providers_needed() derives from customer resources."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "k"
cost_management:
  org_id: "org_1"
  identity: "dGVzdA=="
customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter: {account: ["111"]}
      - provider: openshift
        filter: {project: ["ns-*"]}
  - external_id: "c2"
    name: "C2"
    resources:
      - provider: aws
        filter: {account: ["222"]}
""")
    config = load_config(cfg)
    assert config.providers_needed() == {"aws", "openshift"}


def test_group_by_derived_from_filters(tmp_path):
    """Test that group_by dimensions are derived from customer filter keys."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "k"
cost_management:
  org_id: "org_1"
  identity: "dGVzdA=="
customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter:
          account: ["111"]
          tag:team: ["eng"]
""")
    config = load_config(cfg)
    group_by = config.group_by_for_provider("aws")
    assert "account" in group_by
    assert "tag:team" in group_by


def test_resource_filter_glob_matching():
    """Test glob pattern matching in resource filters."""
    rf = ResourceFilter(provider="openshift", filter={"project": ["acme-*"], "cluster": ["prod-*"]})

    assert rf.matches("openshift", {"project": "acme-frontend", "cluster": "prod-01"})
    assert rf.matches("openshift", {"project": "acme-api", "cluster": "prod-cluster-01"})
    assert not rf.matches("openshift", {"project": "globex-app", "cluster": "prod-01"})
    assert not rf.matches("aws", {"project": "acme-frontend", "cluster": "prod-01"})


def test_customer_matches_leaf():
    """Test customer-level leaf matching."""
    customer = CustomerConfig(
        external_id="cust_a",
        name="A",
        resources=[
            ResourceFilter(provider="aws", filter={"account": ["111111111111"]}),
            ResourceFilter(provider="openshift", filter={"project": ["app-*"]}),
        ],
    )

    assert customer.matches_leaf("aws", {"account": "111111111111", "service": "AmazonEC2"})
    assert customer.matches_leaf("openshift", {"project": "app-backend", "cluster": "c1"})
    assert not customer.matches_leaf("aws", {"account": "999999999999"})
    assert not customer.matches_leaf("openshift", {"project": "other-ns"})


def test_config_file_not_found():
    """Test that a missing config file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")


def test_missing_api_key_raises_config_error(tmp_path):
    """Test that missing lago.api_key gives a helpful error."""
    cfg = _write_config(tmp_path, """
lago:
  api_url: "http://lago:3000"
cost_management:
  org_id: "org_1"
customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter:
          account: ["111"]
""")
    with pytest.raises(ConfigError, match="lago.api_key is required"):
        load_config(cfg)


def test_missing_org_id_raises_config_error(tmp_path):
    """Test that missing org_id gives a helpful error."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "key"
cost_management:
  base_url: "http://localhost:8000"
customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter:
          account: ["111"]
""")
    with pytest.raises(ConfigError, match="cost_management.org_id is required"):
        load_config(cfg)


def test_empty_customers_raises_config_error(tmp_path):
    """Test that an empty customers list gives a helpful error."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "key"
cost_management:
  org_id: "org_1"
  identity: "dGVzdA=="
customers: []
""")
    with pytest.raises(ConfigError, match="No customers defined"):
        load_config(cfg)


def test_invalid_provider_raises_config_error(tmp_path):
    """Test that an unsupported provider gives a helpful error."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "key"
cost_management:
  org_id: "org_1"
  identity: "dGVzdA=="
customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: oracle
        filter:
          account: ["111"]
""")
    with pytest.raises(ConfigError, match="unknown provider 'oracle'"):
        load_config(cfg)


def test_empty_filter_raises_config_error(tmp_path):
    """Test that an empty filter gives a helpful error."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "key"
cost_management:
  org_id: "org_1"
  identity: "dGVzdA=="
customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter: {}
""")
    with pytest.raises(ConfigError, match="filter.*must not be empty"):
        load_config(cfg)


def test_missing_external_id_raises_config_error(tmp_path):
    """Test that a customer without external_id gives a helpful error."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "key"
cost_management:
  org_id: "org_1"
  identity: "dGVzdA=="
customers:
  - name: "C1"
    resources:
      - provider: aws
        filter:
          account: ["111"]
""")
    with pytest.raises(ConfigError, match="external_id.*required"):
        load_config(cfg)


def test_missing_auth_raises_config_error(tmp_path):
    """Test that missing authentication gives a helpful error."""
    cfg = _write_config(tmp_path, """
lago:
  api_key: "key"
cost_management:
  org_id: "org_1"
customers:
  - external_id: "c1"
    name: "C1"
    resources:
      - provider: aws
        filter:
          account: ["111"]
""")
    with pytest.raises(ConfigError, match="authentication is not configured"):
        load_config(cfg)
