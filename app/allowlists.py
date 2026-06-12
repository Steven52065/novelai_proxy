"""allowed_endpoints / allowed_upstreams 字段的值对象与合法端点清单。

users / user_groups 表以 CSV 字符串存储这两个字段，
解析、序列化与合法端点定义全部收敛在此，供鉴权、users 服务层与管理后台共用。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

ENDPOINT_GENERATE_IMAGE = "generate-image"
ENDPOINT_SUGGEST_TAGS = "suggest-tags"
ENDPOINT_UPSCALE = "upscale"
ENDPOINT_AUGMENT_IMAGE = "augment-image"
ENDPOINT_ENCODE_VIBE = "encode-vibe"

# 合法端点与管理后台展示名，顺序即页面复选框顺序。
ALLOWED_ENDPOINT_CHOICES = {
    ENDPOINT_GENERATE_IMAGE: "图像生成",
    ENDPOINT_SUGGEST_TAGS: "标签建议",
    ENDPOINT_UPSCALE: "图片放大",
    ENDPOINT_AUGMENT_IMAGE: "图像增强",
    ENDPOINT_ENCODE_VIBE: "Vibe 编码",
}
DEFAULT_ALLOWED_ENDPOINTS = ENDPOINT_GENERATE_IMAGE


@dataclass(frozen=True)
class _CsvField:
    """CSV 清单字段的公共语义：逗号切分、去空白；解析保留重复项，序列化时才去重。"""

    items: tuple[str, ...]

    @classmethod
    def _split(cls, value: str | None) -> tuple[str, ...]:
        if not value:
            return ()
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @classmethod
    def of(cls, values: Iterable[str] | None) -> Self:
        if not values:
            return cls(())
        return cls(tuple(item.strip() for item in values if item.strip()))

    def _unique_items(self) -> list[str]:
        unique: list[str] = []
        for item in self.items:
            if item not in unique:
                unique.append(item)
        return unique

    def as_list(self) -> list[str]:
        return list(self.items)

    def as_frozenset(self) -> frozenset[str]:
        return frozenset(self.items)


class AllowedEndpoints(_CsvField):
    """用户/组允许访问的端点清单，空值回退为默认端点。"""

    @classmethod
    def parse(cls, value: str | None) -> AllowedEndpoints:
        return cls(cls._split(value) or (DEFAULT_ALLOWED_ENDPOINTS,))

    def serialize(self) -> str:
        return ",".join(self._unique_items() or [DEFAULT_ALLOWED_ENDPOINTS])

    def unknown(self) -> list[str]:
        return sorted({item for item in self.items if item not in ALLOWED_ENDPOINT_CHOICES})


class AllowedUpstreams(_CsvField):
    """用户/组允许使用的上游清单，空值表示不限制。"""

    @classmethod
    def parse(cls, value: str | None) -> AllowedUpstreams:
        return cls(cls._split(value))

    def serialize(self) -> str | None:
        unique = self._unique_items()
        return ",".join(unique) if unique else None
