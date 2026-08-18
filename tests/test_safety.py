import pytest

from aigw_accelerator.cli import _budget_payload
from aigw_accelerator.config import ConfigError, validate_config
from aigw_accelerator.workspace import WorkspaceAutomation


def test_budget_update_preserves_existing_alerts_by_default():
    current = {
        "filter": {"workspace_id": {"operator": "IN", "values": [123]}},
        "alert_configurations": [
            {
                "alert_configuration_id": "shared-1",
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                "quantity_threshold": "1000",
                "action_configurations": [{"action_type": "BLOCK_USAGE"}],
            },
            {
                "alert_configuration_id": "user-1",
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "quantity_threshold": "500",
                "action_configurations": [{"action_type": "BLOCK_USAGE"}],
            },
        ],
    }
    desired = {
        "shared_thresholds": [{"amount_usd": "1000", "block": True}],
    }
    with pytest.raises(ConfigError, match="existing budget alerts"):
        _budget_payload(current, desired, {})


def test_budget_update_does_not_widen_scope_when_scope_is_omitted():
    current = {"filter": {"workspace_id": {"operator": "IN", "values": [123]}}}
    desired = {"shared_thresholds": []}
    payload = _budget_payload(current, desired, {})
    assert payload["filter"] == current["filter"]


def test_budget_update_rejects_partial_scope_removal():
    current = {
        "filter": {
            "workspace_id": {"operator": "IN", "values": [123]},
            "tags": [{"key": "environment", "value": {"operator": "IN", "values": ["prod"]}}],
        }
    }
    with pytest.raises(ConfigError, match="scope clauses"):
        _budget_payload(current, {"workspace_ids": [123]}, {})


def test_budget_update_rejects_duplicate_alert_identity_with_different_actions():
    current = {
        "alert_configurations": [
            {
                "alert_configuration_id": "a",
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                "quantity_threshold": "1000.00",
                "quantity_type": "LIST_PRICE_DOLLARS_USD",
                "time_period": "MONTH",
                "trigger_type": "CUMULATIVE_SPENDING_EXCEEDED",
                "action_configurations": [{"action_type": "BLOCK_USAGE"}],
            },
            {
                "alert_configuration_id": "b",
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                "quantity_threshold": "1000",
                "quantity_type": "LIST_PRICE_DOLLARS_USD",
                "time_period": "MONTH",
                "trigger_type": "CUMULATIVE_SPENDING_EXCEEDED",
                "action_configurations": [
                    {"action_type": "EMAIL_NOTIFICATION", "target": "x@example.com"}
                ],
            },
        ]
    }
    with pytest.raises(ConfigError, match="existing budget alerts"):
        _budget_payload(current, {"shared_thresholds": [{"amount_usd": "1000", "block": True}]}, {})


def test_hosts_are_restricted_to_https_databricks_domains():
    base = {"account": {"id": "account", "host": "https://accounts.attacker.invalid"}}
    with pytest.raises(ConfigError, match="unsupported Databricks account host"):
        validate_config(base)

    base["account"]["host"] = "https://accounts.azuredatabricks.net"
    base["model_services"] = [
        {
            "workspace_host": "https://attacker.invalid",
            "workspace_id": 1,
            "catalog": "main",
            "schema": "ai",
            "name": "standard",
        }
    ]
    with pytest.raises(ConfigError, match="unsupported Databricks workspace host"):
        validate_config(base)


def test_governed_tag_assignment_is_explicitly_not_baseline_applied():
    config = {
        "account": {"id": "account", "host": "https://accounts.azuredatabricks.net"},
        "governed_tags": [
            {
                "key": "cost_tier",
                "assignments": [{"entity_type": "model_service"}],
            }
        ],
    }
    with pytest.raises(ConfigError, match="governed-tag assignments"):
        validate_config(config)


def test_grant_api_keeps_workspace_scope_and_rejects_path_traversal():
    class FakeAPI:
        def __init__(self):
            self.calls = []

        def do(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {}

    class FakeClient:
        def __init__(self):
            self.api_client = FakeAPI()

    fake = FakeClient()
    workspace = WorkspaceAutomation(fake, 123)
    workspace.update_grants(
        "model_service",
        "main.ai.standard",
        [{"principal": "aigw-model-users", "privileges": ["EXECUTE"]}],
    )
    _, _, kwargs = fake.api_client.calls[0]
    assert kwargs["headers"]["x-databricks-workspace-id"] == "123"
    with pytest.raises(ValueError, match="Invalid securable name"):
        workspace.update_grants("model_service", "main.ai/../../permissions", [])


def test_model_service_update_keeps_resource_separator_unencoded():
    class FakeAPI:
        def __init__(self):
            self.calls = []

        def do(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {}

    class FakeClient:
        def __init__(self):
            self.api_client = FakeAPI()

    fake = FakeClient()
    workspace = WorkspaceAutomation(fake, 456)
    workspace.update_model_service(
        "main.ai.standard",
        {"name": "model-services/main.ai.standard", "config": {}},
        ["config.endpoint_tags"],
    )
    _, path, _ = fake.api_client.calls[0]
    assert "/model-services/main.ai.standard" in path
    assert "%2F" not in path
