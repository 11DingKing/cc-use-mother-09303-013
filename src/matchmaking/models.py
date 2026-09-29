"""领域模型：机构、联系人、资源、能力、意向、候选、项目、谱系。

所有实体均为内存对象，由 ``service.Service`` 统一保存与校验。
ID 由调用方（服务层或测试）提供，领域层不生成 ID，保证撮合过程
对相同输入完全确定、可重放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 基础枚举
# ---------------------------------------------------------------------------


class OrgKind(str, Enum):
    """参与撮合的机构类型。"""

    SCHOOL = "职业院校"
    ENTERPRISE = "合作企业"
    SECRETARIAT = "论坛秘书处"


class ContactStatus(str, Enum):
    """授权联系人状态；只有 AUTHORIZED 的联系人能代表机构确认。"""

    AUTHORIZED = "authorized"
    REVOKED = "revoked"


class ResourceStatus(str, Enum):
    """实训资源批次状态。

    AVAILABLE 可参与撮合；OCCUPIED 表示容量已被正式项目占用/封存，
    正在洽谈但未签约的候选不得再占用同一批资源。
    """

    AVAILABLE = "available"
    OCCUPIED = "occupied"


class IntentStatus(str, Enum):
    """合作意向生命周期。"""

    OPEN = "open"
    WITHDRAWN = "withdrawn"
    CONVERTED = "converted"

    @property
    def label(self) -> str:
        return {
            IntentStatus.OPEN: "开放中",
            IntentStatus.WITHDRAWN: "撤回",
            IntentStatus.CONVERTED: "转正式项目",
        }[self]


class DemandField(str, Enum):
    """需求版本中受版本管理的字段。"""

    RESOURCE_TYPES = "resource_types"
    SCALE = "scale"
    REGION = "region"
    TERM = "term"
    DETAIL = "detail"


class CandidateStatus(str, Enum):
    """候选关系状态机。

    PROPOSED -> CONFIRMED(双方独立确认) -> CONTRACTED(转正式项目)
    任意未签约状态可 -> WITHDRAWN / INVALIDATED / SUPERSEDED / TERMINATED
    """

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    CONTRACTED = "contracted"
    WITHDRAWN = "withdrawn"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superceded"
    REJECTED = "rejected"
    TERMINATED = "terminated"


class ConfirmSide(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ProjectStatus(str, Enum):
    """正式项目状态。转项目即冻结匹配依据，之后只能终止，不再重新撮合。"""

    ACTIVE = "active"
    TERMINATED = "terminated"


# ---------------------------------------------------------------------------
# 机构与联系人
# ---------------------------------------------------------------------------


@dataclass
class Contact:
    """机构的授权联系人。

    秘书处登记并授权；联系人失效（REVOKED）后，由其参与确认的
    未签约候选必须重新评估，避免再向失效地址发送跟进邮件。
    """

    contact_id: str
    org_id: str
    name: str
    email: str
    phone: str = ""
    status: ContactStatus = ContactStatus.AUTHORIZED
    version: int = 1

    def is_authorized(self) -> bool:
        return self.status is ContactStatus.AUTHORIZED

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "org_id": self.org_id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "status": self.status.value,
            "version": self.version,
        }


@dataclass
class Organization:
    """撮合参与机构（职业院校 / 合作企业）。"""

    org_id: str
    name: str
    kind: OrgKind
    created_from_intent_id: str | None = None
    contact_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "name": self.name,
            "kind": self.kind.value,
            "created_from_intent_id": self.created_from_intent_id,
            "contact_ids": list(self.contact_ids),
        }


# ---------------------------------------------------------------------------
# 资源与容量
# ---------------------------------------------------------------------------


@dataclass
class ResourceBatch:
    """一批可用于实训的资源（设备/工位/师资名额等）。

    ``capacity`` 为总容量；``reserved`` 为已被正式项目冻结占用的容量。
    同一批资源一旦被正式项目占用，未签约候选之间不得重复承诺，
    这正是“一家企业被三所院校分别承诺同一批实训资源”冲突的防线。
    """

    resource_id: str
    owner_id: str
    resource_type: str
    name: str
    capacity: int
    reserved: int = 0
    status: ResourceStatus = ResourceStatus.AVAILABLE
    occupied_by_project_id: str | None = None

    def available_capacity(self) -> int:
        return max(0, self.capacity - self.reserved)

    def has_capacity(self, need: int) -> bool:
        return self.status is ResourceStatus.AVAILABLE and self.available_capacity() >= need

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "owner_id": self.owner_id,
            "resource_type": self.resource_type,
            "name": self.name,
            "capacity": self.capacity,
            "reserved": self.reserved,
            "available": self.available_capacity(),
            "status": self.status.value,
            "occupied_by_project_id": self.occupied_by_project_id,
        }


# ---------------------------------------------------------------------------
# 机构能力
# ---------------------------------------------------------------------------


@dataclass
class Capability:
    """机构能力声明：机构能提供/承接某类资源及其规模与地域。"""

    capability_id: str
    org_id: str
    resource_type: str
    scale: int = 0
    region: str = ""
    detail: str = ""
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "org_id": self.org_id,
            "resource_type": self.resource_type,
            "scale": self.scale,
            "region": self.region,
            "detail": self.detail,
            "version": self.version,
        }


# ---------------------------------------------------------------------------
# 排除条件
# ---------------------------------------------------------------------------


@dataclass
class Exclusion:
    """机构级排除条件。

    ``scope`` 为 ``org`` 时排除与 ``target_org_id`` 这一家机构撮合；
    为 ``global`` 时对所有对方机构生效（如企业不接受某类区域）。
    """

    exclusion_id: str
    org_id: str
    reason: str
    scope: str = "org"
    target_org_id: str | None = None
    active: bool = True

    def blocks(self, counterparty_id: str) -> bool:
        if not self.active:
            return False
        if self.scope == "global":
            return True
        return self.target_org_id == counterparty_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclusion_id": self.exclusion_id,
            "org_id": self.org_id,
            "reason": self.reason,
            "scope": self.scope,
            "target_org_id": self.target_org_id,
            "active": self.active,
        }


# ---------------------------------------------------------------------------
# 意向与需求版本
# ---------------------------------------------------------------------------


@dataclass
class DemandVersion:
    """不可变的需求快照。

    每次需求变更都追加一个新版本而不是覆盖旧版本；候选关系始终引用
    具体版本号，从而可以回答“当时双方确认的是哪一版需求”。
    """

    version: int
    resource_types: list[str]
    scale: int
    region: str
    term: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "resource_types": list(self.resource_types),
            "scale": self.scale,
            "region": self.region,
            "term": self.term,
            "detail": self.detail,
        }


@dataclass
class Intent:
    """论坛上登记的最初合作意向，需求以版本序列方式演进。"""

    intent_id: str
    org_id: str
    title: str
    direction: str  # supply=提供资源 / demand=需要资源
    status: IntentStatus = IntentStatus.OPEN
    demand_versions: list[DemandVersion] = field(default_factory=list)
    withdrawal_reason: str = ""
    converted_project_id: str | None = None

    def current_demand(self) -> DemandVersion:
        return self.demand_versions[-1]

    def demand_at(self, version: int) -> DemandVersion:
        return self.demand_versions[version - 1]

    def is_open(self) -> bool:
        return self.status is IntentStatus.OPEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "org_id": self.org_id,
            "title": self.title,
            "direction": self.direction,
            "status": self.status.value,
            "withdrawal_reason": self.withdrawal_reason,
            "converted_project_id": self.converted_project_id,
            "current_demand": self.current_demand().to_dict(),
            "demand_versions": [d.to_dict() for d in self.demand_versions],
        }


# ---------------------------------------------------------------------------
# 候选关系
# ---------------------------------------------------------------------------


@dataclass
class Confirmation:
    """一方的独立确认记录。双方必须分别确认，秘书处或单方都不能代签。"""

    side: str  # org_id
    contact_id: str
    decision: ConfirmSide
    reason: str = ""
    demand_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "contact_id": self.contact_id,
            "decision": self.decision.value,
            "reason": self.reason,
            "demand_version": self.demand_version,
        }


@dataclass
class Candidate:
    """带理由的候选关系（双向撮合的产物）。

    - ``score`` / ``reasons``：为什么撮合这两家；
    - ``excluded`` / ``exclusion_reasons``：为什么不再撮合（重评估时）；
    - 引用生成时双方的需求版本，转项目时冻结进项目快照；
    - 需求变更后旧候选标记 SUPERSEDED 并由新一代候选接替。
    """

    candidate_id: str
    school_id: str
    enterprise_id: str
    supply_intent_id: str
    demand_intent_id: str
    resource_type: str
    required_scale: int
    offered_scale: int
    resource_id: str
    score: int
    reasons: list[str] = field(default_factory=list)
    status: CandidateStatus = CandidateStatus.PROPOSED
    supply_demand_version: int = 0
    demand_demand_version: int = 0
    confirmations: dict[str, Confirmation] = field(default_factory=dict)
    excluded: bool = False
    exclusion_reasons: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    project_id: str | None = None
    termination_reason: str = ""

    def both_confirmed(self) -> bool:
        return (
            len(self.confirmations) == 2
            and all(c.decision is ConfirmSide.CONFIRMED for c in self.confirmations.values())
        )

    def is_open(self) -> bool:
        """尚未签约：可能等待确认或已双方确认待转项目。"""
        return self.status in (CandidateStatus.PROPOSED, CandidateStatus.CONFIRMED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "school_id": self.school_id,
            "enterprise_id": self.enterprise_id,
            "supply_intent_id": self.supply_intent_id,
            "demand_intent_id": self.demand_intent_id,
            "resource_type": self.resource_type,
            "required_scale": self.required_scale,
            "offered_scale": self.offered_scale,
            "resource_id": self.resource_id,
            "score": self.score,
            "reasons": list(self.reasons),
            "status": self.status.value,
            "supply_demand_version": self.supply_demand_version,
            "demand_demand_version": self.demand_demand_version,
            "confirmations": {k: v.to_dict() for k, v in self.confirmations.items()},
            "excluded": self.excluded,
            "exclusion_reasons": list(self.exclusion_reasons),
            "superseded_by": self.superseded_by,
            "project_id": self.project_id,
            "termination_reason": self.termination_reason,
        }


# ---------------------------------------------------------------------------
# 正式项目（冻结匹配依据）
# ---------------------------------------------------------------------------


@dataclass
class Project:
    """正式项目。创建时把撮合依据整体冻结为快照。

    冻结后，即便需求再改版、联系人再变更、资源再紧张，已签约项目
    的成立依据都不再变化；项目只能被终止，并记录终止原因。
    """

    project_id: str
    candidate_id: str
    school_id: str
    enterprise_id: str
    resource_id: str
    resource_type: str
    contracted_scale: int
    frozen_basis: dict[str, Any]
    status: ProjectStatus = ProjectStatus.ACTIVE
    termination_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "candidate_id": self.candidate_id,
            "school_id": self.school_id,
            "enterprise_id": self.enterprise_id,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "contracted_scale": self.contracted_scale,
            "frozen_basis": self.frozen_basis,
            "status": self.status.value,
            "termination_reason": self.termination_reason,
        }


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """发给授权联系人的跟进通知。

    每个收件人独立成一条消息，且消息引用具体候选与需求版本，
    从机制上避免“联系人收到互相冲突的跟进邮件”：失效联系人不再收信，
    候选失效时发出更正通知，正式通知只携带冻结后的项目依据。
    """

    message_id: str
    candidate_id: str | None
    recipient_contact_id: str
    recipient_org_id: str
    channel: str
    subject: str
    body: str
    intent_id: str | None = None
    demand_version: int | None = None
    project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "candidate_id": self.candidate_id,
            "recipient_contact_id": self.recipient_contact_id,
            "recipient_org_id": self.recipient_org_id,
            "channel": self.channel,
            "subject": self.subject,
            "body": self.body,
            "intent_id": self.intent_id,
            "demand_version": self.demand_version,
            "project_id": self.project_id,
        }
