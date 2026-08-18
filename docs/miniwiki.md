# Customer Miniwiki: Unity AI Gateway Governance Accelerator

## Purpose

This accelerator provides a repeatable, reviewable path to configure Unity AI Gateway budgets, model services, service tags, grants, and governed-tag metadata where supported.

It is designed to be run from CI or a controlled operator workstation using an account-level service principal and workspace-level identities.

## What the accelerator manages

### Account scope

- Account groups used for budget tiers
- Unity AI Gateway budgets
- Workspace and tag filters
- Shared and per-user monthly thresholds
- Group-based threshold overrides
- Alert and blocking actions

### Workspace scope

- Model Services
- Model Provider Services, where configured through the same Gateway API surface
- Service-level cost tags (`endpoint_tags`)
- Unity Catalog grants
- Model service rate limits

### Capability-gated scope

- Governed-tag definitions and assignments are documented but not applied by the baseline
- Daily/runaway budget workflows
- Legacy endpoint disablement

These items are deliberately guarded because availability can vary by cloud, account ring, API version, or preview enrollment.

## Operating model

```text
Git configuration
      |
      v
plan -> review -> apply -> verify
                         |
                         v
                 audit/change record
```

The repository is declarative, but budget updates are full replacements in the Account API. The reconciler therefore fetches the existing object, merges the desired changes, and preserves fields it does not own.

## Access requirements

The operator identity needs:

- Account permissions to manage budgets and groups
- Workspace permissions to create and manage Model Services
- `USE CATALOG`, `USE SCHEMA`, and `CREATE SERVICE` on the target schema
- `EXECUTE` on every model or provider destination
- `MANAGE` on services being updated
- `APPLY TAG` and governed-tag `ASSIGN` where supported

The customer should use separate identities for:

- CI reconciliation
- Platform administration
- End-user model access

## Recommended group separation

Budget groups should not grant model access.

```text
Budget groups:
  aigw-budget-standard
  aigw-budget-elevated
  aigw-budget-project-exception

Model access groups:
  aigw-model-users
  aigw-model-provider-users
  aigw-platform-admins
```

The budget API resolves group overrides using account principal IDs. The accelerator resolves group display names to IDs and fails when a name is ambiguous or missing.

## Budget implementation

The baseline budget is monthly because that is the standard Account Budget API time period exposed by the SDK.

The accelerator supports:

- Workspace scope
- Service tag filters
- Shared thresholds
- Default per-user thresholds
- Group/user overrides
- Email notifications
- Blocking

A daily/runaway behavior is not assumed to be a native customer API. Until Databricks confirms a supported capability, use model-service QPM/TPM limits for rapid burst protection and keep daily/runaway automation disabled in configuration.

## Model Service setup

Model Services are created through the Unity AI Gateway REST API using the workspace SDK API client. The desired state declares:

- Catalog and schema
- Service name
- Destination models or providers
- Routing/fallback configuration
- Inference logging configuration
- Rate limits
- Service-level cost tags

A service-level tag such as:

```json
{
  "team": "engineering",
  "cost_center": "EDP",
  "cost_tier": "standard"
}
```

is applied to every request routed through that service and can be used for cost attribution and budget filtering.

## Governed tags

The accelerator distinguishes service tags from Unity Catalog governed tags.

- Service tags are the supported initial mechanism for model-service cost attribution.
- Governed tags are account-level metadata with controlled values and assignment permissions.
- The baseline documents governed-tag definitions and assignments but does not apply them. It fails closed rather than substituting an ordinary service tag for a governed tag.
- Use `endpoint_tags` on Model Services until the target account exposes a supported governed-tag assignment API for the `model_service` entity type.

Before enabling governed-tag assignments, verify:

1. The target API version supports the `model_service` entity type.
2. The operator has `APPLY TAG` and governed-tag `ASSIGN`.
3. The governed tag exists with the intended allowed values.
4. The target object is not a system-provided service that cannot be modified.
5. ABAC policies can evaluate that tagged object type in the target account.

## Unity Catalog grants

A caller needs:

- `EXECUTE` on the Model Service
- `USE CATALOG` on the parent catalog
- `USE SCHEMA` on the parent schema

The accelerator uses the Grants API to apply explicit additions. It does not revoke existing grants by default.

Revocation is a separate, reviewed operation because inherited catalog/schema grants may still provide access.

## Legacy access

Revoking access to `system.ai` does not automatically disable direct legacy Model Serving/FMAPI access. Legacy endpoint retirement is an explicit migration/support workstream and is not silently performed by this repository.

Validate both paths during cutover:

- Unity AI Gateway URL
- Legacy `/serving-endpoints/...` URL

## Workflow

### Plan

```bash
python -m aigw_accelerator.cli plan --config config/customer.yml
```

Plan output should be reviewed by the platform owner and cost owner.

### Apply

```bash
python -m aigw_accelerator.cli apply \
  --config config/customer.yml \
  --confirm \
  --apply-budgets
```

The `--apply-budgets` flag is intentionally separate because a budget update replaces the full budget configuration.

### Verify

```bash
python -m aigw_accelerator.cli verify --config config/customer.yml
```

Verification queries:

- Budget configuration
- Account group membership
- Model Service configuration
- Model Service grants
- Governed-tag definitions and assignments, where enabled

## Pilot acceptance criteria

- The existing budget is discovered without mutation.
- Pilot groups are resolved unambiguously.
- A standard user receives the standard threshold.
- An elevated group receives the intended override.
- A user in two override groups resolves to the highest threshold.
- Alerts are delivered.
- Blocking works only after alert-only validation.
- Model Service tags appear in usage/cost records.
- Model Service grants work for the intended groups.
- Legacy endpoint calls are tested separately.
- Every elevated group has an owner and expiry process.

## Known limitations

- Budget enforcement is near real time and approximate; it is not an absolute invoice cap.
- The current SDK budget model exposes monthly thresholds, not a general daily period.
- External-model and provisioned-throughput budget coverage must be confirmed for the target account.
- Governed-tag support on Model Services may depend on API rollout and must be capability-tested.
- Model Service API request/response schemas are subject to preview/GA evolution; pin the SDK and validate the API before production apply.
