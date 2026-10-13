from pathlib import Path

from fastapi.testclient import TestClient

from aigw_accelerator.webapp import app

client = TestClient(app)
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example.yml"


def test_app_serves_console_and_static_assets():
    assert client.get("/").status_code == 200
    assert "Spend with" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_demo_mode_status_and_data_are_explicitly_synthetic(monkeypatch):
    monkeypatch.setenv("AIGW_APP_MODE", "demo")
    status = client.get("/api/status").json()
    demo = client.get("/api/demo-data").json()

    assert status["mode"] == "demo"
    assert status["daily_spend_is_enforcement_signal"] is False
    assert "synthetic" in demo["note"]
    assert demo["tier_move_example"] == {
        "current_group": "daily-tier-125",
        "target_group": "daily-tier-150",
    }


def test_live_plan_requires_server_side_admin_allowlist(monkeypatch):
    monkeypatch.setenv("AIGW_APP_MODE", "live")
    monkeypatch.setenv("ADMIN_EMAILS", "operator@example.com")
    response = client.post(
        "/api/plan", json={"config_yaml": EXAMPLE.read_text(encoding="utf-8")}
    )
    assert response.status_code == 403


def test_live_plan_rejects_unresolved_example_placeholders(monkeypatch):
    monkeypatch.setenv("AIGW_APP_MODE", "live")
    monkeypatch.setenv("ADMIN_EMAILS", "operator@example.com")
    response = client.post(
        "/api/plan",
        json={"config_yaml": EXAMPLE.read_text(encoding="utf-8")},
        headers={"X-Forwarded-Email": "operator@example.com"},
    )
    assert response.status_code == 422
    assert "Replace the example placeholder" in response.json()["detail"]


def test_demo_plan_does_not_call_remote_databricks_state(monkeypatch):
    monkeypatch.setenv("AIGW_APP_MODE", "demo")
    response = client.post(
        "/api/plan", json={"config_yaml": EXAMPLE.read_text(encoding="utf-8")}
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "demo"
    assert "no remote state was read" in response.json()["plan"]


def test_demo_apply_is_blocked(monkeypatch):
    monkeypatch.setenv("AIGW_APP_MODE", "demo")
    response = client.post(
        "/api/apply",
        json={
            "config_yaml": EXAMPLE.read_text(encoding="utf-8"),
            "confirmation": "APPLY",
            "apply_budgets": True,
        },
    )

    assert response.status_code == 409
    assert "demo mode" in response.json()["detail"]


def test_config_validation_does_not_require_live_credentials(monkeypatch):
    monkeypatch.setenv("AIGW_APP_MODE", "demo")
    response = client.post(
        "/api/config/validate",
        json={"config_yaml": EXAMPLE.read_text(encoding="utf-8")},
    )

    assert response.status_code == 200
    assert response.json()["counts"]["daily_tiers"] == 2

def test_tier_endpoints_are_live_admin_only_and_writes_need_confirmation(monkeypatch):
    body = {"config_yaml": EXAMPLE.read_text(encoding="utf-8"), "email": "u@example.com"}
    monkeypatch.setenv("AIGW_APP_MODE", "demo")
    assert client.post("/api/tiers/preview", json=body).status_code == 409
    monkeypatch.setenv("AIGW_APP_MODE", "live")
    monkeypatch.setenv("ADMIN_EMAILS", "operator@example.com")
    assert client.post("/api/tiers/preview", json=body).status_code == 403
    admin = {"X-Forwarded-Email": "operator@example.com"}
    monkeypatch.delenv("AIGW_ENABLE_APPLY", raising=False)
    move = client.post("/api/tiers/move", json={**body, "confirmation": "MOVE"}, headers=admin)
    assert move.status_code == 403
    monkeypatch.setenv("AIGW_ENABLE_APPLY", "true")
    wrong = client.post("/api/tiers/move", json={**body, "confirmation": "nope"}, headers=admin)
    assert wrong.status_code == 400
