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
        # SCIM pages results (default 100); fetch every page so name lookups
        # never miss an existing group and create a duplicate.
        path = f"/api/2.1/accounts/{self.client.config.account_id}/scim/v2/Groups"
        groups: list[dict[str, Any]] = []
        start = 1
        while True:
            response = self.client.api_client.do(
                "GET",
                path,
                query={"startIndex": start, "count": 100, "attributes": "id,displayName"},
                headers={"Accept": "application/scim+json"},
            )
            page = response.get("Resources", []) or []
            groups.extend(page)
            total = int(response.get("totalResults", len(groups)))
            if not page or len(groups) >= total:
                return groups
            start += len(page)

    def ensure_group(self, name: str, description: str = "") -> dict[str, Any]:
        matches = [g for g in self.list_groups() if g.get("displayName") == name]
        if len(matches) > 1:
            raise ValueError(f"Multiple account groups match {name!r}")
        if matches:
            return matches[0]
        path = f"/api/2.1/accounts/{self.client.config.account_id}/scim/v2/Groups"
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

    def groups_by_name(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for group in self.list_groups():
            name = group.get("displayName")
            if not name:
                continue
            if name in result:
                raise ValueError(f"Multiple account groups match {name!r}")
            result[name] = group
        return result

    def get_group_by_name(self, name: str) -> dict[str, Any] | None:
        return self.groups_by_name().get(name)

    def find_user_id(self, email: str) -> str | None:
        """Resolve an account user's SCIM id from their userName (email)."""
        value = str(email).strip()
        if not value or "@" not in value or any(ch in value for ch in '"\\'):
            raise ValueError(f"invalid user email: {email!r}")
        path = f"/api/2.1/accounts/{self.client.config.account_id}/scim/v2/Users"
        response = self.client.api_client.do(
            "GET",
            path,
            query={"filter": f'userName eq "{value}"', "attributes": "id,userName"},
            headers={"Accept": "application/scim+json"},
        )
        matches = response.get("Resources", []) or []
        if len(matches) > 1:
            raise ValueError(f"Multiple account users match {value!r}")
        return str(matches[0]["id"]) if matches else None

    def group_member_ids(self, group_id: str) -> list[str]:
        return [
            str(member.get("value"))
            for member in self._get_group(group_id).get("members", []) or []
            if member.get("value") is not None
        ]

    def group_has_member(self, group_id: str, user_id: str) -> bool:
        group = self._get_group(group_id)
        return any(str(member.get("value")) == str(user_id) for member in group.get("members", []))

    def _get_group(self, group_id: str) -> dict[str, Any]:
        if not group_id or not all(ch.isalnum() or ch in "-_" for ch in group_id):
            raise ValueError("SCIM group ID may contain only letters, digits, '-' and '_'")
        path = f"/api/2.1/accounts/{self.client.config.account_id}/scim/v2/Groups/{group_id}"
        return self.client.api_client.do(
            "GET", path, headers={"Accept": "application/scim+json"}
        )

    def patch_group_member(self, group_id: str, user_id: str, *, add: bool) -> None:
        """Add or remove one member from an account SCIM group."""
        def safe_id(value: str) -> bool:
            return bool(value) and all(ch.isalnum() or ch in "-_" for ch in value)

        if not safe_id(group_id) or not safe_id(user_id):
            raise ValueError(
                "SCIM group and user IDs may contain only letters, digits, '-' and '_'"
            )
        account_id = self.client.config.account_id
        path = f"/api/2.1/accounts/{account_id}/scim/v2/Groups/{group_id}"
        operation = {"op": "add", "value": {"members": [{"value": user_id}]}}
        if not add:
            operation = {
                "op": "remove",
                "path": f'members[value eq "{user_id}"]',
            }
        self.client.api_client.do(
            "PATCH",
            path,
            body={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [operation],
            },
            headers={"Content-Type": "application/scim+json", "Accept": "application/scim+json"},
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
        deployment = getattr(matches[0], "deployment_name", None)
        if not actual_host and deployment and "azure" in str(getattr(matches[0], "cloud", "")):
            # Azure account metadata omits workspace_url; the canonical host is
            # derived from deployment_name. Exact match is still required below.
            actual_host = f"https://{deployment}.azuredatabricks.net"
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
        **({"profile": account["profile"]} if account.get("profile") else {}),
    )
    if not hasattr(client, "budgets"):
        raise RuntimeError(
            "The installed databricks-sdk does not expose AccountClient.budgets; "
            "install a current SDK release with the Account Budgets API."
        )
    return client
