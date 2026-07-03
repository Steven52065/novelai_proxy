from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400
    default_message = "Invalid request"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class InvalidDomainInput(DomainError):
    status_code = 400


class UserNotFound(DomainError):
    status_code = 404
    default_message = "User not found"


class UserGroupNotFound(DomainError):
    status_code = 404
    default_message = "User group not found"


class UserGroupDisabled(DomainError):
    status_code = 400
    default_message = "User group is disabled"


class SelfServiceAccountDeleted(DomainError):
    status_code = 403
    default_message = "Account was deleted; contact administrator"


class SelfServiceAccountDisabled(DomainError):
    status_code = 403
    default_message = "Account is disabled"


class UpstreamNotFound(DomainError):
    status_code = 404
    default_message = "Upstream not found"


class UpstreamConflict(DomainError):
    status_code = 409
