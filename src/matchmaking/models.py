"""领域模型：登记信息、需求版本、排除条件、资源容量、授权联系人、候选与项目。

所有模型均可 ``to_dict`` / ``from_dict``，供仓储做 JSON 持久化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 枚举（字符串常量，保证序列化稳定）------------------------------------

ORG_FORUM = "forum"            # 论坛秘书处
ORG_COLLEGE = "college"        # 职业院校
ORG_ENTERPRISE = "enterprise"  # 合作企业

CONTACT_ACTIVE = "active"
CONTACT_INVALID = "invalid"

INTENT_ACTIVE = "active"
INTENT_WITHDRAWN = "withdrawn"

DEMAND_ACTIVE = "active"
DEMAND_WITHDRAWN = "withdrawn"

VERSION_ACTIVE = "active"
VERSION_SUPERSEDED = "superseded"

# 候选生命周期：
# pending（待双方独立确认）→ confirmed（双边确认）→ converted（已转正式项目）
#   ↘ rejected（任一方拒绝） / expired（重新评估失效：撤回、联系人失效、需求变更、资源占用）
CAND_PENDING = "pending"
CAND_CONFIRMED = "confirmed"
CAND_REJECTED = "rejected"
CAND_EXPIRED = "expired"
CAND_CONVERTED = "converted"

PARTY_ENTERPRISE = "enterprise"
PARTY_COLLEGE = "college"

ALLOC_HELD = "held"            # 候选生成时的容量预留
ALLOC_COMMITTED = "committed"  # 转项目后锁定 / 外部占用
ALLOC_RELEASED = "released"

# 项目：转为正式项目即冻结匹配依据；NEGOTIATING 洽谈，ACTIVE 签约，TERMINATED 终止
PROJECT_NEGOTIATING = "negotiating"
PROJECT_ACTIVE = "active"
PROJECT_TERMINATED = "terminated"


def _ts(d: dict[str, Any], key: str, default: str = "") -> str:
    return str(d.get(key, default))


@dataclass
class Organization:
    id: str
    kind: str  # forum / college / enterprise
    name: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "name": self.name, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Organization":
        return cls(id=d["id"], kind=d["kind"], name=d["name"], created_at=_ts(d, "created_at"))


@dataclass
class Contact:
    """授权联系人。authorized_demand_ids 为 None 表示对本机构全部需求有效。"""

    id: str
    org_id: str
    name: str
    email: str
    status: str = CONTACT_ACTIVE
    authorized_demand_ids: list[str] | None = None
    created_at: str = ""
    invalidated_at: str | None = None
    invalid_reason: str | None = None

    def covers(self, demand_id: str) -> bool:
        return self.status == CONTACT_ACTIVE and (
            self.authorized_demand_ids is None or demand_id in self.authorized_demand_ids
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "org_id": self.org_id, "name": self.name, "email": self.email,
            "status": self.status, "authorized_demand_ids": self.authorized_demand_ids,
            "created_at": self.created_at, "invalidated_at": self.invalidated_at,
            "invalid_reason": self.invalid_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Contact":
        return cls(
            id=d["id"], org_id=d["org_id"], name=d["name"], email=d["email"],
            status=d.get("status", CONTACT_ACTIVE),
            authorized_demand_ids=d.get("authorized_demand_ids"),
            created_at=_ts(d, "created_at"),
            invalidated_at=d.get("invalidated_at"), invalid_reason=d.get("invalid_reason"),
        )


@dataclass
class Capability:
    """院校机构能力：按类别与标签与需求匹配。"""

    id: str
    org_id: str
    name: str
    category: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "org_id": self.org_id, "name": self.name,
                "category": self.category, "tags": self.tags, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Capability":
        return cls(id=d["id"], org_id=d["org_id"], name=d["name"], category=d["category"],
                   tags=list(d.get("tags", [])), created_at=_ts(d, "created_at"))


@dataclass
class Resource:
    """实训资源批次（可由论坛或院校持有），容量按 total_qty 计。"""

    id: str
    owner_org_id: str
    name: str
    total_qty: int
    unit: str
    capability_id: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "owner_org_id": self.owner_org_id, "name": self.name,
                "total_qty": self.total_qty, "unit": self.unit,
                "capability_id": self.capability_id, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Resource":
        return cls(id=d["id"], owner_org_id=d["owner_org_id"], name=d["name"],
                   total_qty=int(d["total_qty"]), unit=d["unit"],
                   capability_id=d.get("capability_id"), created_at=_ts(d, "created_at"))


@dataclass
class Allocation:
    """资源占用记录：候选预留 held，转项目/外部占用 committed，释放 released。"""

    id: str
    resource_id: str
    qty: int
    status: str
    candidate_id: str | None = None
    project_id: str | None = None
    reference: str | None = None
    created_at: str = ""
    released_at: str | None = None
    release_reason: str | None = None

    def occupies_capacity(self) -> bool:
        return self.status in (ALLOC_HELD, ALLOC_COMMITTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "resource_id": self.resource_id, "qty": self.qty, "status": self.status,
            "candidate_id": self.candidate_id, "project_id": self.project_id,
            "reference": self.reference, "created_at": self.created_at,
            "released_at": self.released_at, "release_reason": self.release_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Allocation":
        return cls(
            id=d["id"], resource_id=d["resource_id"], qty=int(d["qty"]), status=d["status"],
            candidate_id=d.get("candidate_id"), project_id=d.get("project_id"),
            reference=d.get("reference"), created_at=_ts(d, "created_at"),
            released_at=d.get("released_at"), release_reason=d.get("release_reason"),
        )


@dataclass
class Exclusion:
    """排除条件：A、B 之间的硬性互斥，双向生效。"""

    id: str
    org_a_id: str
    org_b_id: str
    reason: str
    active: bool = True
    created_at: str = ""

    def involves(self, x: str, y: str) -> bool:
        return {self.org_a_id, self.org_b_id} == {x, y}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "org_a_id": self.org_a_id, "org_b_id": self.org_b_id,
                "reason": self.reason, "active": self.active, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Exclusion":
        return cls(id=d["id"], org_a_id=d["org_a_id"], org_b_id=d["org_b_id"], reason=d["reason"],
                   active=d.get("active", True), created_at=_ts(d, "created_at"))


@dataclass
class DemandVersion:
    """需求的不可变版本；修订即新增版本，旧版本标记 superseded。"""

    id: str
    demand_id: str
    version_no: int
    title: str
    capability_categories: list[str]
    capability_tags: list[str]
    required_qty: int
    location: str | None
    note: str
    status: str = VERSION_ACTIVE
    created_at: str = ""

    def requirements(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no, "title": self.title,
            "capability_categories": list(self.capability_categories),
            "capability_tags": list(self.capability_tags),
            "required_qty": self.required_qty, "location": self.location, "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "demand_id": self.demand_id, "version_no": self.version_no,
            "title": self.title, "capability_categories": self.capability_categories,
            "capability_tags": self.capability_tags, "required_qty": self.required_qty,
            "location": self.location, "note": self.note, "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DemandVersion":
        return cls(
            id=d["id"], demand_id=d["demand_id"], version_no=int(d["version_no"]),
            title=d["title"], capability_categories=list(d.get("capability_categories", [])),
            capability_tags=list(d.get("capability_tags", [])),
            required_qty=int(d["required_qty"]), location=d.get("location"),
            note=d.get("note", ""), status=d.get("status", VERSION_ACTIVE),
            created_at=_ts(d, "created_at"),
        )


@dataclass
class Demand:
    id: str
    intent_id: str
    org_id: str
    status: str = DEMAND_ACTIVE
    current_version_no: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "intent_id": self.intent_id, "org_id": self.org_id,
                "status": self.status, "current_version_no": self.current_version_no,
                "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Demand":
        return cls(id=d["id"], intent_id=d["intent_id"], org_id=d["org_id"],
                   status=d.get("status", DEMAND_ACTIVE),
                   current_version_no=int(d.get("current_version_no", 0)),
                   created_at=_ts(d, "created_at"))


@dataclass
class Intent:
    """最初合作意向，整条追溯链的起点。"""

    id: str
    org_id: str
    summary: str
    target_college_org_id: str | None = None
    status: str = INTENT_ACTIVE
    created_at: str = ""
    withdrawn_at: str | None = None
    withdraw_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "org_id": self.org_id, "summary": self.summary,
            "target_college_org_id": self.target_college_org_id, "status": self.status,
            "created_at": self.created_at, "withdrawn_at": self.withdrawn_at,
            "withdraw_reason": self.withdraw_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        return cls(
            id=d["id"], org_id=d["org_id"], summary=d["summary"],
            target_college_org_id=d.get("target_college_org_id"),
            status=d.get("status", INTENT_ACTIVE), created_at=_ts(d, "created_at"),
            withdrawn_at=d.get("withdrawn_at"), withdraw_reason=d.get("withdraw_reason"),
        )


@dataclass
class Reason:
    """撮合理由（或未撮合原因），weight 为对评分的贡献分。"""

    code: str
    message: str
    weight: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "weight": self.weight,
                "details": self.details}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Reason":
        return cls(code=d["code"], message=d["message"], weight=int(d.get("weight", 0)),
                   details=dict(d.get("details", {})))


@dataclass
class Candidate:
    """带理由的候选关系，需企业与院校独立确认。"""

    id: str
    run_id: str
    demand_id: str
    enterprise_org_id: str
    college_org_id: str
    demand_version_no: int
    required_qty: int
    matched_capability_ids: list[str]
    matched_resource_ids: list[str]
    reasons: list[Reason]
    score: int
    status: str = CAND_PENDING
    enterprise_confirmation: dict[str, Any] | None = None
    college_confirmation: dict[str, Any] | None = None
    project_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    rejected_at: str | None = None
    reject_party: str | None = None
    reject_reason: str | None = None
    expired_at: str | None = None
    expire_reason: str | None = None

    def is_open(self) -> bool:
        """尚未转项目、可能被重新评估的候选。"""
        return self.status in (CAND_PENDING, CAND_CONFIRMED)

    def confirmation_of(self, party: str) -> dict[str, Any] | None:
        return self.enterprise_confirmation if party == PARTY_ENTERPRISE else self.college_confirmation

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "run_id": self.run_id, "demand_id": self.demand_id,
            "enterprise_org_id": self.enterprise_org_id, "college_org_id": self.college_org_id,
            "demand_version_no": self.demand_version_no, "required_qty": self.required_qty,
            "matched_capability_ids": self.matched_capability_ids,
            "matched_resource_ids": self.matched_resource_ids,
            "reasons": [r.to_dict() for r in self.reasons], "score": self.score,
            "status": self.status,
            "enterprise_confirmation": self.enterprise_confirmation,
            "college_confirmation": self.college_confirmation,
            "project_id": self.project_id, "created_at": self.created_at,
            "updated_at": self.updated_at, "rejected_at": self.rejected_at,
            "reject_party": self.reject_party, "reject_reason": self.reject_reason,
            "expired_at": self.expired_at, "expire_reason": self.expire_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Candidate":
        return cls(
            id=d["id"], run_id=d["run_id"], demand_id=d["demand_id"],
            enterprise_org_id=d["enterprise_org_id"], college_org_id=d["college_org_id"],
            demand_version_no=int(d["demand_version_no"]), required_qty=int(d["required_qty"]),
            matched_capability_ids=list(d.get("matched_capability_ids", [])),
            matched_resource_ids=list(d.get("matched_resource_ids", [])),
            reasons=[Reason.from_dict(r) for r in d.get("reasons", [])],
            score=int(d["score"]), status=d.get("status", CAND_PENDING),
            enterprise_confirmation=d.get("enterprise_confirmation"),
            college_confirmation=d.get("college_confirmation"),
            project_id=d.get("project_id"),
            created_at=_ts(d, "created_at"), updated_at=_ts(d, "updated_at"),
            rejected_at=d.get("rejected_at"), reject_party=d.get("reject_party"),
            reject_reason=d.get("reject_reason"),
            expired_at=d.get("expired_at"), expire_reason=d.get("expire_reason"),
        )


@dataclass
class RejectedMatch:
    """进入评估但因硬性条件未生成候选的配对，保留拒绝理由。"""

    id: str
    run_id: str
    demand_id: str
    enterprise_org_id: str
    college_org_id: str
    reason_code: str
    reason_message: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "run_id": self.run_id, "demand_id": self.demand_id,
            "enterprise_org_id": self.enterprise_org_id, "college_org_id": self.college_org_id,
            "reason_code": self.reason_code, "reason_message": self.reason_message,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RejectedMatch":
        return cls(id=d["id"], run_id=d["run_id"], demand_id=d["demand_id"],
                   enterprise_org_id=d["enterprise_org_id"], college_org_id=d["college_org_id"],
                   reason_code=d["reason_code"], reason_message=d["reason_message"],
                   created_at=_ts(d, "created_at"))


@dataclass
class MatchRun:
    """一次撮合/重评估批次。"""

    id: str
    trigger: str
    created_at: str
    demand_ids: list[str] = field(default_factory=list)
    candidates_created: int = 0
    candidates_expired: int = 0
    rejected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "trigger": self.trigger, "created_at": self.created_at,
            "demand_ids": self.demand_ids, "candidates_created": self.candidates_created,
            "candidates_expired": self.candidates_expired, "rejected": self.rejected,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MatchRun":
        return cls(id=d["id"], trigger=d["trigger"], created_at=_ts(d, "created_at"),
                   demand_ids=list(d.get("demand_ids", [])),
                   candidates_created=int(d.get("candidates_created", 0)),
                   candidates_expired=int(d.get("candidates_expired", 0)),
                   rejected=int(d.get("rejected", 0)))


@dataclass
class Project:
    """正式项目：创建即冻结匹配依据，不再受重新评估影响。"""

    id: str
    candidate_id: str
    demand_id: str
    enterprise_org_id: str
    college_org_id: str
    frozen_basis: dict[str, Any]
    status: str = PROJECT_NEGOTIATING
    created_at: str = ""
    signed_at: str | None = None
    terminated_at: str | None = None
    terminate_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "candidate_id": self.candidate_id, "demand_id": self.demand_id,
            "enterprise_org_id": self.enterprise_org_id, "college_org_id": self.college_org_id,
            "frozen_basis": self.frozen_basis, "status": self.status,
            "created_at": self.created_at, "signed_at": self.signed_at,
            "terminated_at": self.terminated_at, "terminate_reason": self.terminate_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        return cls(
            id=d["id"], candidate_id=d["candidate_id"], demand_id=d["demand_id"],
            enterprise_org_id=d["enterprise_org_id"], college_org_id=d["college_org_id"],
            frozen_basis=dict(d.get("frozen_basis", {})),
            status=d.get("status", PROJECT_NEGOTIATING), created_at=_ts(d, "created_at"),
            signed_at=d.get("signed_at"), terminated_at=d.get("terminated_at"),
            terminate_reason=d.get("terminate_reason"),
        )


@dataclass
class TimelineEvent:
    """转换谱系上的一条事件，以 demand_id 为追溯根。"""

    id: str
    demand_id: str
    at: str
    event_type: str
    summary: str
    actor: str = "system"
    run_id: str | None = None
    refs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "demand_id": self.demand_id, "at": self.at,
                "event_type": self.event_type, "summary": self.summary, "actor": self.actor,
                "run_id": self.run_id, "refs": self.refs}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TimelineEvent":
        return cls(id=d["id"], demand_id=d["demand_id"], at=d["at"],
                   event_type=d["event_type"], summary=d["summary"],
                   actor=d.get("actor", "system"), run_id=d.get("run_id"),
                   refs=dict(d.get("refs", {})))
