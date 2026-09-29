"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类，携带稳定的错误码。"""

    code = "DOMAIN_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ValidationError(DomainError):
    code = "VALIDATION_ERROR"


class NotFoundError(DomainError):
    code = "NOT_FOUND"


class ConflictError(DomainError):
    code = "CONFLICT"


class StateError(DomainError):
    code = "ILLEGAL_STATE"
