import pytest

from aigw_accelerator.config import ConfigError, validate_config
from aigw_accelerator.workspace import WorkspaceAutomation


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def do(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses.pop(0) if self.responses else {}


class FakeClient:
    def __init__(self, responses=()):
        self.api_client = FakeApi(responses)


def test_list_provider_services_uses_ga_parent_paginates_and_strips_prefix():
    client = FakeClient(
        [
            {
                "model_provider_services": [
                    {"name": "model-provider-services/cat.ai.foundry"}
                ],
                "next_page_token": "t1",
            },
            {"model_provider_services": [{"name": "model-provider-services/cat.ai.other"}]},
        ]
    )
    items = WorkspaceAutomation(client, 1).list_model_provider_services("cat", "ai")
    assert [item["name"] for item in items] == ["cat.ai.foundry", "cat.ai.other"]
    method, path, kwargs = client.api_client.calls[0]
    assert (method, path) == ("GET", "/api/2.1/unity-catalog/model-provider-services")
    assert kwargs["query"]["parent"] == "schemas/cat.ai"
    assert client.api_client.calls[1][2]["query"]["page_token"] == "t1"


@pytest.mark.parametrize(
    ("method_name", "path", "id_param"),
    [
        ("create_model_service", "/api/2.1/unity-catalog/model-services", "model_service_id"),
        (
            "create_model_provider_service",
            "/api/2.1/unity-catalog/model-provider-services",
            "model_provider_service_id",
        ),
    ],
)
def test_create_moves_identity_to_query_params(method_name, path, id_param):
    client = FakeClient()
    payload = {"catalog_name": "cat", "schema_name": "ai", "name": "svc", "config": {"x": 1}}
    getattr(WorkspaceAutomation(client, 1), method_name)(payload)
    _, called_path, kwargs = client.api_client.calls[0]
    assert called_path == path
    assert kwargs["query"] == {"parent": "schemas/cat.ai", id_param: "svc"}
    assert kwargs["body"] == {"config": {"x": 1}}
    assert "name" in payload  # caller payload is not mutated


def test_provider_payload_rejects_inline_plaintext_credentials():
    config = {
        "account": {"id": "acct", "host": "https://accounts.azuredatabricks.net"},
        "model_provider_services": [
            {
                "workspace_host": "https://adb-1.1.azuredatabricks.net",
                "workspace_id": 1,
                "catalog": "cat",
                "schema": "ai",
                "name": "foundry",
                "payload": {
                    "config": {
                        "microsoft_foundry": {
                            "direct": {"api_key": {"plaintext": "not-a-real-key"}}
                        }
                    }
                },
            }
        ]
    }
    with pytest.raises(ConfigError, match="plaintext"):
        validate_config(config)


def test_azure_workspace_identity_falls_back_to_deployment_name():
    from types import SimpleNamespace

    from aigw_accelerator.account import AccountAutomation

    ws = SimpleNamespace(
        workspace_id=42, workspace_url=None, deployment_name="adb-42.1", cloud="azure"
    )
    client = SimpleNamespace(workspaces=SimpleNamespace(list=lambda: [ws]))
    account = AccountAutomation.__new__(AccountAutomation)
    account.client = client
    account.validate_workspace_identity(42, "https://adb-42.1.azuredatabricks.net/")
    with pytest.raises(ValueError, match="does not match"):
        account.validate_workspace_identity(42, "https://vanity.azuredatabricks.net")


@pytest.mark.parametrize(
    ("method_name", "prefix"),
    [
        ("update_model_service", "model-services"),
        ("update_model_provider_service", "model-provider-services"),
    ],
)
def test_update_sends_unwrapped_body_and_mask_query(method_name, prefix):
    client = FakeClient()
    getattr(WorkspaceAutomation(client, 1), method_name)(
        "cat.ai.svc", {"config": {"targets": []}}, ["config.targets"]
    )
    method, path, kwargs = client.api_client.calls[0]
    assert (method, path) == ("PATCH", f"/api/2.1/unity-catalog/{prefix}/cat.ai.svc")
    assert kwargs["query"] == {"update_mask": "config.targets"}
    assert kwargs["body"] == {"config": {"targets": []}}
