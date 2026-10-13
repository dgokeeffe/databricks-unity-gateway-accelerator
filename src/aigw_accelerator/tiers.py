"""Tier membership operations for the monthly and runaway budgets.

Both budgets express tiers as per-user overrides on account groups. This
module resolves a user's current tiers from group membership, previews a move,
applies it with add-before-remove SCIM operations, and resets the runaway
ladder at the start of a month. Only groups that are part of a configured
ladder are ever touched.

Effective per-user caps:
- monthly: highest monthly tier group the user is in, else the monthly base;
- runaway: highest runaway tier group the user is in, else the runaway base.
The gateway enforces the lower of the two.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .account import AccountAutomation
from .config import ConfigError
from .daily import DailyTier, daily_raise_eligible, next_daily_tier, normalize_tiers

KINDS = ("monthly", "runaway")
BASE = "base"


@dataclass(frozen=True)
class Ladder:
    kind: str
    base_usd: Decimal
    tiers: tuple[DailyTier, ...]

    def by_group(self) -> dict[str, DailyTier]:
        return {tier.group: tier for tier in self.tiers}

    def current(self, memberships: set[str]) -> DailyTier | None:
        held = [tier for tier in self.tiers if tier.group in memberships]
        return max(held) if held else None

    def cap(self, memberships: set[str]) -> Decimal:
        tier = self.current(memberships)
        return tier.limit_usd if tier else self.base_usd


def _budget(config: dict[str, Any], role: str) -> dict[str, Any] | None:
    matches = [b for b in config.get("budgets", []) if b.get("role", "monthly") == role]
    if len(matches) > 1:
        raise ConfigError(f"tier operations need exactly one {role} budget, found {len(matches)}")
    return matches[0] if matches else None


def ladders(config: dict[str, Any]) -> dict[str, Ladder]:
    result: dict[str, Ladder] = {}
    for kind in KINDS:
        budget = _budget(config, kind)
        if not budget or not budget.get("per_user_threshold"):
            continue
        tiers = [
            {"group": item["group"], "limit_usd": item["amount_usd"]}
            for item in budget.get("overrides", [])
        ]
        result[kind] = Ladder(
            kind=kind,
            base_usd=Decimal(str(budget["per_user_threshold"]["amount_usd"])),
            tiers=tuple(normalize_tiers(tiers)) if tiers else (),
        )
    return result


def _ladder(config: dict[str, Any], kind: str) -> Ladder:
    if kind not in KINDS:
        raise ConfigError(f"kind must be one of {KINDS}")
    found = ladders(config).get(kind)
    if not found or not found.tiers:
        raise ConfigError(f"no {kind} budget with per-user tier overrides is configured")
    return found


def memberships(account: AccountAutomation, user_id: str, groups: dict[str, Any], names: set[str]) -> set[str]:
    """Which of ``names`` the user belongs to (only configured tier groups are read)."""
    held: set[str] = set()
    for name in names:
        group = groups.get(name)
        if group and group.get("id") and account.group_has_member(str(group["id"]), user_id):
            held.add(name)
    return held


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def preview(
    account: AccountAutomation,
    config: dict[str, Any],
    *,
    email: str,
    kind: str,
    target: str | None = None,
    spend_usd: float | None = None,
) -> dict[str, Any]:
    """Compute a tier move without writing.

    runaway, no target: self-raise rule — next tier >= MTD spend + monthly cap /
    divisor, only when eligible (known spend, under the monthly cap, less than
    the trigger remaining). runaway with target: operator move, upward only.
    monthly: explicit target tier group or "base" (operator decision).
    """
    ladder = _ladder(config, kind)
    all_ladders = ladders(config)
    user_id = account.find_user_id(email)
    if user_id is None:
        raise ConfigError(f"no account user found for {email}")
    groups = account.groups_by_name()
    tier_names = {t.group for lad in all_ladders.values() for t in lad.tiers}
    missing = sorted(n for n in tier_names if n not in groups)
    if missing:
        raise ConfigError("tier groups missing from the account; run apply first: " + ", ".join(missing))
    held = memberships(account, user_id, groups, tier_names)

    monthly = all_ladders.get("monthly")
    runaway = all_ladders.get("runaway")
    monthly_cap = monthly.cap(held) if monthly else None
    runaway_cap = runaway.cap(held) if runaway else None
    current = ladder.current(held)
    result: dict[str, Any] = {
        "email": email,
        "user_id": user_id,
        "kind": kind,
        "current_tier": current.group if current else BASE,
        "current_cap_usd": _money(ladder.cap(held)),
        "monthly_cap_usd": _money(monthly_cap),
        "runaway_cap_usd": _money(runaway_cap),
        "spend_usd": spend_usd,
        "eligible": None,
        "target_tier": None,
        "target_cap_usd": None,
        "operations": [],
        "reason": "",
    }

    chosen: DailyTier | None
    if kind == "runaway" and target is None:
        policy = config.get("daily_raise") or {}
        if not policy.get("enabled"):
            raise ConfigError("daily_raise is disabled; pass an explicit target tier")
        if spend_usd is None or monthly_cap is None:
            result.update(eligible=False, reason="month-to-date spend is unknown; pass an explicit target")
            return result
        eligible = daily_raise_eligible(
            daily_spend_usd=spend_usd,
            daily_cap_usd=runaway_cap,
            monthly_spend_usd=spend_usd,
            monthly_cap_usd=monthly_cap,
            remaining_trigger_usd=policy.get("raise_trigger_remaining_usd", 100),
        )
        result["eligible"] = eligible
        if not eligible:
            result["reason"] = (
                "not eligible: the monthly cap is reached or more than the trigger remains"
            )
            return result
        chosen = next_daily_tier(
            daily_spend_usd=spend_usd,
            monthly_cap_usd=monthly_cap,
            current_daily_cap_usd=runaway_cap,
            tiers=ladder.tiers,
            monthly_to_daily_divisor=policy.get("monthly_to_daily_divisor", 15),
        )
        if chosen is None:
            result["reason"] = "already at or above the highest runaway tier"
            return result
    elif target in (None, ""):
        raise ConfigError("monthly tier moves need an explicit target tier group or 'base'")
    elif target == BASE:
        if kind == "runaway":
            raise ConfigError("runaway tiers only move up; use the monthly reset to return to base")
        chosen = None
    else:
        chosen = ladder.by_group().get(target)
        if chosen is None:
            raise ConfigError(f"{target!r} is not a configured {kind} tier group")
        if kind == "runaway" and chosen.limit_usd <= ladder.cap(held):
            raise ConfigError("runaway tiers only move up; target is not above the current cap")

    ops: list[dict[str, str]] = []
    if chosen and chosen.group not in held:
        ops.append({"op": "add", "group": chosen.group})
    for tier in ladder.tiers:
        if tier.group in held and (chosen is None or tier.group != chosen.group):
            ops.append({"op": "remove", "group": tier.group})
    result.update(
        target_tier=chosen.group if chosen else BASE,
        target_cap_usd=_money(chosen.limit_usd if chosen else ladder.base_usd),
        operations=ops,
        reason=result["reason"] or ("no change needed" if not ops else "ready"),
    )
    if result["eligible"] is None:
        result["eligible"] = True
    return result


def apply_move(account: AccountAutomation, plan: dict[str, Any]) -> dict[str, Any]:
    """Execute a preview: every add before any remove, then verify."""
    if not plan.get("operations"):
        return {**plan, "applied": False}
    if plan.get("eligible") is False:
        raise ConfigError(f"refusing to move: {plan.get('reason')}")
    groups = account.groups_by_name()
    user_id = str(plan["user_id"])
    ordered = sorted(plan["operations"], key=lambda op: op["op"] != "add")
    for op in ordered:
        group_id = str(groups[op["group"]]["id"])
        has = account.group_has_member(group_id, user_id)
        if op["op"] == "add" and not has:
            account.patch_group_member(group_id, user_id, add=True)
        elif op["op"] == "remove" and has:
            account.patch_group_member(group_id, user_id, add=False)
    failures = [
        op["group"]
        for op in ordered
        if account.group_has_member(str(groups[op["group"]]["id"]), user_id) != (op["op"] == "add")
    ]
    if failures:
        raise RuntimeError("membership did not converge for: " + ", ".join(failures))
    return {**plan, "applied": True}


def reset_runaway(account: AccountAutomation, config: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """Return every user to the runaway base tier (run at the start of each month)."""
    ladder = _ladder(config, "runaway")
    groups = account.groups_by_name()
    removed: dict[str, list[str]] = {}
    for tier in ladder.tiers:
        group = groups.get(tier.group)
        if not group or not group.get("id"):
            continue
        members = account.group_member_ids(str(group["id"]))
        if members:
            removed[tier.group] = members
        if not dry_run:
            for user_id in members:
                account.patch_group_member(str(group["id"]), user_id, add=False)
    return {
        "dry_run": dry_run,
        "groups": len(ladder.tiers),
        "memberships_removed": sum(len(v) for v in removed.values()),
        "by_group": {name: len(ids) for name, ids in removed.items()},
    }


def write_tier_audit(record: dict[str, Any], *, actor: str, action: str) -> Path:
    output = Path("plan-output")
    output.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc)
    path = output / f"tier-{action}-{stamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(
        json.dumps(
            {"timestamp": stamp.isoformat(), "actor": actor, "action": action, **record},
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path
