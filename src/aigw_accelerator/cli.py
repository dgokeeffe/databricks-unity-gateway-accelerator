from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .account import AccountAutomation, make_account_client
from .config import ConfigError, load_config
from .gateway import (
    body_for,
    is_ga_service,
    provider_drift,
    reasoning_api_warnings,
    service_drift,
)
from .reconcile import (
    format_plan,
    plan_budget,
    plan_groups,
    plan_model_services,
)
from .workspace import WorkspaceAutomation, make_workspace_client


def _budget_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return value.as_dict()


def _filter_payload(desired: dict[str, Any]) -> dict[str, Any] | None:
    clauses: dict[str, Any] = {}
    if desired.get("workspace_ids"):
        clauses["workspace_id"] = {
            "operator": "IN",
            "values": [int(v) for v in desired["workspace_ids"]],
        }
    if desired.get("tags"):
        clauses["tags"] = [
            {"key": key, "value": {"operator": "IN", "values": list(values)}}
            for key, values in desired["tags"].items()
        ]
    return clauses or None


def _actions(item: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    actions.extend(
        {"action_type": "EMAIL_NOTIFICATION", "target": address}
        for address in item.get("email_recipients", [])
    )
    if item.get("block"):
        actions.append({"action_type": "BLOCK_USAGE"})
    return actions


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _decimal_text(value: Any) -> str:
    try:
        # normalize() so "100" and "100.000000000000000000" compare equal.
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _alert_key(alert: dict[str, Any]) -> str:
    comparable = {
        "scope_type": str(_enum_value(alert.get("scope_type"))),
        "quantity_threshold": _decimal_text(alert.get("quantity_threshold")),
        "quantity_type": str(_enum_value(alert.get("quantity_type"))),
        "time_period": str(_enum_value(alert.get("time_period"))),
        "trigger_type": str(_enum_value(alert.get("trigger_type"))),
        "action_configurations": sorted(
            (
                str(_enum_value(item.get("action_type"))),
                "" if item.get("target") is None else str(item.get("target")),
            )
            for item in alert.get("action_configurations", [])
        ),
        "principal_overrides": sorted(
            (
                str(item.get("principal_id")),
                _decimal_text(item.get("override_threshold")),
            )
            for item in alert.get("principal_overrides", [])
        ),
    }
    return json.dumps(comparable, sort_keys=True)


def _desired_alerts(
    desired: dict[str, Any], principal_ids: dict[str, int]
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for threshold in desired.get("shared_thresholds", []):
        alerts.append(
            {
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                "quantity_threshold": str(threshold["amount_usd"]),
                "quantity_type": "LIST_PRICE_DOLLARS_USD",
                "time_period": "MONTH",
                "trigger_type": "CUMULATIVE_SPENDING_EXCEEDED",
                "action_configurations": _actions(threshold),
            }
        )

    per_user = desired.get("per_user_threshold")
    if per_user:
        overrides = []
        for item in desired.get("overrides", []):
            if item["group"] not in principal_ids:
                raise ConfigError(f"Budget override group not found: {item['group']}")
            overrides.append(
                {
                    "principal_id": principal_ids[item["group"]],
                    "override_threshold": str(item["amount_usd"]),
                }
            )
        alerts.append(
            {
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "quantity_threshold": str(per_user["amount_usd"]),
                "quantity_type": "LIST_PRICE_DOLLARS_USD",
                "time_period": "MONTH",
                "trigger_type": "CUMULATIVE_SPENDING_EXCEEDED",
                "action_configurations": _actions(per_user),
                "principal_overrides": overrides,
            }
        )
    return alerts


def _budget_payload(
    current: dict[str, Any], desired: dict[str, Any], principal_ids: dict[str, int]
) -> dict[str, Any]:
    """Build a safe full-replacement budget payload.

    Existing alert configurations and scope are never silently removed. An
    operator must declare the complete desired alert set or explicitly opt in
    to replacement.
    """
    payload = dict(current)
    payload["display_name"] = desired.get("display_name", payload.get("display_name"))
    payload["resource_type"] = desired.get("resource_type", payload.get("resource_type"))
    if desired.get("account_id"):
        payload["account_id"] = desired["account_id"]

    scope_declared = "workspace_ids" in desired or "tags" in desired
    if scope_declared:
        requested_scope = _filter_payload(desired) or {}
        current_scope = current.get("filter") or {}
        removed_clauses = set(current_scope) - set(requested_scope)
        changed_clauses = {
            key for key in set(current_scope) & set(requested_scope)
            if current_scope[key] != requested_scope[key]
        }
        if (removed_clauses or changed_clauses) and not desired.get("allow_scope_widening"):
            changed = sorted(removed_clauses | changed_clauses)
            raise ConfigError(
                "Refusing to change budget scope clauses "
                f"{changed}; set allow_scope_widening only after review"
            )
        payload["filter"] = requested_scope or None

    desired_alerts = _desired_alerts(desired, principal_ids)
    if desired_alerts:
        current_alerts = [dict(alert) for alert in current.get("alert_configurations", [])]
        desired_keys = {_alert_key(alert) for alert in desired_alerts}
        current_keys = {_alert_key(alert) for alert in current_alerts}
        omitted = current_keys - desired_keys
        if omitted and not desired.get("allow_alert_replacement"):
            raise ConfigError(
                "Refusing to remove existing budget alerts; declare the complete alert set "
                "or set allow_alert_replacement only after review"
            )
        current_by_key = {_alert_key(alert): alert for alert in current_alerts}
        for alert in desired_alerts:
            previous = current_by_key.get(_alert_key(alert))
            if previous and previous.get("alert_configuration_id"):
                alert["alert_configuration_id"] = previous["alert_configuration_id"]
        payload["alert_configurations"] = desired_alerts
    return payload


def _discover(config: dict[str, Any]) -> tuple[AccountAutomation, list[dict[str, Any]], list[Any]]:
    account_client = make_account_client(config["account"])
    account = AccountAutomation(account_client)
    workspace_specs = {
        (int(item["workspace_id"]), item["workspace_host"])
        for item in config.get("model_services", [])
        + config.get("model_provider_services", [])
        + config.get("grants", [])
    }
    for workspace_id, workspace_host in workspace_specs:
        account.validate_workspace_identity(workspace_id, workspace_host)
    return account, account.list_groups(), account.list_budgets()


def build_plan(config: dict[str, Any]) -> str:
    _, current_groups, current_budgets = _discover(config)
    plan_lines = ["Account groups:"]
    managed_groups = [group for group in config.get("groups", []) if group.get("ensure", True)]
    plan_lines.append(format_plan(plan_groups(managed_groups, current_groups)))

    current_by_id = {
        str(getattr(budget, "budget_configuration_id", "")): _budget_dict(budget)
        for budget in current_budgets
    }
    plan_lines.append("Budgets:")
    for desired in config.get("budgets", []):
        current = current_by_id.get(str(desired.get("id"))) if desired.get("id") else None
        plan_lines.append(format_plan(plan_budget(current, desired)))

    for service in config.get("model_services", []):
        workspace = WorkspaceAutomation(
            make_workspace_client(service["workspace_host"], service.get("profile")), int(service["workspace_id"])
        )
        existing = workspace.list_model_services(service["catalog"], service["schema"])
        text = format_plan(plan_model_services([service], existing))
        qualified = ".".join([service["catalog"], service["schema"], service["name"]])
        if is_ga_service(service) and any(item.get("name") == qualified for item in existing):
            drift = service_drift(service, workspace.get_model_service(qualified))
            if drift:
                gate = "" if service.get("allow_service_config_replacement") else (
                    " (BLOCKED: set allow_service_config_replacement after review)"
                )
                text = f"UPDATE     model_service:{qualified}: {', '.join(drift)}{gate}"
        plan_lines.append(f"Model services {service['workspace_host']}:\n{text}")

    for provider in config.get("model_provider_services", []):
        workspace = WorkspaceAutomation(
            make_workspace_client(provider["workspace_host"], provider.get("profile")), int(provider["workspace_id"])
        )
        existing = workspace.list_model_provider_services(
            provider["catalog"], provider["schema"]
        )
        qualified = ".".join([provider["catalog"], provider["schema"], provider["name"]])
        current = next((item for item in existing if item.get("name") == qualified), None)
        host = provider["workspace_host"]
        if current is None:
            plan_lines.append(f"CREATE model provider service {host}: {qualified}")
        else:
            drift = provider_drift(provider, workspace.get_model_provider_service(qualified))
            if drift:
                gate = "" if provider.get("allow_service_config_replacement") else (
                    " (BLOCKED: set allow_service_config_replacement after review)"
                )
                plan_lines.append(
                    f"UPDATE model provider service {host}: {qualified}: "
                    f"{', '.join(drift)}{gate}"
                )
            else:
                plan_lines.append(f"No changes: model provider service {host}: {qualified}")
    for warning in reasoning_api_warnings(config):
        plan_lines.append(f"WARNING: {warning}")

    for grant in config.get("grants", []):
        plan_lines.append(
            f"Grant {grant['workspace_host']} {grant['securable_type']} "
            f"{grant['full_name']}: {grant.get('grants', [])}"
        )
    daily_raise = config.get("daily_raise", {})
    if daily_raise:
        tiers = daily_raise.get("tiers", [])
        state = "enabled" if daily_raise.get("enabled") else "configured but disabled"
        plan_lines.append(
            "Runaway tier policy (tier groups mapped to the role: runaway budget's "
            f"per-user overrides): {state}; monthly-cap divisor="
            f"{daily_raise.get('monthly_to_daily_divisor', 15)}; raise trigger remaining="
            f"${daily_raise.get('raise_trigger_remaining_usd', 100)}; tiers={len(tiers)}. "
            "The runaway budget enforces; telemetry estimates only inform tier selection."
        )
        if daily_raise.get("enabled"):
            current_group_names = {group.get("displayName") for group in current_groups}
            missing_tiers = sorted(
                tier["group"]
                for tier in tiers
                if tier["group"] not in current_group_names
            )
            if missing_tiers:
                plan_lines.append("MISSING daily tier groups: " + ", ".join(missing_tiers))
    if config.get("governed_tags"):
        plan_lines.append(
            "Governed tags: declared as a capability-gated phase; no Model Service assignment "
            "is applied by this baseline."
        )
    return "\n".join(plan_lines)


def _ensure_groups(account: AccountAutomation, config: dict[str, Any]) -> dict[str, int]:
    principal_ids: dict[str, int] = {}
    for group in config.get("groups", []):
        if not group.get("ensure", True):
            continue
        actual = account.ensure_group(group["name"], group.get("description", ""))
        if actual.get("id"):
            principal_ids[group["name"]] = int(actual["id"])
            _ensure_members(account, str(actual["id"]), group)
    return principal_ids


def _ensure_members(account: AccountAutomation, group_id: str, group: dict[str, Any]) -> None:
    """Additively ensure declared members. Never removes undeclared members."""
    for email in group.get("members", []) or []:
        user_id = account.find_user_id(email)
        if user_id is None:
            raise ConfigError(f"group {group['name']}: no account user for {email}")
        if not account.group_has_member(group_id, user_id):
            account.patch_group_member(group_id, user_id, add=True)


def _apply_budgets(
    account: AccountAutomation,
    config: dict[str, Any],
    current_budgets: list[Any],
    principal_ids: dict[str, int],
) -> None:
    current_by_id = {
        str(getattr(budget, "budget_configuration_id", "")): _budget_dict(budget)
        for budget in current_budgets
    }
    for desired in config.get("budgets", []):
        budget_id = desired.get("id")
        if budget_id:
            current = current_by_id.get(str(budget_id))
            if current is None:
                raise ConfigError(f"Budget not found: {budget_id}")
            payload = _budget_payload(current, desired, principal_ids)
            account.client.api_client.do(
                "PUT",
                f"/api/2.1/accounts/{config['account']['id']}/budgets/{budget_id}",
                body={"budget": payload},
                headers={"Content-Type": "application/json"},
            )
        else:
            if not desired.get("display_name"):
                raise ConfigError("A new budget requires display_name")
            desired_with_account = {**desired, "account_id": config["account"]["id"]}
            payload = _budget_payload({}, desired_with_account, principal_ids)
            account.client.api_client.do(
                "POST",
                f"/api/2.1/accounts/{config['account']['id']}/budgets",
                body={"budget": payload},
                headers={"Content-Type": "application/json"},
            )


def _desired_inference_table(service: dict[str, Any]) -> dict[str, Any] | None:
    return (service.get("payload", {}).get("config") or {}).get("inference_table")


def _ensure_inference_table(
    workspace: WorkspaceAutomation, qualified: str, service: dict[str, Any]
) -> None:
    """Enable payload logging on an existing service (additive only).

    parent/table_name_prefix are immutable once the table exists, so a
    mismatch is reported rather than replaced.
    """
    desired = _desired_inference_table(service)
    if not desired:
        return
    current = (workspace.get_model_service(qualified).get("config") or {}).get(
        "inference_table"
    )
    if current and not current.get("disabled"):
        for key in ("parent", "table_name_prefix"):
            if key in desired and current.get(key) not in (None, desired[key]):
                raise ConfigError(
                    f"inference_table.{key} on {qualified} is immutable once created: "
                    f"{current.get(key)!r} != {desired[key]!r}"
                )
        return
    workspace.update_model_service(
        qualified,
        {"name": f"model-services/{qualified}", "config": {"inference_table": desired}},
        ["config.inference_table"],
    )


def _apply_services_and_grants(config: dict[str, Any]) -> None:
    for provider in config.get("model_provider_services", []):
        workspace = WorkspaceAutomation(
            make_workspace_client(provider["workspace_host"], provider.get("profile")), int(provider["workspace_id"])
        )
        existing = workspace.list_model_provider_services(
            provider["catalog"], provider["schema"]
        )
        qualified = ".".join(
            [provider["catalog"], provider["schema"], provider["name"]]
        )
        current = next((item for item in existing if item.get("name") == qualified), None)
        if current is None:
            payload = dict(provider.get("payload", {}))
            payload.update(
                {
                    "catalog_name": provider["catalog"],
                    "schema_name": provider["schema"],
                    "name": provider["name"],
                }
            )
            workspace.create_model_provider_service(payload)
        else:
            # Only PATCH declared fields that actually differ (idempotent apply).
            drift = provider_drift(provider, workspace.get_model_provider_service(qualified))
            if drift:
                if not provider.get("allow_service_config_replacement"):
                    raise ConfigError(
                        f"Refusing to replace existing provider configuration on {qualified}; "
                        "set allow_service_config_replacement after review"
                    )
                workspace.update_model_provider_service(
                    qualified, body_for(drift), list(drift)
                )

    for service in config.get("model_services", []):
        workspace = WorkspaceAutomation(
            make_workspace_client(service["workspace_host"], service.get("profile")), int(service["workspace_id"])
        )
        existing = workspace.list_model_services(service["catalog"], service["schema"])
        qualified = ".".join([service["catalog"], service["schema"], service["name"]])
        current = next((item for item in existing if item.get("name") == qualified), None)
        if current is None:
            payload = dict(service.get("payload", {}))
            payload.update(
                {
                    "catalog_name": service["catalog"],
                    "schema_name": service["schema"],
                    "name": service["name"],
                }
            )
            if service.get("endpoint_tags"):
                payload["endpoint_tags"] = service["endpoint_tags"]
            workspace.create_model_service(payload)
        elif is_ga_service(service):
            drift = service_drift(service, workspace.get_model_service(qualified))
            if drift:
                if not service.get("allow_service_config_replacement"):
                    raise ConfigError(
                        f"Refusing to replace {', '.join(drift)} on {qualified}; "
                        "set allow_service_config_replacement after review"
                    )
                workspace.update_model_service(qualified, body_for(drift), list(drift))
            _ensure_inference_table(workspace, qualified, service)
        else:
            update_fields: list[str] = []
            update_config: dict[str, Any] = {}
            if service.get("endpoint_tags"):
                update_config["endpoint_tags"] = service["endpoint_tags"]
                update_fields.append("config.endpoint_tags")
            if service.get("payload", {}).get("rate_limits"):
                update_config["rate_limits"] = service["payload"]["rate_limits"]
                update_fields.append("config.rate_limits")
            if update_fields:
                current_detail = workspace.get_model_service(qualified)
                current_config = current_detail.get("config", {})
                current_tags = current_detail.get(
                    "endpoint_tags", current_config.get("endpoint_tags")
                )
                current_limits = current_detail.get(
                    "rate_limits", current_config.get("rate_limits")
                )
                if "config.endpoint_tags" in update_fields and current_tags is None:
                    if not service.get("allow_service_config_replacement"):
                        raise ConfigError(
                            f"Cannot verify existing endpoint_tags on {qualified}; "
                            "set allow_service_config_replacement after review"
                        )
                if "config.rate_limits" in update_fields and current_limits is None:
                    if not service.get("allow_service_config_replacement"):
                        raise ConfigError(
                            f"Cannot verify existing rate_limits on {qualified}; "
                            "set allow_service_config_replacement after review"
                        )
                if (
                    "config.endpoint_tags" in update_fields
                    and current_tags not in (None, update_config["endpoint_tags"])
                ):
                    if not service.get("allow_service_config_replacement"):
                        raise ConfigError(
                            f"Refusing to replace existing endpoint_tags on {qualified}; "
                            "set allow_service_config_replacement after review"
                        )
                if (
                    "config.rate_limits" in update_fields
                    and current_limits not in (None, update_config["rate_limits"])
                ):
                    if not service.get("allow_service_config_replacement"):
                        raise ConfigError(
                            f"Refusing to replace existing rate_limits on {qualified}; "
                            "set allow_service_config_replacement after review"
                        )
                workspace.update_model_service(
                    qualified,
                    {"name": f"model-services/{qualified}", "config": update_config},
                    update_fields,
                )
            _ensure_inference_table(workspace, qualified, service)

    declared_services = {
        (
            service["workspace_host"],
            int(service["workspace_id"]),
            service["catalog"],
            service["schema"],
            service["name"],
        )
        for service in config.get("model_services", [])
        + config.get("model_provider_services", [])
    }
    for grant in config.get("grants", []):
        match = next(
            (
                key
                for key in declared_services
                if key[0] == grant["workspace_host"]
                and key[1] == int(grant["workspace_id"])
                and ".".join(key[2:]) == grant["full_name"]
            ),
            None,
        )
        if match is None:
            raise ConfigError(
                f"Grant does not match a declared service in the same workspace: {grant}"
            )
        workspace = WorkspaceAutomation(
            make_workspace_client(grant["workspace_host"], grant.get("profile")), int(grant["workspace_id"])
        )
        workspace.update_grants(
            grant["securable_type"], grant["full_name"], grant.get("grants", []), revoke=False
        )


def write_audit_record(
    config: dict[str, Any],
    *,
    status: str,
    apply_budgets: bool,
    error: str | None = None,
) -> Path:
    output = Path("plan-output")
    output.mkdir(exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "account_id": config["account"]["id"],
        "apply_budgets": apply_budgets,
        "budgets": [
            item.get("id") or item.get("display_name")
            for item in config.get("budgets", [])
        ],
        "groups": [item["name"] for item in config.get("groups", [])],
        "model_services": [
            ".".join([item["catalog"], item["schema"], item["name"]])
            for item in config.get("model_services", [])
        ],
        "model_provider_services": [
            ".".join([item["catalog"], item["schema"], item["name"]])
            for item in config.get("model_provider_services", [])
        ],
        "grants": [
            {
                "workspace_host": item["workspace_host"],
                "workspace_id": item["workspace_id"],
                "securable_type": item["securable_type"],
                "full_name": item["full_name"],
                "grants": item.get("grants", []),
            }
            for item in config.get("grants", [])
        ],
        "governed_tags_applied": False,
        "destructive_opt_ins": {
            "allow_alert_replacement": any(
                item.get("allow_alert_replacement") for item in config.get("budgets", [])
            ),
            "allow_scope_widening": any(
                item.get("allow_scope_widening") for item in config.get("budgets", [])
            ),
            "allow_service_config_replacement": any(
                item.get("allow_service_config_replacement")
                for item in config.get("model_services", [])
                + config.get("model_provider_services", [])
            ),
        },
    }
    if error:
        record["error_type"] = error
    path = output / (
        f"apply-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8]}.json"
    )
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def apply_config(
    config: dict[str, Any],
    *,
    apply_budgets: bool = False,
    audit_writer: Callable[..., Any] | None = None,
) -> tuple[Any, Any]:
    """Apply declared groups, budgets (optionally), services and grants.

    ``audit_writer`` receives ``config``, ``status``, ``apply_budgets``, and
    optional ``error`` keyword arguments. It is called before and after writes
    so a caller can persist a durable audit record before mutation begins.
    """
    writer = audit_writer or write_audit_record
    started = writer(config, status="started", apply_budgets=apply_budgets)
    try:
        account, _, current_budgets = _discover(config)
        principal_ids = _ensure_groups(account, config)
        if apply_budgets:
            _apply_budgets(account, config, current_budgets, principal_ids)
        _apply_services_and_grants(config)
    except Exception as exc:
        try:
            writer(
                config,
                status="failed",
                apply_budgets=apply_budgets,
                error=type(exc).__name__,
            )
        except Exception as audit_error:
            raise RuntimeError(
                "apply failed and the failure audit record could not be persisted"
            ) from audit_error
        raise
    try:
        completed = writer(config, status="succeeded", apply_budgets=apply_budgets)
    except Exception as audit_error:
        raise RuntimeError(
            "apply completed but the completion audit could not be persisted; "
            "inspect the started audit record and remote state before retrying"
        ) from audit_error
    return started, completed


def verify_config(config: dict[str, Any]) -> list[str]:
    account, current_groups, current_budgets = _discover(config)
    group_names = {group.get("displayName") for group in current_groups}
    current_budget_by_id = {
        str(getattr(budget, "budget_configuration_id", "")): _budget_dict(budget)
        for budget in current_budgets
    }
    failures: list[str] = []
    for group in config.get("groups", []):
        if group.get("ensure", True) and group["name"] not in group_names:
            failures.append(f"missing group: {group['name']}")
        elif group.get("members"):
            actual = account.get_group_by_name(group["name"]) or {}
            for email in group["members"]:
                user_id = account.find_user_id(email)
                if user_id is None or not account.group_has_member(str(actual.get("id")), user_id):
                    failures.append(f"missing member {email} in group {group['name']}")
    for tier in config.get("daily_raise", {}).get("tiers", []):
        if config.get("daily_raise", {}).get("enabled") and tier["group"] not in group_names:
            failures.append(f"missing daily tier group: {tier['group']}")
    principal_ids = account.principal_ids()
    for budget in config.get("budgets", []):
        if not budget.get("id"):
            failures.append(
                "new budget requires apply verification by returned id: "
                f"{budget.get('display_name')}"
            )
            continue
        current = current_budget_by_id.get(str(budget["id"]))
        if current is None:
            failures.append(f"missing budget: {budget['id']}")
            continue
        if budget.get("display_name") and current.get("display_name") != budget["display_name"]:
            failures.append(f"drifted budget display_name: {budget['id']}")
        if "workspace_ids" in budget or "tags" in budget:
            expected_filter = _filter_payload(budget)
            if current.get("filter") != expected_filter:
                failures.append(f"drifted budget scope: {budget['id']}")
        desired_alerts = _desired_alerts(budget, principal_ids)
        if desired_alerts:
            current_keys = {
                _alert_key(alert) for alert in current.get("alert_configurations", [])
            }
            missing_alerts = {
                _alert_key(alert) for alert in desired_alerts
            } - current_keys
            if missing_alerts:
                failures.append(f"drifted budget alerts: {budget['id']}")
    declared_services = {
        (
            service["workspace_host"],
            int(service["workspace_id"]),
            service["catalog"],
            service["schema"],
            service["name"],
        )
        for service in config.get("model_services", [])
        + config.get("model_provider_services", [])
    }
    for service in config.get("model_services", []):
        workspace = WorkspaceAutomation(
            make_workspace_client(service["workspace_host"], service.get("profile")), int(service["workspace_id"])
        )
        qualified = ".".join([service["catalog"], service["schema"], service["name"]])
        current = next(
            (
                item
                for item in workspace.list_model_services(service["catalog"], service["schema"])
                if item.get("name") == qualified
            ),
            None,
        )
        if current is None:
            failures.append(f"missing model service: {service['workspace_host']} {qualified}")
        else:
            expected_tags = service.get("endpoint_tags", {})
            actual_tags = current.get("endpoint_tags", {})
            if expected_tags and actual_tags != expected_tags:
                failures.append(f"drifted endpoint_tags: {service['workspace_host']} {qualified}")
            if _desired_inference_table(service):
                table = (current.get("config") or {}).get("inference_table") or {}
                if not table or table.get("disabled") or table.get("is_deleted"):
                    failures.append(f"missing inference table: {qualified}")
            expected_limits = service.get("payload", {}).get("rate_limits")
            actual_config = current.get("config", {})
            actual_limits = current.get("rate_limits", actual_config.get("rate_limits"))
            if expected_limits and actual_limits != expected_limits:
                failures.append(f"drifted rate_limits: {service['workspace_host']} {qualified}")
            if is_ga_service(service):
                for path in service_drift(service, workspace.get_model_service(qualified)):
                    failures.append(f"drifted {path}: {service['workspace_host']} {qualified}")

    for provider in config.get("model_provider_services", []):
        workspace = WorkspaceAutomation(
            make_workspace_client(provider["workspace_host"], provider.get("profile")), int(provider["workspace_id"])
        )
        qualified = ".".join([provider["catalog"], provider["schema"], provider["name"]])
        names = {
            item.get("name")
            for item in workspace.list_model_provider_services(
                provider["catalog"], provider["schema"]
            )
        }
        if qualified not in names:
            failures.append(
                f"missing model provider service: {provider['workspace_host']} {qualified}"
            )
        else:
            current = workspace.get_model_provider_service(qualified)
            for path in provider_drift(provider, current):
                failures.append(f"drifted {path}: {provider['workspace_host']} {qualified}")

    for grant in config.get("grants", []):
        match = next(
            (
                key
                for key in declared_services
                if key[0] == grant["workspace_host"]
                and key[1] == int(grant["workspace_id"])
                and ".".join(key[2:]) == grant["full_name"]
            ),
            None,
        )
        if match is None:
            failures.append(f"grant is not attached to a declared service: {grant['full_name']}")
            continue
        workspace = WorkspaceAutomation(
            make_workspace_client(grant["workspace_host"], grant.get("profile")), int(grant["workspace_id"])
        )
        state = workspace.get_grants(grant["securable_type"], grant["full_name"])
        assignments = {
            item.get("principal"): set(item.get("privileges", []))
            for item in state.get("privilege_assignments", [])
        }
        expected_assignments = {
            expected["principal"]: set(expected.get("privileges", []))
            for expected in grant.get("grants", [])
        }
        for principal, expected_privileges in expected_assignments.items():
            missing = expected_privileges - assignments.get(principal, set())
            if missing:
                failures.append(
                    f"missing grants on {grant['full_name']} for {principal}: "
                    f"{sorted(missing)}"
                )
        for principal, actual_privileges in assignments.items():
            unexpected = actual_privileges - expected_assignments.get(principal, set())
            if unexpected:
                failures.append(
                    f"unexpected grants on {grant['full_name']} for {principal}: "
                    f"{sorted(unexpected)}"
                )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aigw-accelerator")
    parser.add_argument(
        "command", choices=["plan", "apply", "verify", "tier-preview", "tier-move", "tier-reset"]
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--apply-budgets", action="store_true")
    parser.add_argument("--email", help="tier-preview/tier-move: account user")
    parser.add_argument("--kind", choices=["monthly", "runaway"], default="runaway")
    parser.add_argument("--target", help="tier group name, or 'base' (monthly only)")
    parser.add_argument("--spend", type=float, help="month-to-date spend for the runaway self-raise rule")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
        if args.command.startswith("tier-"):
            return _tier_command(config, args)
        if args.command == "plan":
            print(build_plan(config))
            return 0
        if args.command == "verify":
            failures = verify_config(config)
            if failures:
                print("Verification failed:\n" + "\n".join(f"- {item}" for item in failures))
                return 1
            print("Verification passed.")
            return 0
        if not args.confirm:
            print("Refusing to apply without --confirm", file=sys.stderr)
            return 2

        _, audit_path = apply_config(config, apply_budgets=args.apply_budgets)
        print(f"Apply completed. Audit record: {audit_path}")
        return 0
    except Exception as exc:  # CLI boundary: concise failure without request payloads.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _tier_command(config: dict[str, Any], args: argparse.Namespace) -> int:
    from . import tiers

    account = AccountAutomation(make_account_client(config["account"]))
    if args.command == "tier-reset":
        result = tiers.reset_runaway(account, config, dry_run=not args.confirm)
        if args.confirm:
            result["audit"] = str(tiers.write_tier_audit(result, actor="cli", action="reset"))
        print(json.dumps(result, indent=2))
        return 0
    if not args.email:
        print("--email is required", file=sys.stderr)
        return 2
    plan = tiers.preview(
        account, config, email=args.email, kind=args.kind, target=args.target, spend_usd=args.spend
    )
    if args.command == "tier-move":
        if not args.confirm:
            print(json.dumps(plan, indent=2))
            print("Refusing to move without --confirm", file=sys.stderr)
            return 2
        plan = tiers.apply_move(account, plan)
        plan["audit"] = str(tiers.write_tier_audit(plan, actor="cli", action="move"))
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
