from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    """Raised when desired state is invalid or unsafe."""


ALLOWED_GRANTS = {"EXECUTE", "MANAGE", "USE_CATALOG", "USE_SCHEMA", "SELECT"}
ALLOWED_SECURABLE_TYPES = {"model_service", "model_provider_service"}
ALLOWED_ACCOUNT_HOSTS = {
    "accounts.azuredatabricks.net",
    "accounts.cloud.databricks.com",
    "accounts.gcp.databricks.com",
}
ALLOWED_WORKSPACE_SUFFIXES = (
    ".azuredatabricks.net",
    ".cloud.databricks.com",
    ".gcp.databricks.com",
)


def _validate_host(value: Any, *, account: bool) -> None:
    parsed = urlparse(str(value))
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname:
        raise ConfigError("Databricks hosts must use https and include a hostname")
    if account:
        if hostname not in ALLOWED_ACCOUNT_HOSTS:
            raise ConfigError(f"unsupported Databricks account host: {hostname}")
    elif not hostname.endswith(ALLOWED_WORKSPACE_SUFFIXES):
        raise ConfigError(f"unsupported Databricks workspace host: {hostname}")


def load_config(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise ConfigError(f"Configuration file does not exist: {source}")
    with source.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a mapping")
    validate_config(data)
    return data


def _amount(value: Any, field: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be a positive numeric amount") from exc
    if number <= 0:
        raise ConfigError(f"{field} must be greater than zero")
    return str(value)


def _validate_threshold(item: dict[str, Any], field: str) -> None:
    _amount(item.get("amount_usd"), f"{field}.amount_usd")
    for address in item.get("email_recipients", []):
        if not isinstance(address, str) or "@" not in address:
            raise ConfigError(f"{field}.email_recipients contains an invalid address")


def validate_config(data: dict[str, Any]) -> None:
    account = data.get("account")
    if not isinstance(account, dict) or not account.get("id") or not account.get("host"):
        raise ConfigError("account.id and account.host are required")
    _validate_host(account["host"], account=True)
    if any(part in str(account["id"]) for part in ("/", "\\", "..")):
        raise ConfigError("account.id contains invalid path characters")

    runaway = data.get("runaway_budget", {})
    if runaway.get("enabled"):
        raise ConfigError(
            "runaway_budget.enabled is not supported by the standard budget API; "
            "confirm a supported account capability before enabling it"
        )

    for budget in data.get("budgets", []):
        if not isinstance(budget, dict):
            raise ConfigError("budgets entries must be mappings")
        if not budget.get("id") and not budget.get("display_name"):
            raise ConfigError("Each budget needs id or display_name")
        if budget.get("id") and any(part in str(budget["id"]) for part in ("/", "\\", "..")):
            raise ConfigError("budget.id contains invalid path characters")
        if budget.get("resource_type") != "BUDGET_RESOURCE_TYPE_UNITY_AI_GATEWAY":
            raise ConfigError("This accelerator only manages Unity AI Gateway budgets")
        for index, item in enumerate(budget.get("shared_thresholds", [])):
            _validate_threshold(item, f"budget.shared_thresholds[{index}]")
        if budget.get("per_user_threshold"):
            _validate_threshold(budget["per_user_threshold"], "budget.per_user_threshold")
        for index, override in enumerate(budget.get("overrides", [])):
            if not override.get("group"):
                raise ConfigError(f"budget.overrides[{index}].group is required")
            _amount(override.get("amount_usd"), f"budget.overrides[{index}].amount_usd")

    for service in data.get("model_services", []) + data.get("model_provider_services", []):
        _validate_host(service.get("workspace_host"), account=False)
        for key in ("workspace_host", "workspace_id", "catalog", "schema", "name"):
            if key not in service:
                raise ConfigError(f"service is missing {key}")
        if any(part in service["name"] for part in ("/", "\\", "..")):
            raise ConfigError(f"invalid service name: {service['name']!r}")

    for grant in data.get("grants", []):
        _validate_host(grant.get("workspace_host"), account=False)
        if grant.get("securable_type") not in ALLOWED_SECURABLE_TYPES:
            raise ConfigError(
                f"grants.securable_type must be one of {sorted(ALLOWED_SECURABLE_TYPES)}"
            )
        if not grant.get("workspace_host") or not grant.get("workspace_id"):
            raise ConfigError("each grant needs workspace_host and workspace_id")
        for item in grant.get("grants", []):
            unknown = set(item.get("privileges", [])) - ALLOWED_GRANTS
            if unknown:
                raise ConfigError(f"unsupported privileges: {sorted(unknown)}")

    for tag in data.get("governed_tags", []):
        if tag.get("ensure"):
            raise ConfigError(
                "governed tag creation is not implemented in the baseline; create it through "
                "the governed-tags API/SQL after capability review"
            )
        if tag.get("assignments"):
            raise ConfigError(
                "governed-tag assignments are not implemented in the baseline; use endpoint_tags "
                "for Model Services until the target API supports model_service assignments"
            )

    for provider in data.get("model_provider_services", []):
        if provider.get("update_payload") and not provider.get("update_mask"):
            raise ConfigError(
                "model provider update_payload requires a non-empty update_mask"
            )
        for path in provider.get("update_mask", []):
            if not path.startswith("config."):
                raise ConfigError(
                    "model provider update_mask paths must be explicit config.* fields"
                )
