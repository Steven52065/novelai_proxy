from __future__ import annotations

from .api_errors import APIError, api_error_type

from .admin_notifications import AdminNotificationRepository
from .config import UpstreamAutoDisableConfig
from .database import utc_now_iso
from .logging_utils import logger
from .upstreams import UpstreamRuntimeManager


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
        if not self.config.enabled:
            return

        matched_status_code = status_code is not None and status_code in set(self.config.status_codes)
        matched_error_type = error_type in set(self.config.error_types)
        if not matched_status_code and not matched_error_type:
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
                },
            )
            logger.warning(
                "upstream auto disabled upstream_id=%s status_code=%s error_type=%s",
                upstream_id,
                status_code,
                error_type,
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


def _match_description(
    *,
    status_code: int | None,
    error_type: str,
    matched_status_code: bool,
    matched_error_type: bool,
) -> str:
    if matched_status_code and matched_error_type:
        return f" HTTP {status_code}（错误类型 {error_type}）"
    if matched_status_code:
        return f" HTTP {status_code}"
    return f"错误类型 {error_type}"
