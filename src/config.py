"""Configuration loader: YAML mapping file + environment variable interpolation."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_VAR_RE = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def _interpolate_env(value: str) -> str:
    """Replace ${VAR} or ${VAR:default} patterns with environment variable values."""
    def _replace(match):
        var_name = match.group(1)
        default = match.group(2)
        return os.environ.get(var_name, default if default is not None else "")
    return _ENV_VAR_RE.sub(_replace, value)


def _interpolate_recursive(obj: Any) -> Any:
    if isinstance(obj, str):
        return _interpolate_env(obj)
    if isinstance(obj, dict):
        return {k: _interpolate_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_recursive(item) for item in obj]
    return obj


@dataclass
class ResourceFilter:
    """Defines which Cost Management resources belong to a customer."""

    provider: str
    filter: dict[str, list[str]] = field(default_factory=dict)

    def matches(self, provider: str, dimensions: dict[str, str]) -> bool:
        """Check if a cost data leaf with given dimensions matches this filter."""
        if self.provider != provider:
            return False
        for key, patterns in self.filter.items():
            dim_value = dimensions.get(key, "")
            if not any(fnmatch.fnmatch(dim_value, pat) for pat in patterns):
                return False
        return True


@dataclass
class CustomerAddress:
    """Billing address for tax calculation."""

    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    zipcode: str = ""
    country: str = ""  # ISO 3166 alpha-2 (e.g., "US", "DE", "FR")


@dataclass
class CustomerConfig:
    """A billable customer and their associated resources."""

    external_id: str
    name: str
    currency: str = "USD"
    resources: list[ResourceFilter] = field(default_factory=list)
    # Tax-related fields
    email: str = ""
    legal_name: str = ""
    tax_identification_number: str = ""
    tax_codes: list[str] = field(default_factory=list)
    address: CustomerAddress | None = None

    def matches_leaf(self, provider: str, dimensions: dict[str, str]) -> bool:
        """Return True if any resource filter matches the given dimensions."""
        return any(r.matches(provider, dimensions) for r in self.resources)


@dataclass
class CostManagementConfig:
    base_url: str = "http://localhost:8000/api/cost-management/v1"
    identity: str = ""
    org_id: str = ""


@dataclass
class LagoConfig:
    api_url: str = "http://localhost:3000"
    api_key: str = ""


@dataclass
class SyncConfig:
    ocp_include_overhead: bool = True
    invoice_group_by: dict[str, list[str]] = field(default_factory=dict)

    def get_invoice_group_by(self, provider: str) -> list[str]:
        """Get the invoice grouping dimensions for a provider.

        These become Lago pricing_group_keys on the charge, producing
        one invoice line item per unique combination of these dimension values.
        """
        if provider in self.invoice_group_by:
            return self.invoice_group_by[provider]
        return _default_invoice_group_by(provider)


def _default_invoice_group_by(provider: str) -> list[str]:
    """Default dimensions for invoice itemization per provider."""
    defaults = {
        "aws": ["account", "service"],
        "azure": ["subscription_guid", "service_name"],
        "gcp": ["account", "service"],
        "openshift": ["project", "cluster"],
    }
    return defaults.get(provider, [])


@dataclass
class AppConfig:
    """Top-level application configuration."""

    lago: LagoConfig = field(default_factory=LagoConfig)
    cost_management: CostManagementConfig = field(default_factory=CostManagementConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    customers: list[CustomerConfig] = field(default_factory=list)

    def providers_needed(self) -> set[str]:
        """Derive the set of providers needed from customer resource definitions."""
        providers = set()
        for customer in self.customers:
            for resource in customer.resources:
                providers.add(resource.provider)
        return providers

    def group_by_for_provider(self, provider: str) -> list[str]:
        """Derive group_by dimensions needed for a provider from all customer filters."""
        dimensions: set[str] = set()
        for customer in self.customers:
            for resource in customer.resources:
                if resource.provider == provider:
                    dimensions.update(resource.filter.keys())
        return sorted(dimensions) if dimensions else _default_group_by(provider)

    def customers_for_provider(self, provider: str) -> list[CustomerConfig]:
        """Return customers that have at least one resource filter for this provider."""
        return [c for c in self.customers if any(r.provider == provider for r in c.resources)]


def _default_group_by(provider: str) -> list[str]:
    defaults = {
        "aws": ["account", "service", "region"],
        "azure": ["subscription_guid", "service_name", "resource_location"],
        "gcp": ["account", "service", "region"],
        "openshift": ["cluster", "project"],
    }
    return defaults.get(provider, [])


class ConfigError(Exception):
    """Raised when configuration is invalid."""


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from a YAML file with env var interpolation.

    Falls back to LAGO_SYNC_CONFIG env var, then ./config.yaml.
    Raises ConfigError with a helpful message if the config is malformed.
    """
    if config_path is None:
        config_path = os.environ.get("LAGO_SYNC_CONFIG", "config.yaml")

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {path}\n"
            f"  Create one from the template: cp config.example.yaml config.yaml"
        )

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration file {path} must be a YAML mapping (dict), got {type(raw).__name__}")

    data = _interpolate_recursive(raw)

    lago_data = data.get("lago", {})
    cm_data = data.get("cost_management", {})
    sync_data = data.get("sync", {})
    customers_data = data.get("customers", [])

    # Validate required fields
    if not lago_data.get("api_key"):
        raise ConfigError(
            "lago.api_key is required.\n"
            "  Set it in config.yaml or via the LAGO_API_KEY environment variable.\n"
            "  Find your API key in the Lago UI: Settings → Developers → API Keys."
        )

    if not cm_data.get("org_id"):
        raise ConfigError(
            "cost_management.org_id is required.\n"
            "  This is your organization identifier in Cost Management.\n"
            "  It's used to scope deduplication keys and state tracking."
        )

    if not customers_data:
        raise ConfigError(
            "No customers defined in config.yaml.\n"
            "  At least one customer with resource filters is required.\n"
            "  See config.example.yaml for the expected format."
        )

    # Parse customers
    customers = []
    for i, cust in enumerate(customers_data):
        if not isinstance(cust, dict):
            raise ConfigError(f"customers[{i}]: expected a mapping, got {type(cust).__name__}")

        if "external_id" not in cust:
            raise ConfigError(
                f"customers[{i}]: 'external_id' is required.\n"
                f"  This becomes the Lago customer identifier."
            )
        if "name" not in cust:
            raise ConfigError(f"customers[{i}] ({cust.get('external_id', '?')}): 'name' is required.")

        resources = []
        for j, res in enumerate(cust.get("resources", [])):
            if not isinstance(res, dict):
                raise ConfigError(
                    f"customers[{i}].resources[{j}]: expected a mapping, got {type(res).__name__}"
                )
            if "provider" not in res:
                raise ConfigError(
                    f"customers[{i}].resources[{j}]: 'provider' is required.\n"
                    f"  Supported providers: aws, azure, gcp, openshift"
                )
            provider = res["provider"]
            if provider not in ("aws", "azure", "gcp", "openshift"):
                raise ConfigError(
                    f"customers[{i}].resources[{j}]: unknown provider '{provider}'.\n"
                    f"  Supported providers: aws, azure, gcp, openshift"
                )
            filter_data = res.get("filter", {})
            if not filter_data:
                raise ConfigError(
                    f"customers[{i}].resources[{j}]: 'filter' is required and must not be empty.\n"
                    f"  Without a filter, all costs for this provider would match.\n"
                    f"  Example: filter: {{account: ['123456789012']}}"
                )
            resources.append(ResourceFilter(provider=provider, filter=filter_data))

        if not resources:
            raise ConfigError(
                f"customers[{i}] ({cust['external_id']}): at least one resource block is required."
            )

        # Parse address if present
        address = None
        addr_data = cust.get("address")
        if addr_data and isinstance(addr_data, dict):
            address = CustomerAddress(
                address_line1=addr_data.get("address_line1", ""),
                address_line2=addr_data.get("address_line2", ""),
                city=addr_data.get("city", ""),
                state=addr_data.get("state", ""),
                zipcode=addr_data.get("zipcode", ""),
                country=addr_data.get("country", ""),
            )

        customers.append(CustomerConfig(
            external_id=cust["external_id"],
            name=cust["name"],
            currency=cust.get("currency", "USD"),
            resources=resources,
            email=cust.get("email", ""),
            legal_name=cust.get("legal_name", ""),
            tax_identification_number=cust.get("tax_identification_number", ""),
            tax_codes=cust.get("tax_codes", []),
            address=address,
        ))

    return AppConfig(
        lago=LagoConfig(
            api_url=lago_data.get("api_url", "http://localhost:3000"),
            api_key=lago_data.get("api_key", ""),
        ),
        cost_management=CostManagementConfig(
            base_url=cm_data.get("base_url", "http://localhost:8000/api/cost-management/v1"),
            identity=cm_data.get("identity", ""),
            org_id=cm_data.get("org_id", ""),
        ),
        sync=SyncConfig(
            ocp_include_overhead=sync_data.get("ocp_include_overhead", True),
            invoice_group_by=sync_data.get("invoice_group_by", {}),
        ),
        customers=customers,
    )
