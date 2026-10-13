import pytest

from aigw_accelerator import tiers
from aigw_accelerator.config import ConfigError


class FakeAccount:
    def __init__(self, groups, members=None):
        self.groups = {name: {"id": str(i + 1), "displayName": name} for i, name in enumerate(groups)}
        self.members = {g["id"]: set() for g in self.groups.values()}
        for name, users in (members or {}).items():
            self.members[self.groups[name]["id"]] |= set(users)
        self.calls = []

    def find_user_id(self, email):
        return {"u@example.com": "42"}.get(email)

    def groups_by_name(self):
        return self.groups

    def group_has_member(self, group_id, user_id):
        return user_id in self.members[group_id]

    def group_member_ids(self, group_id):
        return sorted(self.members[group_id])

    def patch_group_member(self, group_id, user_id, *, add):
        self.calls.append(("add" if add else "remove", group_id, user_id))
        (self.members[group_id].add if add else self.members[group_id].discard)(user_id)


RUNAWAY = ["r.75", "r.100", "r.150"]
MONTHLY = ["m.t1", "m.t2"]


def config(enabled=True):
    return {
        "daily_raise": {
            "enabled": enabled,
            "monthly_to_daily_divisor": 15,
            "raise_trigger_remaining_usd": 10,
            "tiers": [{"group": g, "limit_usd": g.split(".")[1]} for g in RUNAWAY],
        },
        "budgets": [
            {"role": "monthly", "per_user_threshold": {"amount_usd": "100"},
             "overrides": [{"group": "m.t1", "amount_usd": "200"}, {"group": "m.t2", "amount_usd": "500"}]},
            {"role": "runaway", "per_user_threshold": {"amount_usd": "50"},
             "overrides": [{"group": g, "amount_usd": g.split(".")[1]} for g in RUNAWAY]},
        ],
    }


def test_runaway_self_raise_picks_next_tier_and_adds_before_removing():
    account = FakeAccount(RUNAWAY + MONTHLY, {"r.75": ["42"]})
    # cap 75, spend 70 (< $10 left) -> target >= 70 + 100/15 = 76.67 -> r.100
    plan = tiers.preview(account, config(), email="u@example.com", kind="runaway", spend_usd=70)
    assert plan["eligible"] is True
    assert plan["current_tier"] == "r.75"
    assert plan["target_tier"] == "r.100"
    assert plan["operations"] == [{"op": "add", "group": "r.100"}, {"op": "remove", "group": "r.75"}]
    tiers.apply_move(account, plan)
    assert [c[0] for c in account.calls] == ["add", "remove"]
    assert account.group_has_member("2", "42") and not account.group_has_member("1", "42")


def test_runaway_self_raise_fails_closed():
    account = FakeAccount(RUNAWAY + MONTHLY)
    plan = tiers.preview(account, config(), email="u@example.com", kind="runaway", spend_usd=20)
    assert plan["eligible"] is False and plan["operations"] == []  # $30 left > $10 trigger
    unknown = tiers.preview(account, config(), email="u@example.com", kind="runaway")
    assert unknown["eligible"] is False
    with pytest.raises(ConfigError):
        tiers.apply_move(account, {**plan, "operations": [{"op": "add", "group": "r.100"}]})
    assert account.calls == []


def test_runaway_rejects_downward_moves():
    account = FakeAccount(RUNAWAY + MONTHLY, {"r.100": ["42"]})
    with pytest.raises(ConfigError, match="only move up"):
        tiers.preview(account, config(), email="u@example.com", kind="runaway", target="r.75")
    with pytest.raises(ConfigError, match="only move up"):
        tiers.preview(account, config(), email="u@example.com", kind="runaway", target="base")


def test_monthly_move_and_return_to_base():
    account = FakeAccount(RUNAWAY + MONTHLY, {"m.t1": ["42"]})
    up = tiers.preview(account, config(), email="u@example.com", kind="monthly", target="m.t2")
    assert up["operations"] == [{"op": "add", "group": "m.t2"}, {"op": "remove", "group": "m.t1"}]
    tiers.apply_move(account, up)
    down = tiers.preview(account, config(), email="u@example.com", kind="monthly", target="base")
    assert down["target_cap_usd"] == 100.0
    assert down["operations"] == [{"op": "remove", "group": "m.t2"}]


def test_monthly_cap_feeds_runaway_step():
    # Monthly tier $500 -> step 500/15 = 33.3; spend 70 on cap 75 -> target >= 103.3 -> r.150
    account = FakeAccount(RUNAWAY + MONTHLY, {"r.75": ["42"], "m.t2": ["42"]})
    plan = tiers.preview(account, config(), email="u@example.com", kind="runaway", spend_usd=70)
    assert plan["monthly_cap_usd"] == 500.0
    assert plan["target_tier"] == "r.150"


def test_unknown_user_and_unconfigured_group_are_rejected():
    account = FakeAccount(RUNAWAY + MONTHLY)
    with pytest.raises(ConfigError, match="no account user"):
        tiers.preview(account, config(), email="x@example.com", kind="monthly", target="m.t1")
    with pytest.raises(ConfigError, match="not a configured"):
        tiers.preview(account, config(), email="u@example.com", kind="monthly", target="other")
    with pytest.raises(ConfigError, match="missing from the account"):
        tiers.preview(FakeAccount(MONTHLY), config(), email="u@example.com", kind="monthly", target="m.t1")


def test_reset_runaway_only_touches_runaway_groups():
    account = FakeAccount(RUNAWAY + MONTHLY, {"r.100": ["42", "7"], "m.t1": ["42"]})
    dry = tiers.reset_runaway(account, config(), dry_run=True)
    assert dry["memberships_removed"] == 2 and account.calls == []
    tiers.reset_runaway(account, config(), dry_run=False)
    assert account.group_member_ids("2") == []
    assert account.group_member_ids(account.groups["m.t1"]["id"]) == ["42"]
