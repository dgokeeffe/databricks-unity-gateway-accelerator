from aigw_accelerator.usage import SPEND_ESTIMATES_SQL


def test_public_spend_estimate_uses_documented_system_tables():
    assert "system.billing.usage" in SPEND_ESTIMATES_SQL
    assert "system.billing.list_prices" in SPEND_ESTIMATES_SQL
    assert "system.ai_gateway.external_model_spend" in SPEND_ESTIMATES_SQL
    assert "usersBudgetSpendStatus" not in SPEND_ESTIMATES_SQL
