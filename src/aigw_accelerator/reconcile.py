from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Change:
    action: str
    resource: str
    detail: str
    destructive: bool = False


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)

    def add(self, action: str, resource: str, detail: str, *, destructive: bool = False) -> None:
        self.changes.append(Change(action, resource, detail, destructive))

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


def index_by(items: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(item[key]): item for item in items if key in item}


def plan_groups(desired: list[dict[str, Any]], current: list[dict[str, Any]]) -> Plan:
    plan = Plan()
    current_by_name = index_by(current, "displayName")
    for group in desired:
        name = group["name"]
        if name not in current_by_name:
            plan.add("create", f"group:{name}", group.get("description", ""))
        # Existing descriptions are intentionally not mutated by the baseline.
        # Group ownership and membership changes require a separate reviewed workflow.
    return plan


def plan_budget(current: dict[str, Any] | None, desired: dict[str, Any]) -> Plan:
    plan = Plan()
    resource = f"budget:{desired.get('id') or desired.get('display_name')}"
    if current is None:
        plan.add("create", resource, desired.get("display_name", ""))
        return plan

    current_name = current.get("display_name")
    if desired.get("display_name") and current_name != desired["display_name"]:
        plan.add(
            "update",
            resource,
            f"display_name {current_name!r} -> {desired['display_name']!r}",
        )

    if desired.get("workspace_ids") is not None or desired.get("tags") is not None:
        plan.add("reconcile", resource, "workspace scope and tag filters")
    if desired.get("shared_thresholds") is not None:
        plan.add(
            "reconcile",
            resource,
            "shared thresholds (full alert set is replaced unless preserved)",
            destructive=bool(current.get("alert_configurations")),
        )
    if desired.get("per_user_threshold") is not None:
        plan.add(
            "reconcile",
            resource,
            "per-user threshold (full alert set is replaced unless preserved)",
            destructive=bool(current.get("alert_configurations")),
        )
    if desired.get("overrides"):
        plan.add("reconcile", resource, "group threshold overrides")
    return plan


def plan_model_services(
    desired: list[dict[str, Any]], current: list[dict[str, Any]]
) -> Plan:
    plan = Plan()
    current_by_name = index_by(current, "name")
    for service in desired:
        qualified = ".".join([service["catalog"], service["schema"], service["name"]])
        if qualified not in current_by_name:
            plan.add("create", f"model_service:{qualified}", "create service from declared payload")
        else:
            if service.get("endpoint_tags"):
                plan.add(
                    "reconcile",
                    f"model_service:{qualified}",
                    "endpoint tags (replacement requires explicit opt-in)",
                    destructive=True,
                )
            if service.get("payload", {}).get("rate_limits"):
                plan.add(
                    "reconcile",
                    f"model_service:{qualified}",
                    "rate limits (replacement requires explicit opt-in)",
                    destructive=True,
                )
    return plan


def format_plan(plan: Plan) -> str:
    if not plan.changes:
        return "No changes."
    lines = []
    for change in plan.changes:
        marker = "DESTRUCTIVE " if change.destructive else ""
        lines.append(f"{marker}{change.action.upper():10} {change.resource}: {change.detail}")
    return "\n".join(lines)
