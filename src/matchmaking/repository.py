"""内存仓储与谱系记录器。

仓储集中持有全部聚合，服务层通过它完成保存；所有写操作先经过
服务层校验。换成数据库实现时只需保持同名存取接口。
"""
from __future__ import annotations

from .lineage import LineageEvent
from .models import (
    Candidate,
    Capability,
    Contact,
    Exclusion,
    Intent,
    Message,
    Organization,
    Project,
    ResourceBatch,
)


class Repository:
    def __init__(self) -> None:
        self.organizations: dict[str, Organization] = {}
        self.contacts: dict[str, Contact] = {}
        self.resources: dict[str, ResourceBatch] = {}
        self.capabilities: dict[str, Capability] = {}
        self.exclusions: dict[str, Exclusion] = {}
        self.intents: dict[str, Intent] = {}
        self.candidates: dict[str, Candidate] = {}
        self.projects: dict[str, Project] = {}
        self.messages: dict[str, Message] = {}
        self._lineage: list[LineageEvent] = []
        self._seq = 0

    # -- 谱系 -------------------------------------------------------------

    def record(
        self,
        kind: str,
        detail: dict | None = None,
        *,
        intent_ids: list[str] | None = None,
        candidate_ids: list[str] | None = None,
        project_id: str | None = None,
    ) -> LineageEvent:
        self._seq += 1
        event = LineageEvent(
            seq=self._seq,
            kind=kind,
            detail=detail or {},
            intent_ids=list(intent_ids or []),
            candidate_ids=list(candidate_ids or []),
            project_id=project_id,
        )
        self._lineage.append(event)
        return event

    def lineage(self) -> list[LineageEvent]:
        return list(self._lineage)

    # -- 查询辅助 -----------------------------------------------------------

    def contacts_of(self, org_id: str) -> list[Contact]:
        return [c for c in self.contacts.values() if c.org_id == org_id]

    def capabilities_of(self, org_id: str) -> list[Capability]:
        return [c for c in self.capabilities.values() if c.org_id == org_id]

    def resources_of(self, org_id: str) -> list[ResourceBatch]:
        return [r for r in self.resources.values() if r.owner_id == org_id]

    def intents_of(self, org_id: str) -> list[Intent]:
        return [i for i in self.intents.values() if i.org_id == org_id]

    def exclusions_of(self, org_id: str) -> list[Exclusion]:
        return [e for e in self.exclusions.values() if e.org_id == org_id and e.active]

    def open_candidates_involving(self, org_id: str) -> list[Candidate]:
        return [
            c
            for c in self.candidates.values()
            if c.is_open() and org_id in (c.school_id, c.enterprise_id)
        ]

    def open_candidates_using_resource(self, resource_id: str) -> list[Candidate]:
        return [c for c in self.candidates.values() if c.is_open() and c.resource_id == resource_id]
