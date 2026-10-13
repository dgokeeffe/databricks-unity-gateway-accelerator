import pytest

from aigw_accelerator import cli
from aigw_accelerator.config import ConfigError
from aigw_accelerator.gateway import (
    body_for,
    provider_drift,
    reasoning_api_warnings,
    service_drift,
)

CHAT = "openai/v1/chat/completions"
RESP = "openai/v1/responses"
MPS = "model-provider-services/cat.ai.foundry"


def _service(types, *, allow=False, tokens=100000):
    return {
        "workspace_host": "https://adb-1.1.azuredatabricks.net",
        "workspace_id": 1,
        "catalog": "cat",
        "schema": "ai",
        "name": "svc",
        "allow_service_config_replacement": allow,
        "payload": {
            "config": {
                "routing": {
                    "destinations": [
                        {
                            "name": "primary",
                            "destination_type": "DESTINATION_TYPE_EXTERNAL_FOUNDATION_MODEL",
                            "external_model_config": {
                                "model_provider_service": MPS,
                                "target": {"model": "gpt-6.1-sol", "native_api_types": types},
                            },
                        }
                    ]
                },
                "rate_limits": [
                    {
                        "key": "RATE_LIMIT_KEY_USER_DEFAULT",
                        "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE",
                        "requests": 60,
                        "tokens": tokens,
                    }
                ],
            }
        },
    }


def _live(types):
    """Shape returned by GET: int64 as strings, prefixed name, extra fields."""
    return {
        "name": "model-services/cat.ai.svc",
        "etag": "x",
        "config": {
            "routing": {
                "destinations": [
                    {
                        "name": "primary",
                        "destination_type": "DESTINATION_TYPE_EXTERNAL_FOUNDATION_MODEL",
                        "external_model_config": {
                            "model_provider_service": MPS,
                            "target": {
                                "model": "gpt-6.1-sol",
                                "native_api_types": list(reversed(types)),
                            },
                        },
                    }
                ]
            },
            "rate_limits": [
                {
                    "key": "RATE_LIMIT_KEY_USER_DEFAULT",
                    "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE",
                    "requests": "60",
                    "tokens": "100000",
                }
            ],
        },
    }


def test_service_in_sync_ignores_string_ints_and_type_order():
    assert service_drift(_service([CHAT, RESP]), _live([CHAT, RESP])) == {}


def test_service_detects_routing_and_rate_limit_drift():
    drift = service_drift(_service([CHAT, RESP], tokens=20000), _live([CHAT]))
    assert set(drift) == {"config.routing.destinations", "config.rate_limits"}
    assert body_for(drift)["config"]["routing"]["destinations"][0]["name"] == "primary"


def test_legacy_shape_has_no_ga_drift():
    legacy = {"payload": {"rate_limits": {"qpm": 1}}}
    assert service_drift(legacy, _live([CHAT])) == {}


def test_provider_drift_respects_update_mask_and_target_order():
    provider = {
        "update_mask": ["config.targets"],
        "update_payload": {
            "config": {
                "targets": [
                    {"model": "b", "native_api_types": [CHAT]},
                    {"model": "a", "native_api_types": [RESP, CHAT]},
                ]
            }
        },
    }
    live = {
        "config": {
            "allow_all_targets": True,
            "targets": [
                {"model": "a", "native_api_types": [CHAT, RESP]},
                {"model": "b", "native_api_types": [CHAT]},
            ],
        }
    }
    assert provider_drift(provider, live) == {}
    live["config"]["targets"][0]["native_api_types"] = [CHAT]
    assert list(provider_drift(provider, live)) == ["config.targets"]


def test_reasoning_warning_only_when_responses_missing():
    config = {"model_services": [_service([CHAT])], "model_provider_services": []}
    assert len(reasoning_api_warnings(config)) == 1
    config["model_services"] = [_service([CHAT, RESP])]
    assert reasoning_api_warnings(config) == []


class _FakeWorkspace:
    def __init__(self, live):
        self.live = live
        self.updates = []

    def list_model_services(self, catalog, schema):
        return [{"name": "cat.ai.svc"}]

    def get_model_service(self, qualified):
        return self.live

    def update_model_service(self, qualified, body, mask):
        self.updates.append((qualified, body, mask))


def _run_apply(monkeypatch, service, live):
    fake = _FakeWorkspace(live)
    monkeypatch.setattr(cli, "make_workspace_client", lambda *a, **k: object())
    monkeypatch.setattr(cli, "WorkspaceAutomation", lambda *a, **k: fake)
    cli._apply_services_and_grants({"model_services": [service]})
    return fake.updates


def test_apply_refuses_ga_drift_without_opt_in(monkeypatch):
    with pytest.raises(ConfigError, match="allow_service_config_replacement"):
        _run_apply(monkeypatch, _service([CHAT, RESP]), _live([CHAT]))


def test_apply_patches_only_drifted_paths_with_opt_in(monkeypatch):
    updates = _run_apply(monkeypatch, _service([CHAT, RESP], allow=True), _live([CHAT]))
    assert [mask for _, _, mask in updates] == [["config.routing.destinations"]]


def test_apply_is_noop_when_in_sync(monkeypatch):
    assert _run_apply(monkeypatch, _service([CHAT, RESP]), _live([CHAT, RESP])) == []
