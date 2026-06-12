from __future__ import annotations

from datetime import timedelta, timezone

# 项目统一的本地时区（UTC+8）单点定义：管理后台时间展示、仪表盘小时统计桶、
# 每日免费小图窗口重置均以此为准。仅支持整小时偏移。
TZ_OFFSET_HOURS = 8
DISPLAY_TIMEZONE = timezone(timedelta(hours=TZ_OFFSET_HOURS))
