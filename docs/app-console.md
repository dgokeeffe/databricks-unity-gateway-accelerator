# Operator console

The public FastAPI console is the first phase of the app work. It is a customer-configured companion to the CLI; the engineer request/approval workflow is deliberately deferred.

## Local demo

The default `app.yaml` sets `AIGW_APP_MODE=demo`. Demo mode uses synthetic values only and makes no Databricks API or SQL calls.

```bash
uv sync --extra app --extra dev
python app.py
# http://127.0.0.1:8000
```

The configuration workbench loads `config/example.yml`. Editing, validating, and planning in demo mode does not persist the YAML or make changes to a workspace.

## Live deployment

1. Create a workspace app service principal and grant it only the required workspace and account permissions. The app uses its service-principal credentials for platform APIs; do not place tokens or secrets in the YAML editor.
2. Add a SQL warehouse resource to the app with key `sql-warehouse` and `CAN_USE`; grant the app service principal `SELECT` on the required system tables.
3. Add a pre-created Unity Catalog Volume for operation audit records. Grant the app service principal `WRITE VOLUME` on it before enabling app-side apply.
4. Edit the root `app.yaml` for the target deployment. Configure:

   ```yaml
   env:
     - name: AIGW_APP_MODE
       value: live
     - name: ADMIN_EMAILS
       value: admin1@example.com,admin2@example.com
     - name: AIGW_ENABLE_APPLY
       value: "false"
     - name: DATABRICKS_WAREHOUSE_ID
       valueFrom: sql-warehouse
     - name: AIGW_AUDIT_VOLUME
       value: /Volumes/<catalog>/<schema>/<volume>
   ```

   Set `AIGW_APP_MODE=live` only after workspace resources and the admin allowlist are configured. Leave `AIGW_ENABLE_APPLY=false` until the app has been reviewed and the audit Volume has been verified. Set it to `true` only when app-side mutations are intended.
5. Deploy with the customer's Databricks CLI profile:

   ```bash
   databricks bundle validate -t dev
   databricks bundle deploy -t dev
   databricks bundle run governance_console -t dev
   ```

   `prod` is a separate target. Review and configure the app in each target workspace; do not carry workspace IDs, account IDs, emails, or hostnames from this sample into customer config.

Live plan, verify, spend, and apply routes require the authenticated email in `ADMIN_EMAILS`. Ensure the app deployment forwards the authenticated `X-Forwarded-Email` header. Workspace app access control is an additional perimeter, not a replacement for the server-side allowlist.

## Data and safety model

- The app sends YAML to the existing validator and reconciler in memory. It does not save customer config, credentials, or request/approval state.
- `plan` reads remote state and performs no writes. `verify` compares declared and actual state.
- App-side `apply` is disabled by default. When enabled, it requires an administrator, the literal confirmation `APPLY`, and writes started/completed (or failed) audit JSON files to the configured UC Volume. Budget updates are separately opt-in in the UI because they replace alert configuration.
- The daily tier policy is customer-configured in `daily_raise`. Eligibility requires the monthly cap not to have been reached, known spend/cap values, and daily remaining capacity below the configured trigger (default `$100`). The tier-selection helper uses the first configured limit at or above `daily spend + monthly cap / divisor` (default divisor `15`); the group-move helper uses account SCIM and adds the target group before removing older tier groups. It only touches the configured tier groups, never the user's other group memberships.

Tier groups must be Databricks **account-level groups** that the app service principal is allowed to manage—not workspace-local groups. This helper calls the account SCIM API directly; it does not call a separate custom AIM endpoint. If your AIM APIs expose/manage this same SCIM group-membership surface and authorize these writes, that is the intended path. For IdP-owned groups, AIM/SCIM provisioning may reject the write or restore the IdP's membership on its next sync; use Databricks-managed account groups for the tier ladder, or route the change through the owning identity provider. The Overview page shows an illustrative tier transition in demo mode. Declare the runaway budget under `budgets` with `role: runaway`; `apply --apply-budgets` creates it and maps each tier group to its override. The **Tiers** view lets admins preview and apply monthly/runaway tier moves and the month-start runaway reset (live mode, `AIGW_ENABLE_APPLY=true`, typed confirmation, UC Volume audit). The same operations are available as `aigw-accelerator tier-preview|tier-move|tier-reset`. The user-facing raise/request flow is Phase 2.
- Daily and month-to-date spend is estimated from documented system tables. Databricks-hosted model cost uses `system.billing.usage` joined to `system.billing.list_prices`; external-model spend uses `system.ai_gateway.external_model_spend`. The source tables have different update cadences and are not equivalent to the native near-real-time budget counters. Never use these estimates as a hard-stop signal.
- The original internal console's live budget-status RPC, internal group names, employee snapshots, org hierarchy, and company-specific policy numbers are not part of this public app.

## Configuration

`daily_raise` in `config/example.yml` is disabled by default. When enabled it requires a `role: runaway` budget whose overrides match the tiers exactly. `runaway_budget.enabled` (a native daily period) remains rejected.
