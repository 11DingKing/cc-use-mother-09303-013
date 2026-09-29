"""国际论坛项目撮合后端。"""
from __future__ import annotations

from .errors import DomainError, NotFoundError, ConflictError, ValidationError
from .models import (
    Organization,
    Contact,
    Capability,
    Resource,
    Allocation,
    Exclusion,
    Demand,
    DemandVersion,
    Intent,
    Candidate,
    Reason,
    RejectedMatch,
    MatchRun,
    Project,
    TimelineEvent,
)
from .repository import Repository
from .service import MatchmakingService

__all__ = [
    "DomainError",
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "Organization",
    "Contact",
    "Capability",
    "Resource",
    "Allocation",
    "Exclusion",
    "Demand",
    "DemandVersion",
    "Intent",
    "Candidate",
    "Reason",
    "Confirmation",
    "RejectedMatch",
    "MatchRun",
    "Project",
    "TimelineEvent",
    "Repository",
    "MatchmakingService",
]
