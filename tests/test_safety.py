import pytest

from aigw_accelerator import cli
from aigw_accelerator.cli import _budget_payload
from aigw_accelerator.config import ConfigError, validate_config
from aigw_accelerator.workspace import WorkspaceAutomation


def test_apply_config_emits_started_and_completed_audit_records(monkeypatch):
    monkeypatch.setattr(cli, "_discover", lambda config: (object(), [], []))
    monkeypatch.setattr(cli, "_ensure_groups", lambda account, config: {})
    monkeypatch.setattr(cli, "_apply_services_and_grants", lambda config: None)
    audit = []

    def writer(config, **kwargs):
        audit.append(kwargs)
        return kwargs["status"]

    started, completed = cli.apply_config({}, audit_writer=writer)
    assert (started, completed) == ("started", "succeeded")
    assert [item["status"] for item in audit] == ["started", "succeeded"]


def test_apply_config_writes_failure_audit_when_mutation_raises(monkeypatch):
    monkeypatch.setattr(cli, "_discover", lambda config: (object(), [], []))
    monkeypatch.setattr(cli, "_ensure_groups", lambda account, config: {})

    def fail(config):
        raise RuntimeError("test failure")

    monkeypatch.setattr(cli, "_apply_services_and_grants", fail)
    audit = []

    def writer(config, **kwargs):
        audit.append(kwargs)
        return kwargs["status"]

    with pytest.raises(RuntimeError, match="test failure"):
        cli.apply_config({}, audit_writer=writer)
    assert [item["status"] for item in audit] == ["started", "failed"]
    assert audit[-1]["error"] == "RuntimeError"


def test_apply_config_reports_completion_audit_failure_without_suggesting_retry(monkeypatch):
    monkeypatch.setattr(cli, "_discover", lambda config: (object(), [], []))
    monkeypatch.setattr(cli, "_ensure_groups", lambda account, config: {})
    monkeypatch.setattr(cli, "_apply_services_and_grants", lambda config: None)

    def writer(config, **kwargs):
        if kwargs["status"] == "succeeded":
            raise OSError("volume unavailable")
        return kwargs["status"]

    with pytest.raises(RuntimeError, match="apply completed but the completion audit"):
        cli.apply_config({}, audit_writer=writer)


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


def test_daily_raise_policy_requires_tiers_and_declared_account_groups():
    config = {
        "account": {"id": "account", "host": "https://accounts.azuredatabricks.net"},
        "daily_raise": {
            "enabled": True,
            "monthly_to_daily_divisor": 15,
            "tiers": [{"group": "daily-100", "limit_usd": "100"}],
        },
    }
    with pytest.raises(ConfigError, match="declared under groups"):
        validate_config(config)

    config["groups"] = [{"name": "daily-100", "ensure": False}]
    with pytest.raises(ConfigError, match="role: runaway"):
        validate_config(config)

    config["budgets"] = [
        {
            "id": "r",
            "role": "runaway",
            "resource_type": "BUDGET_RESOURCE_TYPE_UNITY_AI_GATEWAY",
            "per_user_threshold": {"amount_usd": "50"},
            "overrides": [{"group": "daily-100", "amount_usd": "100"}],
        }
    ]
    validate_config(config)


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


def test_account_group_member_patch_uses_safe_scim_resource_and_payload():
    from aigw_accelerator.account import AccountAutomation

    class FakeAPI:
        def __init__(self):
            self.calls = []

        def do(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {}

    class FakeClient:
        def __init__(self):
            self.api_client = FakeAPI()
            self.config = type("Config", (), {"account_id": "acct-1"})()

    fake = FakeClient()
    account = AccountAutomation(fake)
    account.patch_group_member("group-1", "user-2", add=True)
    method, path, kwargs = fake.api_client.calls[0]
    assert (method, path) == (
        "PATCH",
        "/api/2.1/accounts/acct-1/scim/v2/Groups/group-1",
    )
    assert kwargs["body"]["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:PatchOp"]
    assert kwargs["body"]["Operations"][0]["value"] == {
        "members": [{"value": "user-2"}]
    }
    with pytest.raises(ValueError, match="SCIM group and user IDs"):
        account.patch_group_member("../../groups", "user-2", add=True)


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


def test_alert_key_ignores_trailing_decimal_zeros():
    from aigw_accelerator.cli import _alert_key

    a = {"quantity_threshold": "1000", "principal_overrides": [{"principal_id": 1, "override_threshold": "300"}]}
    b = {"quantity_threshold": "1000.000000000000000000", "principal_overrides": [{"principal_id": 1, "override_threshold": "300.00"}]}
    assert _alert_key(a) == _alert_key(b)


def test_list_groups_reads_every_scim_page():
    from types import SimpleNamespace

    from aigw_accelerator.account import AccountAutomation

    pages = {
        1: {"totalResults": 3, "Resources": [{"id": "1", "displayName": "a"}, {"id": "2", "displayName": "b"}]},
        3: {"totalResults": 3, "Resources": [{"id": "3", "displayName": "c"}]},
    }
    api = SimpleNamespace(do=lambda method, path, query=None, headers=None: pages[query["startIndex"]])
    client = SimpleNamespace(config=SimpleNamespace(account_id="acct"), api_client=api)
    assert [g["displayName"] for g in AccountAutomation(client).list_groups()] == ["a", "b", "c"]


def test_inference_table_enabled_additively_and_immutable():
    import pytest

    from aigw_accelerator import cli
    from aigw_accelerator.config import ConfigError

    class Fake:
        def __init__(self, table):
            self.table, self.calls = table, []

        def get_model_service(self, name):
            return {"config": {"inference_table": self.table} if self.table else {}}

        def update_model_service(self, name, body, mask):
            self.calls.append((body, mask))

    svc = {"payload": {"config": {"inference_table": {"parent": "schemas/c.s"}}}}
    fake = Fake(None)
    cli._ensure_inference_table(fake, "c.s.n", svc)
    assert fake.calls[0][1] == ["config.inference_table"]
    assert fake.calls[0][0]["config"] == {"inference_table": {"parent": "schemas/c.s"}}

    same = Fake({"parent": "schemas/c.s", "table": "tables/c.s.n_payload"})
    cli._ensure_inference_table(same, "c.s.n", svc)
    assert same.calls == []

    with pytest.raises(ConfigError, match="immutable"):
        cli._ensure_inference_table(Fake({"parent": "schemas/x.y"}), "c.s.n", svc)


def _runaway_config(**overrides):
    base = {
        "account": {"id": "a", "host": "https://accounts.azuredatabricks.net"},
        "groups": [{"name": "r-100"}, {"name": "r-200"}],
        "daily_raise": {
            "enabled": True,
            "raise_trigger_remaining_usd": 10,
            "tiers": [{"group": "r-100", "limit_usd": "100"}, {"group": "r-200", "limit_usd": "200"}],
        },
        "budgets": [
            {"id": "m", "role": "monthly", "resource_type": "BUDGET_RESOURCE_TYPE_UNITY_AI_GATEWAY",
             "workspace_ids": [1], "per_user_threshold": {"amount_usd": "100"}},
            {"id": "r", "role": "runaway", "resource_type": "BUDGET_RESOURCE_TYPE_UNITY_AI_GATEWAY",
             "workspace_ids": [1], "per_user_threshold": {"amount_usd": "50"},
             "overrides": [{"group": "r-100", "amount_usd": "100"}, {"group": "r-200", "amount_usd": "200"}]},
        ],
    }
    base.update(overrides)
    return base


def test_runaway_budget_pair_validates():
    from aigw_accelerator.config import validate_config

    validate_config(_runaway_config())


def test_runaway_budget_rules_fail_closed():
    import copy

    import pytest

    from aigw_accelerator.config import ConfigError, validate_config

    cfg = _runaway_config()
    cfg["budgets"] = cfg["budgets"][:1]
    with pytest.raises(ConfigError, match="role: runaway"):
        validate_config(cfg)

    cfg = _runaway_config()
    cfg["budgets"][1]["workspace_ids"] = [2]
    with pytest.raises(ConfigError, match="same workspace_ids"):
        validate_config(cfg)

    cfg = _runaway_config()
    cfg["daily_raise"]["tiers"][1]["limit_usd"] = "250"
    with pytest.raises(ConfigError, match="match the runaway budget overrides"):
        validate_config(cfg)

    cfg = _runaway_config()
    cfg["budgets"][1]["per_user_threshold"]["amount_usd"] = "150"
    with pytest.raises(ConfigError, match="above the base"):
        validate_config(copy.deepcopy(cfg))
