from __future__ import annotations

from typing import Any
from urllib.parse import quote


class WorkspaceAutomation:
    """Workspace-level adapters for Gateway services, grants, and service tags."""

    def __init__(self, workspace_client: Any, workspace_id: int):
        self.client = workspace_client
        self.workspace_id = workspace_id

    def _headers(self) -> dict[str, str]:
        return {"x-databricks-workspace-id": str(self.workspace_id)}

    def _list(self, collection: str, catalog: str, schema: str) -> list[dict[str, Any]]:
        """List a GA AI Gateway collection, paginating and normalising names.

        The API returns ``{collection}/{catalog}.{schema}.{name}``; callers compare
        against the bare three-part name, so the prefix is stripped here.
        """
        items: list[dict[str, Any]] = []
        query: dict[str, Any] = {"parent": f"schemas/{catalog}.{schema}", "page_size": 200}
        while True:
            response = self.client.api_client.do(
                "GET",
                f"/api/2.1/unity-catalog/{collection}",
                query=query,
                headers={"Accept": "application/json", **self._headers()},
            )
            for item in response.get(collection.replace("-", "_"), []):
                name = item.get("name", "")
                if name.startswith(f"{collection}/"):
                    item = {**item, "name": name[len(collection) + 1 :]}
                items.append(item)
            token = response.get("next_page_token")
            if not token:
                return items
            query = {**query, "page_token": token}

    def _create(self, collection: str, id_param: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        catalog = body.pop("catalog_name")
        schema = body.pop("schema_name")
        service_id = body.pop("name")
        return self.client.api_client.do(
            "POST",
            f"/api/2.1/unity-catalog/{collection}",
            query={"parent": f"schemas/{catalog}.{schema}", id_param: service_id},
            body=body,
            headers={"Content-Type": "application/json", **self._headers()},
        )

    def list_model_services(self, catalog: str, schema: str) -> list[dict[str, Any]]:
        return self._list("model-services", catalog, schema)

    def get_model_service(self, qualified_name: str) -> dict[str, Any]:
        if any(part in qualified_name for part in ("/", "\\", "..")):
            raise ValueError(f"Invalid model service name: {qualified_name!r}")
        resource = f"model-services/{quote(qualified_name, safe='.') }"
        return self.client.api_client.do(
            "GET",
            f"/api/2.1/unity-catalog/{resource}",
            headers={"Accept": "application/json", **self._headers()},
        )

    def create_model_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._create("model-services", "model_service_id", payload)

    def list_model_provider_services(self, catalog: str, schema: str) -> list[dict[str, Any]]:
        return self._list("model-provider-services", catalog, schema)

    def get_model_provider_service(self, qualified_name: str) -> dict[str, Any]:
        if any(part in qualified_name for part in ("/", "\\", "..")):
            raise ValueError(f"Invalid model provider service name: {qualified_name!r}")
        resource = f"model-provider-services/{quote(qualified_name, safe='.') }"
        return self.client.api_client.do(
            "GET",
            f"/api/2.1/unity-catalog/{resource}",
            headers={"Accept": "application/json", **self._headers()},
        )

    def create_model_provider_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._create("model-provider-services", "model_provider_service_id", payload)

    def update_model_service(
        self, qualified_name: str, model_service: dict[str, Any], update_mask: list[str]
    ) -> dict[str, Any]:
        # The API requires explicit update paths; wildcard updates are not supported.
        if any(part in qualified_name for part in ("/", "\\", "..")):
            raise ValueError(f"Invalid model service name: {qualified_name!r}")
        resource = f"model-services/{quote(qualified_name, safe='.') }"
        # GA API: resource object is the body; update_mask is a query parameter.
        return self.client.api_client.do(
            "PATCH",
            f"/api/2.1/unity-catalog/{resource}",
            query={"update_mask": ",".join(update_mask)},
            body=model_service,
            headers={"Content-Type": "application/json", **self._headers()},
        )

    def get_grants(self, securable_type: str, full_name: str) -> dict[str, Any]:
        if securable_type not in {"model_service", "model_provider_service"}:
            raise ValueError(f"Unsupported securable_type: {securable_type!r}")
        if any(part in full_name for part in ("/", "\\", "..")):
            raise ValueError(f"Invalid securable name: {full_name!r}")
        path = (
            f"/api/2.1/unity-catalog/permissions/"
            f"{securable_type}/{quote(full_name, safe='.') }"
        )
        return self.client.api_client.do(
            "GET",
            path,
            headers={"Accept": "application/json", **self._headers()},
        )

    def update_model_provider_service(
        self, qualified_name: str, provider_service: dict[str, Any], update_mask: list[str]
    ) -> dict[str, Any]:
        if any(part in qualified_name for part in ("/", "\\", "..")):
            raise ValueError(f"Invalid model provider service name: {qualified_name!r}")
        resource = f"model-provider-services/{quote(qualified_name, safe='.') }"
        return self.client.api_client.do(
            "PATCH",
            f"/api/2.1/unity-catalog/{resource}",
            query={"update_mask": ",".join(update_mask)},
            body=provider_service,
            headers={"Content-Type": "application/json", **self._headers()},
        )

    def update_grants(
        self,
        securable_type: str,
        full_name: str,
        grants: list[dict[str, Any]],
        *,
        revoke: bool = False,
    ) -> dict[str, Any]:
        changes = []
        for grant in grants:
            principal = grant["principal"]
            privileges = grant.get("privileges", [])
            changes.append(
                {
                    "principal": principal,
                    "add": [] if revoke else privileges,
                    "remove": privileges if revoke else [],
                }
            )
        if securable_type not in {"model_service", "model_provider_service"}:
            raise ValueError(f"Unsupported securable_type: {securable_type!r}")
        if any(part in full_name for part in ("/", "\\", "..")):
            raise ValueError(f"Invalid securable name: {full_name!r}")
        path = (
            f"/api/2.1/unity-catalog/permissions/"
            f"{securable_type}/{quote(full_name, safe='.') }"
        )
        return self.client.api_client.do(
            "PATCH",
            path,
            body={"changes": changes, "omit_permissions_in_response": False},
            headers={"Content-Type": "application/json", **self._headers()},
        )


def make_workspace_client(host: str, profile: str | None = None) -> Any:
    from databricks.sdk import WorkspaceClient

    if profile:
        return WorkspaceClient(host=host, profile=profile)
    return WorkspaceClient(host=host)
