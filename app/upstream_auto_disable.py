from __future__ import annotations

from .api_errors import APIError, api_error_message, api_error_type

from .admin_notifications import AdminNotificationRepository
from .config import UpstreamAutoDisableConfig
from .database import utc_now_iso
from .logging_utils import logger
from .upstreams import UpstreamRuntimeManager

_ERROR_MESSAGE_METADATA_MAX = 500


class UpstreamAutoDisableService:
    def __init__(
        self,
        *,
        config: UpstreamAutoDisableConfig,
        runtime: UpstreamRuntimeManager,
        notifications: AdminNotificationRepository,
    ):
        self.config = config
        self.runtime = runtime
        self.notifications = notifications

    def handle_api_error(self, upstream_id: str, exc: APIError) -> None:
        status_code = _status_code(exc)
        error_type = api_error_type(exc)
        error_message = api_error_message(exc)
        if not self.config.enabled:
            return

        matched_status_code = status_code is not None and status_code in set(self.config.status_codes)
        matched_error_type = error_type in set(self.config.error_types)
        matched_keyword = _matched_keyword(error_message, self.config.error_message_keywords)
        matched_exact = _matched_exact(error_message, self.config.error_message_exact)
        if not matched_status_code and not matched_error_type and matched_keyword is None and matched_exact is None:
            return

        try:
            disabled = self.runtime.disable_upstream(upstream_id)
            if disabled is None:
                return
            match_description = _match_description(
                status_code=status_code,
                error_type=error_type,
                matched_status_code=matched_status_code,
                matched_error_type=matched_error_type,
                matched_keyword=matched_keyword,
                matched_exact=matched_exact,
            )
            event_time = utc_now_iso()
            self.notifications.create(
                event_type="upstream_auto_disabled",
                title="上游账号已自动禁用",
                content=(
                    f"上游账号 {upstream_id} 返回{match_description}，已从可用队列中移除。"
                    "请检查 NovelAI API Key 或账号状态后再手动启用。"
                ),
                event_time=event_time,
                metadata={
                    "upstream_id": upstream_id,
                    "status_code": status_code,
                    "error_type": error_type,
                    "error_message": _truncate_error_message(error_message),
                },
            )
            logger.warning(
                "upstream auto disabled upstream_id=%s status_code=%s error_type=%s error_message=%s",
                upstream_id,
                status_code,
                error_type,
                _truncate_error_message(error_message),
            )
        except Exception:
            logger.exception(
                "failed to auto disable upstream upstream_id=%s status_code=%s error_type=%s",
                upstream_id,
                status_code,
                error_type,
            )


def _status_code(exc: APIError) -> int | None:
    try:
        return int(str(exc.code))
    except (TypeError, ValueError):
        return None


def _matched_keyword(error_message: str | None, keywords: list[str]) -> str | None:
    if error_message is None:
        return None
    for keyword in keywords:
        if keyword in error_message:
            return keyword
    return None


def _matched_exact(error_message: str | None, exacts: list[str]) -> str | None:
    if error_message is None:
        return None
    for exact in exacts:
        if error_message == exact:
            return exact
    return None


def _truncate_error_message(error_message: str | None) -> str | None:
    if error_message is None:
        return None
    return error_message[:_ERROR_MESSAGE_METADATA_MAX]


def _match_description(
    *,
    status_code: int | None,
    error_type: str,
    matched_status_code: bool,
    matched_error_type: bool,
    matched_keyword: str | None,
    matched_exact: str | None,
) -> str:
    extras: list[str] = []
    if matched_error_type:
        extras.append(f"错误类型 {error_type}")
    if matched_keyword is not None:
        extras.append(f"错误消息包含「{matched_keyword}」")
    if matched_exact is not None:
        extras.append(f"错误消息为「{matched_exact}」")
    if matched_status_code:
        if extras:
            return f" HTTP {status_code}（{'、'.join(extras)}）"
        return f" HTTP {status_code}"
    if not extras:
        return ""
    if len(extras) == 1:
        return extras[0]
    return "、".join(extras)
