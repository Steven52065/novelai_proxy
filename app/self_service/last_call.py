from __future__ import annotations

from sqlite3 import Row

from ..admin.common import format_display_time


LAST_CALL_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "rejected": "已拒绝",
}

# 徽章配色按语义分三档，不能只按“是否成功”二分：badge-inactive 用的是
# --danger-bg 与红色前景（与「账号已禁用」同款），排队中/运行中套上去会让
# 正常等待显示成告警，用户会以为出错而重复提交。
LAST_CALL_STATUS_BADGES = {
    "success": "badge-active",
    "failed": "badge-inactive",
    "rejected": "badge-inactive",
    "queued": "badge-normal",
    "running": "badge-normal",
}

# /account「最近调用」失败原因白名单：只有文案硬编码在本仓库、或只描述该用户
# 自身状态的错误码才把 error_message 原文展示给用户。其余一律折叠成通用文案，
# 避免把上游内网 IP/端口、curl/OpenSSL 传输栈细节、以及他人自助上游的数字 ID
# 泄露给普通用户。白名单是安全边界，写死在这里，不做成配置项。
SAFE_ERROR_CODES = frozenset({
    # 本仓库硬编码中文的队列/生命周期错误。
    "queue_full",
    "server_shutting_down",
    "client_cancelled",
    "user_unavailable",
    "unsupported_cost",
    # 用户自身配额/限流，消息由本仓库生成，用户看了能自己调整。
    "rate_limited",
    "free_small_only_blocked",
    "free_small_daily_limit_exceeded",
    "insufficient_anlas",
    # 上游对本次请求参数的判定，用户看了能自己改参数；429 原文实测安全。
    "400",
    "429",
})

# 未命中白名单的错误码折叠成通用文案；命中 401/402 时也不回显原文，
# 因为描述的是公共号池健康状况，不是用户自己能处理的问题。
GENERIC_FAILURE_MESSAGES = {
    "401": "上游账号异常，请联系管理员",
    "402": "上游账号异常，请联系管理员",
    "500": "上游服务异常，请稍后重试",
    "no_available_upstream": "当前没有可用的上游，请稍后重试",
    "upstream_timeout": "上游处理超时，请稍后重试",
}


# 白名单命中后仍要过一道“形状检查”：上游返回非 JSON 时，_response_error()
# （app/upstream.py）会把最多 2000 字符的原始响应体当作 message 落库，网关 HTML 页
# 里可能带 nginx 版本、ray-id、origin IP。真正安全的上游文案都是短单行，
# 因此超长、多行或含标签的一律按未命中处理。
_MAX_SAFE_MESSAGE_CHARS = 200


def _looks_like_safe_message(message: str) -> bool:
    if len(message) > _MAX_SAFE_MESSAGE_CHARS:
        return False
    return not any(char in message for char in "<\n\r")


def describe_failure(error_code: str | None, error_message: str | None) -> str:
    """把 usage_logs 里的失败原因转成可展示文案；未命中白名单不回显 error_code，
    否则 SSLError 之类的异常类名本身仍会暴露传输栈细节。"""
    generic = GENERIC_FAILURE_MESSAGES.get(error_code or "", "调用失败，请稍后重试")
    if error_code not in SAFE_ERROR_CODES:
        return generic
    message = (error_message or "").strip()
    if not message or not _looks_like_safe_message(message):
        return generic
    if error_code.isdigit():
        return f"上游返回 {error_code}：{message}"
    return message


def build_last_call_view(row: Row | None) -> dict | None:
    """把 get_last_call_for_user() 的行组装成 /account 模板需要的字段。"""
    if row is None:
        return None
    status = row["status"]
    is_success = status == "success"
    reason = None
    if not is_success and status in ("failed", "rejected"):
        reason = describe_failure(row["error_code"], row["error_message"])
    return {
        "time_display": format_display_time(row["created_at"]),
        "status": status,
        "status_label": LAST_CALL_STATUS_LABELS.get(status, status),
        "badge_class": LAST_CALL_STATUS_BADGES.get(status, "badge-normal"),
        "is_success": is_success,
        "reason": reason,
    }
