"""限频规则的值对象、周期常量与展示文案。

用户级 `rate_limit_rules` 与组级模板 `group_member_rate_limit_rules` 共用同一套
规则语义，解析、规范化、校验与展示全部收敛在此，供限频执行、users 服务层与
管理后台共用。本模块只依赖标准库，避免与 rate_limiter / users / admin 形成循环导入。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Self

PERIOD_MINUTE = "minute"
PERIOD_HOUR = "hour"
PERIOD_DAY = "day"
PERIOD_MONTH = "month"

# 滑动窗口长度。month 是滚动 30 天，不是自然月。
PERIOD_SECONDS = {
    PERIOD_MINUTE: 60,
    PERIOD_HOUR: 3600,
    PERIOD_DAY: 86400,
    PERIOD_MONTH: 30 * 86400,
}

# 周期展示名，顺序即页面下拉顺序与规则规范化排序顺序。
PERIOD_CHOICES = {
    PERIOD_MINUTE: "每分钟",
    PERIOD_HOUR: "每小时",
    PERIOD_DAY: "每天",
    PERIOD_MONTH: "每 30 天（滚动）",
}

_PERIOD_ORDER = {period: index for index, period in enumerate(PERIOD_CHOICES)}

# 单个用户或单个组模板允许配置的规则条数上限。周期不可重复，因此上限就是周期总数。
MAX_RULES_PER_SCOPE = len(PERIOD_CHOICES)

EMPTY_DISPLAY = "未配置（不限频）"


@dataclass(frozen=True, order=True)
class RateLimitRule:
    period: str
    max_requests: int
    is_active: bool = True

    def display(self) -> str:
        label = PERIOD_CHOICES.get(self.period, self.period)
        text = f"{label} {self.max_requests} 次"
        return text if self.is_active else f"{text}（已停用）"


@dataclass(frozen=True)
class RateLimitRuleSet:
    """一组限频规则。

    规则集按周期顺序规范化排序，因此两个内容相同、行序不同的规则集相等。
    组配置向成员传播时靠相等判断「成员是否仍跟随组配置」，顺序无关很关键。
    """

    rules: tuple[RateLimitRule, ...] = ()

    @classmethod
    def of(cls, values: Iterable[RateLimitRule | Mapping[str, object]] | None) -> Self:
        if not values:
            return cls(())
        parsed = [item if isinstance(item, RateLimitRule) else _rule_from_mapping(item) for item in values]
        parsed.sort(key=lambda rule: (_PERIOD_ORDER.get(rule.period, len(_PERIOD_ORDER)), rule.max_requests))
        return cls(tuple(parsed))

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, object]]) -> Self:
        return cls.of(
            RateLimitRule(
                period=str(row["period"]),
                max_requests=int(row["max_requests"]),
                is_active=bool(row["is_active"]),
            )
            for row in rows
        )

    def validate(self) -> None:
        if len(self.rules) > MAX_RULES_PER_SCOPE:
            raise ValueError(f"最多允许 {MAX_RULES_PER_SCOPE} 条限频规则")
        seen: set[str] = set()
        for rule in self.rules:
            if rule.period not in PERIOD_SECONDS:
                raise ValueError(f"未知的限频周期：{rule.period}")
            if rule.max_requests < 1:
                raise ValueError("max_requests 必须大于等于 1")
            if rule.period in seen:
                raise ValueError(f"限频周期重复：{rule.period}")
            seen.add(rule.period)

    def display(self) -> str:
        if not self.rules:
            return EMPTY_DISPLAY
        return " · ".join(rule.display() for rule in self.rules)

    def as_dicts(self) -> list[dict[str, object]]:
        return [
            {"period": rule.period, "max_requests": rule.max_requests, "is_active": rule.is_active}
            for rule in self.rules
        ]

    def __bool__(self) -> bool:
        return bool(self.rules)


EMPTY_RATE_LIMIT_RULES = RateLimitRuleSet()


def _rule_from_mapping(value: Mapping[str, object] | object) -> RateLimitRule:
    getter = value.get if isinstance(value, Mapping) else lambda key, default=None: getattr(value, key, default)
    try:
        max_requests = int(getter("max_requests", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_requests 必须是整数") from exc
    return RateLimitRule(
        period=str(getter("period", "")),
        max_requests=max_requests,
        is_active=bool(getter("is_active", True)),
    )
