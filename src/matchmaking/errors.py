"""撮合后端的领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域错误的基类。"""

    code = "domain_error"


class ValidationError(DomainError):
    """输入不满足领域约束，对应 HTTP 400。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """聚合根或实体不存在，对应 HTTP 404。"""

    code = "not_found"


class ConflictError(DomainError):
    """当前状态不允许该操作（如未双方确认就转项目），对应 HTTP 409。"""

    code = "conflict"
