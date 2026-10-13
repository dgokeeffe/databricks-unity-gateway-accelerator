from aigw_accelerator.config import ConfigError, validate_config
from aigw_accelerator.reconcile import format_plan, plan_budget, plan_groups, plan_model_services


def test_new_group_is_planned():
    plan = plan_groups(
        [{"name": "aigw-budget-pilot", "description": "pilot"}],
        [],
    )
    assert "CREATE" in format_plan(plan)
    assert "group:aigw-budget-pilot" in format_plan(plan)


def test_budget_changes_are_non_destructive_plan_items():
    plan = plan_budget(
        {"display_name": "existing"},
        {
            "id": "budget-1",
            "display_name": "existing",
            "workspace_ids": [123],
            "shared_thresholds": [{"amount_usd": "1000"}],
        },
    )
    rendered = format_plan(plan)
    assert "workspace scope" in rendered
    assert "shared thresholds" in rendered
    assert all(not change.destructive for change in plan.changes)


def test_model_service_plan_detects_missing_service():
    plan = plan_model_services(
        [{"catalog": "main", "schema": "ai", "name": "standard"}],
        [],
    )
    assert "model_service:main.ai.standard" in format_plan(plan)


def test_runaway_budget_is_rejected_until_capability_is_confirmed():
    config = {
        "account": {"id": "account", "host": "https://accounts.azuredatabricks.net"},
        "runaway_budget": {"enabled": True},
    }
    try:
        validate_config(config)
    except ConfigError as exc:
        assert "role: runaway" in str(exc)
    else:
        raise AssertionError("native daily runaway_budget should be rejected")
