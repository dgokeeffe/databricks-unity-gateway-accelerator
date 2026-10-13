from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .daily import DailyTierError, normalize_tiers


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


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


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
    return parse_config_text(source.read_text(encoding="utf-8"))


def parse_config_text(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a mapping")
    validate_config(data)
    return data


def _amount(value: Any, field: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be a positive numeric amount") from exc
    if not math.isfinite(number) or number <= 0:
        raise ConfigError(f"{field} must be a positive finite number")
    return str(value)


def _validate_threshold(item: dict[str, Any], field: str) -> None:
    _amount(item.get("amount_usd"), f"{field}.amount_usd")
    for address in item.get("email_recipients", []):
        if not isinstance(address, str) or "@" not in address:
            raise ConfigError(f"{field}.email_recipients contains an invalid address")


BUDGET_ROLES = {"monthly", "runaway"}


def _scope(budget: dict[str, Any]) -> tuple[Any, Any]:
    # str(): example configs keep unresolved "<WORKSPACE_ID>" placeholders.
    workspaces = tuple(sorted(str(v) for v in budget.get("workspace_ids", []) or []))
    tags = tuple(
        sorted((k, tuple(sorted(v))) for k, v in (budget.get("tags") or {}).items())
    )
    return workspaces, tags


def _validate_budget_roles(data: dict[str, Any]) -> None:
    """Validate the monthly + runaway budget pair.

    The runaway ("daily") budget is a second *monthly-period* Unity AI Gateway
    budget over the same scope. Its per-user overrides form a tier ladder of
    month-to-date caps; a raise moves the user into the next tier group. The
    daily_raise ladder and the runaway budget's overrides must therefore be the
    same set of (group, amount) pairs, or the policy would place users in a
    group whose enforced threshold differs from what the policy expects.
    """
    budgets = data.get("budgets", [])
    for budget in budgets:
        role = budget.get("role", "monthly")
        if role not in BUDGET_ROLES:
            raise ConfigError(f"budget.role must be one of {sorted(BUDGET_ROLES)}")
    runaway = [b for b in budgets if b.get("role") == "runaway"]
    monthly = [b for b in budgets if b.get("role", "monthly") == "monthly"]
    if len(runaway) > 1:
        raise ConfigError("declare at most one budget with role: runaway")
    daily_raise = data.get("daily_raise") or {}
    if daily_raise.get("enabled") and not runaway:
        raise ConfigError("enabled daily_raise requires a budget with role: runaway")
    if not runaway:
        return
    budget = runaway[0]
    if not budget.get("per_user_threshold"):
        raise ConfigError("the runaway budget needs a per_user_threshold (the base tier)")
    if monthly and any(_scope(m) != _scope(budget) for m in monthly):
        raise ConfigError(
            "the runaway budget must have the same workspace_ids/tags scope as the monthly budget"
        )
    base = float(budget["per_user_threshold"]["amount_usd"])
    overrides = {
        (o["group"], float(o["amount_usd"])) for o in budget.get("overrides", [])
    }
    if any(amount <= base for _, amount in overrides):
        raise ConfigError("runaway tier overrides must all be above the base per-user threshold")
    if daily_raise.get("enabled"):
        ladder = {
            (t["group"], float(t["limit_usd"])) for t in daily_raise.get("tiers", [])
        }
        if ladder != overrides:
            raise ConfigError(
                "daily_raise.tiers must match the runaway budget overrides exactly "
                "(same groups and amounts)"
            )


def validate_config(data: dict[str, Any]) -> None:
    for key in (
        "groups",
        "budgets",
        "model_services",
        "model_provider_services",
        "grants",
        "governed_tags",
    ):
        if data.get(key) is None:
            data[key] = []
        elif not isinstance(data[key], list):
            raise ConfigError(f"{key} must be a list")

    account = data.get("account")
    if not isinstance(account, dict) or not account.get("id") or not account.get("host"):
        raise ConfigError("account.id and account.host are required")
    _validate_host(account["host"], account=True)
    if any(part in str(account["id"]) for part in ("/", "\\", "..")):
        raise ConfigError("account.id contains invalid path characters")

    runaway = data.get("runaway_budget") or {}
    if not isinstance(runaway, dict):
        raise ConfigError("runaway_budget must be a mapping")
    if runaway.get("enabled"):
        raise ConfigError(
            "runaway_budget.enabled (a native daily-period budget) is not supported by the "
            "standard budget API; declare the runaway budget under budgets with "
            "role: runaway instead"
        )

    daily_raise = data.get("daily_raise", {})
    if not isinstance(daily_raise, dict):
        raise ConfigError("daily_raise must be a mapping")
    if daily_raise:
        enabled = daily_raise.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError("daily_raise.enabled must be a boolean")
        divisor = daily_raise.get("monthly_to_daily_divisor", 15)
        try:
            divisor_number = float(divisor)
            if not math.isfinite(divisor_number) or divisor_number <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ConfigError("daily_raise.monthly_to_daily_divisor must be positive") from exc
        trigger = daily_raise.get("raise_trigger_remaining_usd", 100)
        try:
            trigger_number = float(trigger)
            if not math.isfinite(trigger_number) or trigger_number <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ConfigError("daily_raise.raise_trigger_remaining_usd must be positive") from exc
        tiers = daily_raise.get("tiers", [])
        if not isinstance(tiers, list):
            raise ConfigError("daily_raise.tiers must be a list")
        try:
            normalized_tiers = normalize_tiers(tiers) if tiers else []
        except DailyTierError as exc:
            raise ConfigError(f"invalid daily_raise tiers: {exc}") from exc
        if enabled and not normalized_tiers:
            raise ConfigError("enabled daily_raise requires at least one configured tier")
        if enabled:
            configured_groups = {
                item.get("name") for item in data.get("groups", []) if isinstance(item, dict)
            }
            missing_groups = sorted({tier.group for tier in normalized_tiers} - configured_groups)
            if missing_groups:
                raise ConfigError(
                    "daily_raise tier groups must also be declared under groups: "
                    + ", ".join(missing_groups)
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

    for group in data.get("groups", []):
        members = group.get("members", []) if isinstance(group, dict) else []
        if not isinstance(members, list) or any(
            not isinstance(m, str) or "@" not in m or '"' in m for m in members
        ):
            raise ConfigError(f"groups[{group.get('name')}].members must be a list of user emails")

    _validate_budget_roles(data)

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
        for key in ("payload", "update_payload"):
            if _contains_key(provider.get(key), "plaintext"):
                raise ConfigError(
                    f"model provider {key} must not contain inline 'plaintext' credentials; "
                    "use service_credential or secret_reference"
                )
        if provider.get("update_payload") and not provider.get("update_mask"):
            raise ConfigError(
                "model provider update_payload requires a non-empty update_mask"
            )
        for path in provider.get("update_mask", []):
            if not path.startswith("config."):
                raise ConfigError(
                    "model provider update_mask paths must be explicit config.* fields"
                )
