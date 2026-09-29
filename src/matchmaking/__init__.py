"""国际论坛项目撮合后端。

公开入口：
- ``Service``：全部用例的应用服务；
- ``Repository``：内存仓储，可替换为数据库实现；
- 领域错误类型见 ``errors``。
"""
from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .repository import Repository
from .service import Service

__all__ = [
    "Service",
    "Repository",
    "DomainError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
]
