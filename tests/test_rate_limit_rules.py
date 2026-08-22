from __future__ import annotations

import pytest

from app.rate_limit_rules import (
    EMPTY_DISPLAY,
    EMPTY_RATE_LIMIT_RULES,
    MAX_RULES_PER_SCOPE,
    PERIOD_CHOICES,
    PERIOD_SECONDS,
    RateLimitRule,
    RateLimitRuleSet,
)


def test_rule_set_equality_ignores_input_order():
    # 组配置传播时靠相等判断「成员是否仍跟随组配置」，行序不能影响结果。
    ascending = RateLimitRuleSet.of(
        [{"period": "minute", "max_requests": 3}, {"period": "day", "max_requests": 100}]
    )
    descending = RateLimitRuleSet.of(
        [{"period": "day", "max_requests": 100}, {"period": "minute", "max_requests": 3}]
    )
    assert ascending == descending
    assert [rule.period for rule in descending.rules] == ["minute", "day"]


def test_from_rows_normalizes_order_so_row_id_order_does_not_matter():
    # from_rows 按主键顺序拿到行，成员与组模板的插入顺序不同也必须判定为相等。
    group_rows = [
        {"period": "minute", "max_requests": 5, "is_active": 1},
        {"period": "hour", "max_requests": 60, "is_active": 1},
    ]
    member_rows = [
        {"period": "hour", "max_requests": 60, "is_active": 1},
        {"period": "minute", "max_requests": 5, "is_active": 1},
    ]
    assert RateLimitRuleSet.from_rows(group_rows) == RateLimitRuleSet.from_rows(member_rows)


def test_is_active_participates_in_equality():
    active = RateLimitRuleSet.of([{"period": "minute", "max_requests": 5, "is_active": True}])
    inactive = RateLimitRuleSet.of([{"period": "minute", "max_requests": 5, "is_active": False}])
    assert active != inactive


def test_of_accepts_rule_objects_and_empty_inputs():
    from_objects = RateLimitRuleSet.of([RateLimitRule(period="hour", max_requests=10)])
    assert from_objects.rules == (RateLimitRule(period="hour", max_requests=10, is_active=True),)
    assert RateLimitRuleSet.of(None) == EMPTY_RATE_LIMIT_RULES
    assert RateLimitRuleSet.of([]) == EMPTY_RATE_LIMIT_RULES


def test_empty_rule_set_is_falsy_but_not_none():
    # update_user 用 `is not None` 判定是否改写规则，空规则集必须是「清空」而不是「不修改」。
    assert not EMPTY_RATE_LIMIT_RULES
    assert EMPTY_RATE_LIMIT_RULES is not None
    assert RateLimitRuleSet.of([{"period": "minute", "max_requests": 1}])


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ([], EMPTY_DISPLAY),
        ([{"period": "minute", "max_requests": 5}], "每分钟 5 次"),
        ([{"period": "hour", "max_requests": 40, "is_active": False}], "每小时 40 次（已停用）"),
        (
            [{"period": "minute", "max_requests": 5}, {"period": "day", "max_requests": 100}],
            "每分钟 5 次 · 每天 100 次",
        ),
    ],
)
def test_display_renders_preview_text(rules, expected):
    # 覆盖预览弹窗直接展示这段文案，管理员据此确认破坏性覆盖。
    assert RateLimitRuleSet.of(rules).display() == expected


def test_as_dicts_roundtrips_through_of():
    original = RateLimitRuleSet.of(
        [{"period": "minute", "max_requests": 5}, {"period": "day", "max_requests": 9, "is_active": False}]
    )
    assert original.as_dicts() == [
        {"period": "minute", "max_requests": 5, "is_active": True},
        {"period": "day", "max_requests": 9, "is_active": False},
    ]
    assert RateLimitRuleSet.of(original.as_dicts()) == original


def test_validate_accepts_every_distinct_period():
    RateLimitRuleSet.of([{"period": period, "max_requests": 5} for period in PERIOD_CHOICES]).validate()


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ([{"period": "minute", "max_requests": 5}, {"period": "minute", "max_requests": 6}], "重复"),
        ([{"period": "year", "max_requests": 5}], "未知的限频周期"),
        ([{"period": "minute", "max_requests": 0}], "max_requests 必须大于等于 1"),
        ([{"period": "minute", "max_requests": -1}], "max_requests 必须大于等于 1"),
    ],
)
def test_validate_rejects_invalid_rules(rules, message):
    with pytest.raises(ValueError, match=message):
        RateLimitRuleSet.of(rules).validate()


def test_validate_rejects_more_rules_than_the_cap():
    rules = [RateLimitRule(period="minute", max_requests=index + 1) for index in range(MAX_RULES_PER_SCOPE + 1)]
    with pytest.raises(ValueError, match=f"最多允许 {MAX_RULES_PER_SCOPE}"):
        RateLimitRuleSet(tuple(rules)).validate()


def test_of_rejects_non_integer_max_requests():
    with pytest.raises(ValueError, match="max_requests 必须是整数"):
        RateLimitRuleSet.of([{"period": "minute", "max_requests": "abc"}])


def test_period_tables_stay_in_sync():
    # 页面下拉、规范化排序和限频执行分别读这三张表，任何一张漏加周期都会静默出错。
    assert set(PERIOD_CHOICES) == set(PERIOD_SECONDS)
