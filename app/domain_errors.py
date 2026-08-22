from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400
    default_message = "无效的请求"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class InvalidDomainInput(DomainError):
    status_code = 400


class UserNotFound(DomainError):
    status_code = 404
    default_message = "用户不存在"


class UserGroupNotFound(DomainError):
    status_code = 404
    default_message = "用户组不存在"


class UserGroupDisabled(DomainError):
    status_code = 400
    default_message = "用户组已被禁用"


class SelfServiceAccountDeleted(DomainError):
    status_code = 403
    default_message = "账号已被删除，请联系管理员"


class SelfServiceAccountDisabled(DomainError):
    status_code = 403
    default_message = "账号已被禁用"


class UpstreamNotFound(DomainError):
    status_code = 404
    default_message = "上游不存在"


class UpstreamConflict(DomainError):
    status_code = 409
