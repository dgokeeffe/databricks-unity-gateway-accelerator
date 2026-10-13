from decimal import Decimal

import pytest

from aigw_accelerator.daily import (
    DailyTier,
    DailyTierError,
    current_daily_tier,
    daily_raise_eligible,
    move_daily_tier,
    next_daily_tier,
    normalize_tiers,
)

TIERS = [
    {"group": "daily-1000", "limit_usd": 1000},
    {"group": "daily-1100", "limit_usd": 1100},
    {"group": "daily-1250", "limit_usd": 1250},
]


def test_daily_raise_eligibility_requires_low_daily_remaining_and_monthly_headroom():
    assert daily_raise_eligible(
        daily_spend_usd=450,
        daily_cap_usd=500,
        monthly_spend_usd=500,
        monthly_cap_usd=1500,
    )
    assert not daily_raise_eligible(
        daily_spend_usd=399,
        daily_cap_usd=500,
        monthly_spend_usd=500,
        monthly_cap_usd=1500,
    )
    assert not daily_raise_eligible(
        daily_spend_usd=499,
        daily_cap_usd=500,
        monthly_spend_usd=1500,
        monthly_cap_usd=1500,
    )
    assert not daily_raise_eligible(
        daily_spend_usd=None,
        daily_cap_usd=500,
        monthly_spend_usd=500,
        monthly_cap_usd=1500,
    )


def test_next_daily_tier_uses_monthly_cap_divided_by_default_step():
    target = next_daily_tier(
        daily_spend_usd=1000,
        monthly_cap_usd=1500,
        current_daily_cap_usd=1000,
        tiers=TIERS,
    )
    assert target == DailyTier(Decimal("1100"), "daily-1100")


def test_next_daily_tier_must_exceed_current_cap_even_if_spend_floor_is_lower():
    target = next_daily_tier(
        daily_spend_usd=100,
        monthly_cap_usd=1500,
        current_daily_cap_usd=1000,
        tiers=TIERS,
    )
    assert target == DailyTier(Decimal("1100"), "daily-1100")


def test_next_daily_tier_returns_none_when_ladder_has_no_eligible_step():
    assert (
        next_daily_tier(
            daily_spend_usd=1200,
            monthly_cap_usd=1500,
            current_daily_cap_usd=1100,
            tiers=TIERS,
        )
        is None
    )


def test_next_daily_tier_supports_customer_configured_divisor():
    target = next_daily_tier(
        daily_spend_usd=100,
        monthly_cap_usd=1200,
        current_daily_cap_usd=100,
        tiers=[{"group": "daily-200", "limit_usd": 200}],
        monthly_to_daily_divisor=12,
    )
    assert target == DailyTier(Decimal("200"), "daily-200")


def test_tier_configuration_requires_unique_groups_and_limits():
    with pytest.raises(DailyTierError, match="group names must be unique"):
        normalize_tiers(
            [
                {"group": "daily", "limit_usd": 100},
                {"group": "daily", "limit_usd": 200},
            ]
        )
    with pytest.raises(DailyTierError, match="limits must be unique"):
        normalize_tiers(
            [
                {"group": "daily-a", "limit_usd": 100},
                {"group": "daily-b", "limit_usd": 100},
            ]
        )


def test_highest_group_membership_wins_if_user_is_in_multiple_tiers():
    assert current_daily_tier(["daily-1000", "daily-1250"], TIERS) == DailyTier(
        Decimal("1250"), "daily-1250"
    )


def test_move_daily_tier_adds_target_before_removing_old_memberships():
    class FakeAccount:
        def __init__(self):
            self.memberships = {"daily-1000": True, "daily-1100": False, "daily-1250": False}
            self.calls = []

        def groups_by_name(self):
            return {name: {"id": name} for name in self.memberships}

        def group_has_member(self, group_id, user_id):
            return self.memberships[group_id]

        def patch_group_member(self, group_id, user_id, *, add):
            self.calls.append((group_id, user_id, add))
            self.memberships[group_id] = add

    account = FakeAccount()
    move_daily_tier(
        account,
        user_id="42",
        current_tier=DailyTier(Decimal("1000"), "daily-1000"),
        target_tier=DailyTier(Decimal("1100"), "daily-1100"),
        tiers=TIERS,
    )
    assert account.calls == [
        ("daily-1100", "42", True),
        ("daily-1000", "42", False),
    ]
    assert account.memberships["daily-1100"] is True
    assert account.memberships["daily-1000"] is False


def test_move_daily_tier_refuses_downward_moves():
    class FakeAccount:
        def get_group_by_name(self, name):
            return {"id": name}

    with pytest.raises(DailyTierError, match="strictly upward"):
        move_daily_tier(
            FakeAccount(),
            user_id="42",
            current_tier=DailyTier(Decimal("1100"), "daily-1100"),
            target_tier=DailyTier(Decimal("1000"), "daily-1000"),
            tiers=TIERS,
        )
