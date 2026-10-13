"""Public, configurable daily/runaway tier policy.

Unity AI Gateway's budget API is monthly. The runaway control is a second
monthly budget (``role: runaway``) whose per-user overrides form a tier ladder of
month-to-date caps. This module selects tiers and moves users between tier
groups. Despite the "daily" names, its spend/cap inputs are the month-to-date
spend and threshold on the runaway budget; the budget itself enforces.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


class DailyTierError(ValueError):
    """Raised when a daily tier policy is invalid or cannot be applied safely."""


@dataclass(frozen=True, order=True)
class DailyTier:
    limit_usd: Decimal
    group: str

    @classmethod
    def from_mapping(cls, item: dict[str, Any]) -> "DailyTier":
        if not isinstance(item, dict):
            raise DailyTierError("each daily tier must be a mapping")
        group = str(item.get("group", "")).strip()
        if not group:
            raise DailyTierError("each daily tier needs a non-empty group")
        try:
            limit = Decimal(str(item["limit_usd"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise DailyTierError("daily tier limit_usd must be a positive number") from exc
        if not limit.is_finite() or limit <= 0:
            raise DailyTierError("daily tier limit_usd must be a positive finite number")
        return cls(limit_usd=limit, group=group)


def normalize_tiers(tiers: Iterable[DailyTier | dict[str, Any]]) -> list[DailyTier]:
    """Validate and sort the configured ladder from lowest to highest."""
    result = [
        item if isinstance(item, DailyTier) else DailyTier.from_mapping(item) for item in tiers
    ]
    if not result:
        raise DailyTierError("at least one daily tier must be configured")
    groups = [tier.group for tier in result]
    limits = [tier.limit_usd for tier in result]
    if len(groups) != len(set(groups)):
        raise DailyTierError("daily tier group names must be unique")
    if len(limits) != len(set(limits)):
        raise DailyTierError("daily tier limits must be unique")
    return sorted(result)


def daily_raise_eligible(
    *,
    daily_spend_usd: Decimal | float | int | None,
    daily_cap_usd: Decimal | float | int | None,
    monthly_spend_usd: Decimal | float | int | None,
    monthly_cap_usd: Decimal | float | int | None,
    remaining_trigger_usd: Decimal | float | int = 100,
) -> bool:
    """Fail-closed eligibility check for a daily tier raise.

    A raise is offered only when all spend/cap values are known, the monthly
    cap has not been reached, and daily remaining capacity is below the
    configured trigger. Public telemetry may lag, so an absent metric never
    silently makes a raise eligible.
    """
    values = (daily_spend_usd, daily_cap_usd, monthly_spend_usd, monthly_cap_usd)
    if any(value is None for value in values):
        return False
    try:
        daily_spend, daily_cap, monthly_spend, monthly_cap = (
            Decimal(str(value)) for value in values
        )
        trigger = Decimal(str(remaining_trigger_usd))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailyTierError("spend, caps, and trigger must be numeric") from exc
    if not all(
        value.is_finite() for value in (daily_spend, daily_cap, monthly_spend, monthly_cap, trigger)
    ):
        raise DailyTierError("spend, caps, and trigger must be finite")
    if daily_spend < 0 or daily_cap <= 0 or monthly_spend < 0 or monthly_cap <= 0 or trigger <= 0:
        raise DailyTierError("spend must be non-negative; caps and trigger must be positive")
    if monthly_spend >= monthly_cap:
        return False
    return daily_cap - daily_spend < trigger


def next_daily_tier(
    *,
    daily_spend_usd: Decimal | float | int,
    monthly_cap_usd: Decimal | float | int,
    current_daily_cap_usd: Decimal | float | int | None,
    tiers: Iterable[DailyTier | dict[str, Any]],
    monthly_to_daily_divisor: Decimal | float | int = 15,
) -> DailyTier | None:
    """Return the next eligible tier in the configured daily ladder.

    The target must be at least ``daily_spend + monthly_cap / divisor`` and
    strictly higher than the user's current tier. The default divisor (15)
    preserves the source console's policy: one raise step is based on one
    fifteenth of the monthly cap. Returns ``None`` when no configured tier
    satisfies both conditions.
    """
    try:
        spend = Decimal(str(daily_spend_usd))
        monthly_cap = Decimal(str(monthly_cap_usd))
        divisor = Decimal(str(monthly_to_daily_divisor))
        current_cap = (
            Decimal(str(current_daily_cap_usd))
            if current_daily_cap_usd is not None
            else Decimal("0")
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailyTierError("spend, caps, and divisor must be numeric") from exc
    if not all(value.is_finite() for value in (spend, monthly_cap, divisor, current_cap)):
        raise DailyTierError("spend, caps, and divisor must be finite")
    if spend < 0 or monthly_cap <= 0 or divisor <= 0 or current_cap < 0:
        raise DailyTierError(
            "spend/current cap must be non-negative; monthly cap and divisor positive"
        )

    minimum_target = spend + monthly_cap / divisor
    for tier in normalize_tiers(tiers):
        if tier.limit_usd > current_cap and tier.limit_usd >= minimum_target:
            return tier
    return None


def current_daily_tier(
    memberships: Iterable[str], tiers: Iterable[DailyTier | dict[str, Any]]
) -> DailyTier | None:
    """Resolve a user's current tier from group membership.

    If membership is accidentally present in multiple configured tier groups,
    the highest tier wins, matching group-override semantics.
    """
    names = set(memberships)
    return next(
        (tier for tier in reversed(normalize_tiers(tiers)) if tier.group in names),
        None,
    )


def move_daily_tier(
    account: Any,
    *,
    user_id: str,
    current_tier: DailyTier | None,
    target_tier: DailyTier,
    tiers: Iterable[DailyTier | dict[str, Any]],
    current_memberships: Iterable[str] = (),
) -> None:
    """Move a user to a configured tier group using account SCIM operations.

    Add the target membership first, then remove known old tier memberships.
    If cleanup fails, the user remains at the higher effective override rather
    than being accidentally reduced to the default.
    """
    ladder = normalize_tiers(tiers)
    if target_tier not in ladder:
        raise DailyTierError("target tier is not part of the configured ladder")
    if current_tier and current_tier not in ladder:
        raise DailyTierError("current tier is not part of the configured ladder")
    if current_tier and target_tier.limit_usd <= current_tier.limit_usd:
        raise DailyTierError("daily tier moves must be strictly upward")

    groups = account.groups_by_name()
    target_group = groups.get(target_tier.group)
    if not target_group or not target_group.get("id"):
        raise DailyTierError(f"target tier group does not exist: {target_tier.group}")
    target_group_id = str(target_group["id"])
    if not account.group_has_member(target_group_id, str(user_id)):
        account.patch_group_member(target_group_id, str(user_id), add=True)

    known_tiers = {tier.group for tier in ladder}
    old_groups = set(current_memberships) & known_tiers
    if current_tier:
        old_groups.add(current_tier.group)
    for group_name in old_groups - {target_tier.group}:
        group = groups.get(group_name)
        if group and group.get("id"):
            group_id = str(group["id"])
            if account.group_has_member(group_id, str(user_id)):
                account.patch_group_member(group_id, str(user_id), add=False)
