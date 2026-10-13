# Daily/runaway budget — design and status

## Design (resolved)

The runaway ("daily") control is **not a native daily-period budget**. As in the internal AI Dev Tools deployment it is modelled on, it is a **second monthly-period Unity AI Gateway budget** over the same scope as the monthly budget:

| | Monthly (Spend_max) budget | Runaway budget |
|---|---|---|
| Config | `budgets[]` with `role: monthly` | `budgets[]` with `role: runaway` |
| Default per-user cap | `per_user_threshold` | `per_user_threshold` (base tier) |
| Tiers | a few coarse override groups (≈2×, 5×, max), raised by approval and time-limited | a fine ladder of override groups, each a month-to-date cap; the group name encodes the amount |
| Movement | deliberate (manager/operator) | self-raise: next tier ≥ `month-to-date spend + monthly cap / 15`, offered when less than `raise_trigger_remaining_usd` remains and the monthly cap is not reached |
| Reset | — | users return to the base tier at the start of each month |

Both budgets apply at once, so a user's effective limit is the lower of (runaway tier) and (monthly cap). Enforcement is native: each budget blocks at its per-user threshold. Public telemetry is used only to *choose* a tier, never as the stop signal.

The standard Account Budgets API is sufficient: both objects use `time_period: MONTH`, and tiers are `principal_overrides` that point at account groups. No private daily-period API is needed.

## What the accelerator does

- `config.py` validates the pair: at most one `role: runaway` budget, the same `workspace_ids`/`tags` scope as the monthly budget, every tier above the base threshold, and — when `daily_raise.enabled` — `daily_raise.tiers` exactly equal to the runaway budget's overrides (same groups and amounts). `runaway_budget.enabled` (a native daily period) stays rejected.
- `apply --apply-budgets` creates or updates both budgets and ensures every tier group exists. A new budget is created when no `id` is given; pin the returned `id` in config before rerunning.
- `verify` checks both budgets' scope, thresholds, actions, and override amounts, plus tier-group existence.
- `daily.py` implements eligibility, next-tier selection, and add-before-remove SCIM moves. Despite the module name, its "daily spend/cap" inputs are the **month-to-date spend and threshold on the runaway budget**.

## Tier operations (operator)

`tiers.py` drives both the CLI and the console's **Tiers** view:

| Operation | CLI | App |
|---|---|---|
| Preview a move (no writes) | `aigw-accelerator tier-preview --config C --email E --kind runaway\|monthly [--target G\|base] [--spend USD]` | `POST /api/tiers/preview` |
| Apply a move | `tier-move ... --confirm` | `POST /api/tiers/move` (`confirmation: "MOVE"`) |
| Month-start runaway reset | `tier-reset` (dry run), `tier-reset --confirm` | `POST /api/tiers/reset-runaway` (`dry_run`, `confirmation: "RESET"`) |

- **Runaway, no target:** the self-raise rule. It needs month-to-date spend (`--spend`, or the system-table estimate when `DATABRICKS_WAREHOUSE_ID` is set) and fails closed when spend is unknown or the user isn't eligible.
- **Runaway with a target:** an operator move, upward only. Returning to base happens only through the reset.
- **Monthly:** an explicit target tier group, or `base`.
- Moves add the target group before removing other tiers in the same ladder, then check that membership converged. Only configured tier groups are read or changed.
- Every applied move or reset writes an audit record (`plan-output/` for the CLI, the UC Volume for the app). App writes also need `AIGW_ENABLE_APPLY=true` and the admin allowlist.
- `groups[].members` declares members that `apply` adds (additive only), and `verify` checks them.

## Still to build

1. **Engineer self-service.** An identity-bound self-raise for engineers, with the subject taken from the authenticated identity. The admin flow above is the building block.
2. **Scheduling.** Run `tier-reset --confirm` at the start of each month, for example as a Databricks job.
3. **Proactive promotion.** Optionally, a scheduled job that promotes users at most once a day when spend nears their current tier.
4. **Monthly tier expiry.** Monthly tier grants should be time-limited (1, 3 or 6 months) and reverted when they expire; that needs durable state, not the app filesystem.
5. **Spend source.** Native per-user spend-status APIs are not part of the public surface. Public system-table estimates lag, so they may promote late; enforcement is unaffected.

## Operating notes

- Tier groups must be Databricks-managed **account** groups. IdP-synchronised groups can have memberships overwritten.
- Because the gateway uses the highest override a user qualifies for, lowering a tier requires removing the higher membership.
- Keep the ladder and overrides in sync through config; validation fails closed when they differ.
- Pilot with test principals before blocking broadly; confirm an enforcement change after a membership move rather than inferring it from telemetry.
