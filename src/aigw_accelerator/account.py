from __future__ import annotations

from typing import Any


class AccountAutomation:
    """Account-level budget and group operations.

    Imports Databricks SDK types lazily so plan/diff tests do not require credentials.
    """

    def __init__(self, account_client: Any):
        self.client = account_client

    def list_budgets(self) -> list[Any]:
        return list(self.client.budgets.list())

    def get_budget(self, budget_id: str) -> Any:
        return self.client.budgets.get(budget_id).budget

    def list_groups(self) -> list[dict[str, Any]]:
        path = f"/api/2.0/accounts/{self.client.config.account_id}/scim/v2/Groups"
        response = self.client.api_client.do(
            "GET", path, headers={"Accept": "application/scim+json"}
        )
        return response.get("Resources", [])

    def ensure_group(self, name: str, description: str = "") -> dict[str, Any]:
        matches = [g for g in self.list_groups() if g.get("displayName") == name]
        if len(matches) > 1:
            raise ValueError(f"Multiple account groups match {name!r}")
        if matches:
            return matches[0]
        path = f"/api/2.0/accounts/{self.client.config.account_id}/scim/v2/Groups"
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": name,
        }
        if description:
            body["urn:ietf:params:scim:schemas:extension:workspace:2.0:Group"] = {
                "description": description
            }
        return self.client.api_client.do(
            "POST", path, body=body, headers={"Accept": "application/scim+json"}
        )

    def principal_ids(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for group in self.list_groups():
            if group.get("displayName") and group.get("id"):
                result[group["displayName"]] = int(group["id"])
        return result

    def validate_workspace_identity(self, workspace_id: int, workspace_host: str) -> None:
        if not hasattr(self.client, "workspaces"):
            raise RuntimeError("Account SDK does not expose workspace identity discovery")
        matches = [
            item
            for item in self.client.workspaces.list()
            if int(getattr(item, "workspace_id", -1)) == int(workspace_id)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected one account workspace for id {workspace_id}, found {len(matches)}"
            )
        actual_host = getattr(matches[0], "workspace_url", None)
        if not actual_host:
            raise ValueError(
                f"account metadata has no workspace URL for {workspace_id}; "
                "refusing to validate an unbound workspace host"
            )
        if actual_host.rstrip("/") != workspace_host.rstrip("/"):
            raise ValueError(
                f"workspace host does not match account metadata for {workspace_id}: "
                f"{workspace_host} != {actual_host}"
            )

    def apply_budget(self, budget_id: str, payload: Any) -> Any:
        # The SDK update is a full replacement. The caller must fetch and merge
        # the existing object before calling this method.
        return self.client.budgets.update(budget_id, payload)


def make_account_client(account: dict[str, Any]) -> Any:
    from databricks.sdk import AccountClient

    client = AccountClient(
        host=account["host"],
        account_id=account["id"],
    )
    if not hasattr(client, "budgets"):
        raise RuntimeError(
            "The installed databricks-sdk does not expose AccountClient.budgets; "
            "install a current SDK release with the Account Budgets API."
        )
    return client
