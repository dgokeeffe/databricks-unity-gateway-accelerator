# Unity AI Gateway Governance Accelerator

A customer-shareable, configuration-driven accelerator for:

- Unity AI Gateway budget discovery and reconciliation
- Account-level budget configuration
- Budget group creation and per-group overrides
- Model Service and Model Provider Service setup
- Unity Catalog grants
- Service-level cost tags (`endpoint_tags`)
- Governed-tag configuration documentation and capability gates (assignment is not baseline-applied)
- Dry-run planning, apply, verification, and audit output

This repository deliberately separates **supported automation** from **preview or account-specific behavior**. It does not use private AI Dev Tools endpoints or undocumented daily-budget APIs.

## Supported baseline

The baseline uses:

- Databricks Python SDK `AccountClient.budgets` for account budgets
- Account SCIM APIs for budget groups
- Databricks Workspace SDK/API clients for Model Services and grants
- Declarative YAML desired state
- Plan/apply/verify workflow

The standard budget API currently models monthly thresholds. A runaway/daily workflow is an optional extension and is not assumed to be available in every account.

## Repository layout

```text
config/example.yml                 # customer-safe desired state
src/aigw_accelerator/
  cli.py                            # plan/apply/verify CLI
  config.py                         # YAML loading and validation
  account.py                        # account SDK + SCIM operations
  workspace.py                      # Gateway, grants and tag API adapters
  reconcile.py                      # pure desired/current diff logic
tests/test_reconcile.py             # deterministic unit tests
docs/miniwiki.md                    # customer-facing implementation plan
```

## Prerequisites

- Python 3.11+
- A current `databricks-sdk` release exposing `AccountClient.budgets` (pin the tested minor version in customer CI)
- Account administrator or delegated account-budget permissions
- Workspace identity with permissions to create/manage Model Services
- `USE CATALOG`, `USE SCHEMA`, and `CREATE SERVICE` on the target schema
- `EXECUTE` on every destination model/provider
- `MANAGE` on services and `APPLY TAG`/governed-tag `ASSIGN` where assignments are supported
- An account-level OAuth service principal is recommended for CI

## Quick start

```bash
cd ai-gateway-accelerator
uv sync --extra dev

cp config/example.yml config/customer.yml
# Edit customer.yml. Do not commit secrets or customer identifiers.

# Read-only discovery/plan
python -m aigw_accelerator.cli plan --config config/customer.yml

# Apply only after reviewing the plan
python -m aigw_accelerator.cli apply --config config/customer.yml --confirm

# Re-read and verify the resulting state
python -m aigw_accelerator.cli verify --config config/customer.yml
```

The default mode is read-only. `apply` requires the explicit `--confirm` flag.

## Important tagging distinction

This accelerator supports two different tag concepts:

1. **Service tags (`endpoint_tags`)** — tags on a Model Service used for cost attribution and budget filters. These are the recommended initial mechanism.
2. **Unity Catalog governed tags** — account-level governed metadata used by supported UC/ABAC features. Support for assigning governed tags directly to Model Services must be verified against the target account/API version before enabling it.

The baseline fails closed for governed-tag definitions or assignments rather than claiming they were applied. It never silently substitutes an ordinary service tag for a governed tag. Use `endpoint_tags` until the target account exposes a supported Model Service governed-tag assignment API.

## Safety model

- `plan` performs no writes.
- `apply` uses fetch-modify-write for budgets and preserves existing fields.
- Budget updates are treated as full replacements by the Account API.
- Budget deletion is never performed by the reconciler.
- Existing grants are not revoked unless an explicit `revoke` policy is enabled.
- Existing Model Service tags/rate limits are not replaced unless `allow_service_config_replacement` is explicitly set.
- Legacy Model Serving/FMAPI disablement is out of scope for automatic destructive changes and remains an explicit migration/support action.
- Private or undocumented budget-console APIs are not used.

## Current limitations

- Standard budget thresholds are monthly; no native daily period is assumed.
- Budget enforcement is near-real-time and approximate; it is not an absolute invoice cap.
- Provisioned-throughput and external-model budget coverage must be confirmed for the customer account.
- Model Service API schemas evolve; the adapter keeps raw payloads configurable and validates the target API before apply.
- Governed-tag support for Model Services is capability-gated because public tagging documentation and AI Gateway metadata APIs may not have identical rollout coverage.

See [`docs/miniwiki.md`](docs/miniwiki.md) for the customer-facing plan and operational runbook.

## License

(c) 2026 Databricks, Inc. All rights reserved.

This project is provided under the [Databricks License](LICENSE.md).
