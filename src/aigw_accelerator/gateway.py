"""Pure desired-vs-live comparison for GA Unity AI Gateway services.

Only fields declared in configuration are compared, so unmanaged live fields
are never reported as drift. Each drift item maps directly to an update_mask
path accepted by the GA PATCH APIs.
"""

from __future__ import annotations

import re
from typing import Any

RESPONSES_API = "openai/v1/responses"
# OpenAI reasoning families reject function tools with reasoning_effort on
# /v1/chat/completions; tool-calling agents must use the Responses API.
_REASONING_MODEL = re.compile(r"^(gpt-5|gpt-6|o\d)", re.IGNORECASE)

SERVICE_PATHS = ("config.routing.destinations", "config.rate_limits")
PROVIDER_PATHS = ("config.targets", "config.allow_all_targets")


def _get(data: dict[str, Any] | None, path: str) -> Any:
    value: Any = data or {}
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _norm_target(target: dict[str, Any] | None) -> dict[str, Any]:
    target = target or {}
    return {
        "model": target.get("model"),
        "native_api_types": sorted(target.get("native_api_types") or []),
    }


def _norm_destination(dest: dict[str, Any]) -> dict[str, Any]:
    external = dest.get("external_model_config")
    return {
        "name": dest.get("name"),
        "destination_type": dest.get("destination_type"),
        "traffic_percentage": dest.get("traffic_percentage"),
        "pay_per_token_config": dest.get("pay_per_token_config"),
        "provisioned_throughput_config": dest.get("provisioned_throughput_config"),
        "external_model_config": None
        if external is None
        else {
            "model_provider_service": external.get("model_provider_service"),
            "target": _norm_target(external.get("target")),
        },
    }


def _norm_rate_limit(limit: dict[str, Any]) -> dict[str, Any]:
    # The API returns int64 fields as strings; config uses integers.
    return {
        "key": limit.get("key"),
        "renewal_period": limit.get("renewal_period"),
        "principal": limit.get("principal"),
        "requests": None if limit.get("requests") is None else int(limit["requests"]),
        "tokens": None if limit.get("tokens") is None else int(limit["tokens"]),
    }


def _normalise(path: str, value: Any) -> Any:
    if value is None:
        return None
    if path == "config.routing.destinations":
        return [_norm_destination(item) for item in value]
    if path == "config.rate_limits":
        return sorted(
            (_norm_rate_limit(item) for item in value),
            key=lambda item: (item["key"] or "", item["principal"] or ""),
        )
    if path == "config.targets":
        return sorted((_norm_target(item) for item in value), key=lambda t: t["model"] or "")
    return value


def _drift(
    desired: dict[str, Any], current: dict[str, Any], paths: tuple[str, ...]
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for path in paths:
        wanted = _get(desired, path)
        if wanted is None:
            continue
        if _normalise(path, wanted) != _normalise(path, _get(current, path)):
            changes[path] = wanted
    return changes


def is_ga_service(service: dict[str, Any]) -> bool:
    return isinstance(service.get("payload", {}).get("config"), dict)


def service_drift(service: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Return {mask_path: desired_value} for declared GA model-service fields."""
    if not is_ga_service(service):
        return {}
    return _drift(service["payload"], current, SERVICE_PATHS)


def provider_desired(provider: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    if provider.get("update_payload"):
        mask = tuple(provider.get("update_mask", []))
        return provider["update_payload"], tuple(p for p in PROVIDER_PATHS if p in mask)
    return provider.get("payload", {}), PROVIDER_PATHS


def provider_drift(provider: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Return {mask_path: desired_value} for declared provider target fields."""
    desired, paths = provider_desired(provider)
    return _drift(desired, current, paths)


def body_for(changes: dict[str, Any]) -> dict[str, Any]:
    """Build a PATCH body containing only the changed mask paths."""
    body: dict[str, Any] = {}
    for path, value in changes.items():
        node = body
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return body


def reasoning_api_warnings(config: dict[str, Any]) -> list[str]:
    """Warn when a reasoning-model target cannot use the Responses API."""
    warnings: list[str] = []

    def check(where: str, target: dict[str, Any] | None) -> None:
        if not target or not _REASONING_MODEL.match(str(target.get("model", ""))):
            return
        types = target.get("native_api_types") or []
        if RESPONSES_API not in types:
            warnings.append(
                f"{where}: reasoning model {target.get('model')!r} lacks {RESPONSES_API}; "
                "function tools with reasoning_effort fail on chat/completions"
            )

    for provider in config.get("model_provider_services", []):
        desired, _ = provider_desired(provider)
        targets = _get(desired, "config.targets") or _get(
            provider.get("payload"), "config.targets"
        )
        for target in targets or []:
            check(f"provider {provider.get('name')}", target)
    for service in config.get("model_services", []):
        for dest in _get(service.get("payload"), "config.routing.destinations") or []:
            check(
                f"model service {service.get('name')}",
                (dest.get("external_model_config") or {}).get("target"),
            )
    return warnings
