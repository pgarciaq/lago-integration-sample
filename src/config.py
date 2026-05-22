"""Configuration via environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "LAGO_SYNC_"}

    # Koku / Cost Management API
    koku_base_url: str = Field(default="http://localhost:8000/api/cost-management/v1")
    koku_identity: str = Field(
        default="",
        description="Base64-encoded x-rh-identity header value. Required for cloud; optional if using dev mode.",
    )

    # Lago API
    lago_api_key: str = Field(default="")
    lago_api_url: str = Field(
        default="http://localhost:3000",
        description="Lago base URL. Use https://api.getlago.com for Lago Cloud.",
    )

    # Sync behavior
    providers: list[str] = Field(default=["aws", "azure", "gcp", "openshift"])
    aws_group_by: list[str] = Field(default=["account", "service", "region"])
    azure_group_by: list[str] = Field(default=["subscription_guid", "service_name", "resource_location"])
    gcp_group_by: list[str] = Field(default=["account", "service", "region"])
    ocp_group_by: list[str] = Field(default=["cluster", "project"])

    # Org identifier used as Lago external_customer_id
    org_id: str = Field(default="", description="Koku org_id; becomes the Lago customer external_id")

    # Whether to include distributed overhead as separate line items for OCP
    ocp_include_overhead: bool = Field(default=True)


settings = Settings()
