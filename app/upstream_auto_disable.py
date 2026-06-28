from __future__ import annotations

from novelai_python._exceptions import APIError

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
        if not self.config.enabled or status_code is None:
            return
        if status_code not in set(self.config.status_codes):
            return

        try:
            disabled = self.runtime.disable_upstream(upstream_id)
            if disabled is None:
                return
            event_time = utc_now_iso()
            self.notifications.create(
                event_type="upstream_auto_disabled",
                title="上游账号已自动禁用",
                content=(
                    f"上游账号 {upstream_id} 返回 HTTP {status_code}，已从可用队列中移除。"
                    "请检查 NovelAI API Key 或账号状态后再手动启用。"
                ),
                event_time=event_time,
                metadata={
                    "upstream_id": upstream_id,
                    "status_code": status_code,
                },
            )
            logger.warning(
                "upstream auto disabled upstream_id=%s status_code=%s",
                upstream_id,
                status_code,
            )
        except Exception:
            logger.exception(
                "failed to auto disable upstream upstream_id=%s status_code=%s",
                upstream_id,
                status_code,
            )


def _status_code(exc: APIError) -> int | None:
    try:
        return int(str(exc.code))
    except (TypeError, ValueError):
        return None
