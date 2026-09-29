"""应用服务层：撮合业务的全部用例编排。

职责：
- 保存机构能力、需求版本、排除条件、资源容量与授权联系人；
- 生成带理由的候选关系并让双方独立确认；
- 在撤回意向、联系人失效、需求变更、资源被占用时重新评估尚未签约的候选；
- 转正式项目时冻结匹配依据；
- 为每个意向维护从最初登记到签约或终止原因的完整谱系。
"""
from __future__ import annotations

import itertools
from typing import Any

from . import engine
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    Candidate,
    CandidateStatus,
    Capability,
    Confirmation,
    ConfirmSide,
    Contact,
    ContactStatus,
    DemandVersion,
    Exclusion,
    Intent,
    IntentStatus,
    Message,
    Organization,
    OrgKind,
    Project,
    ProjectStatus,
    ResourceBatch,
    ResourceStatus,
)
from .repository import Repository


class Service:
    def __init__(self, repo: Repository | None = None) -> None:
        self.repo = repo or Repository()
        self._counter = itertools.count(1)

    # ======================================================================
    # 基础工具
    # ======================================================================

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter):04d}"

    def _get_org(self, org_id: str) -> Organization:
        org = self.repo.organizations.get(org_id)
        if org is None:
            raise NotFoundError(f"机构不存在：{org_id}")
        return org

    def _get_intent(self, intent_id: str) -> Intent:
        intent = self.repo.intents.get(intent_id)
        if intent is None:
            raise NotFoundError(f"意向不存在：{intent_id}")
        return intent

    def _get_candidate(self, candidate_id: str) -> Candidate:
        candidate = self.repo.candidates.get(candidate_id)
        if candidate is None:
            raise NotFoundError(f"候选不存在：{candidate_id}")
        return candidate

    def _get_contact(self, contact_id: str) -> Contact:
        contact = self.repo.contacts.get(contact_id)
        if contact is None:
            raise NotFoundError(f"联系人不存在：{contact_id}")
        return contact

    def _intent_for_org(self, candidate: Candidate, org_id: str) -> str | None:
        """返回该机构在候选中对应的意向 ID（无论供给方还是需求方）。"""
        for intent_id in (candidate.supply_intent_id, candidate.demand_intent_id):
            if self.repo.intents[intent_id].org_id == org_id:
                return intent_id
        return None

    def _emit(self, candidate: Candidate, subject: str, body: str) -> list[Message]:
        """给候选双方当前有效的授权联系人各发一条跟进通知。"""
        messages: list[Message] = []
        for org_id in (candidate.school_id, candidate.enterprise_id):
            for contact in self.repo.contacts_of(org_id):
                if not contact.is_authorized():
                    continue
                msg = Message(
                    message_id=self._next_id("msg"),
                    candidate_id=candidate.candidate_id,
                    recipient_contact_id=contact.contact_id,
                    recipient_org_id=org_id,
                    channel="email",
                    subject=subject,
                    body=body,
                    intent_id=self._intent_for_org(candidate, org_id),
                )
                self.repo.messages[msg.message_id] = msg
                messages.append(msg)
        return messages

    # ======================================================================
    # 登记：机构、联系人、能力、资源、排除条件
    # ======================================================================

    def register_organization(
        self, org_id: str, name: str, kind: str, *, created_from_intent_id: str | None = None
    ) -> Organization:
        if org_id in self.repo.organizations:
            raise ConflictError(f"机构已存在：{org_id}")
        try:
            org_kind = OrgKind(kind)
        except ValueError as exc:
            raise ValidationError(f"未知机构类型：{kind}") from exc
        if not name:
            raise ValidationError("机构名称不能为空")
        org = Organization(
            org_id=org_id,
            name=name,
            kind=org_kind,
            created_from_intent_id=created_from_intent_id,
        )
        self.repo.organizations[org_id] = org
        self.repo.record("organization_registered", {"org_id": org_id, "kind": kind})
        return org

    def authorize_contact(
        self, org_id: str, name: str, email: str, phone: str = "", *, contact_id: str | None = None
    ) -> Contact:
        self._get_org(org_id)
        if not name or not email:
            raise ValidationError("联系人姓名与邮箱不能为空")
        if "@" not in email:
            raise ValidationError("联系人邮箱格式不合法")
        contact_id = contact_id or self._next_id("contact")
        contact = Contact(
            contact_id=contact_id, org_id=org_id, name=name, email=email, phone=phone
        )
        self.repo.contacts[contact_id] = contact
        self.repo.organizations[org_id].contact_ids.append(contact_id)
        self.repo.record(
            "contact_authorized",
            {"org_id": org_id, "contact_id": contact_id, "email": email},
        )
        return contact

    def revoke_contact(self, contact_id: str, reason: str) -> dict[str, Any]:
        """联系人失效：作废联系人并重新评估涉及机构的全部未签约候选。"""
        contact = self._get_contact(contact_id)
        if not contact.is_authorized():
            raise ConflictError(f"联系人已失效：{contact_id}")
        contact.status = ContactStatus.REVOKED
        contact.version += 1
        self.repo.record(
            "contact_revoked",
            {"contact_id": contact_id, "org_id": contact.org_id, "reason": reason},
        )
        affected = self.repo.open_candidates_involving(contact.org_id)
        results = [self._invalidate_candidate(c, f"授权联系人 {contact_id} 失效：{reason}") for c in affected]
        return {
            "revoked_contact": contact.to_dict(),
            "reevaluated_candidates": [r["candidate_id"] for r in results if r],
        }

    def register_capability(
        self,
        org_id: str,
        resource_type: str,
        scale: int = 0,
        region: str = "",
        detail: str = "",
        *,
        capability_id: str | None = None,
    ) -> Capability:
        self._get_org(org_id)
        if not resource_type:
            raise ValidationError("资源类型不能为空")
        if scale < 0:
            raise ValidationError("能力规模不能为负")
        capability_id = capability_id or self._next_id("cap")
        cap = Capability(
            capability_id=capability_id,
            org_id=org_id,
            resource_type=resource_type,
            scale=scale,
            region=region,
            detail=detail,
        )
        self.repo.capabilities[capability_id] = cap
        self.repo.record(
            "capability_registered",
            {"org_id": org_id, "capability_id": capability_id, "resource_type": resource_type},
        )
        return cap

    def register_resource(
        self,
        owner_id: str,
        resource_type: str,
        name: str,
        capacity: int,
        *,
        resource_id: str | None = None,
    ) -> ResourceBatch:
        self._get_org(owner_id)
        if capacity <= 0:
            raise ValidationError("资源容量必须为正整数")
        resource_id = resource_id or self._next_id("res")
        batch = ResourceBatch(
            resource_id=resource_id,
            owner_id=owner_id,
            resource_type=resource_type,
            name=name,
            capacity=capacity,
        )
        self.repo.resources[resource_id] = batch
        self.repo.record(
            "resource_registered",
            {"resource_id": resource_id, "owner_id": owner_id, "capacity": capacity},
        )
        return batch

    def occupy_resource(self, resource_id: str, project_id: str, reserved: int) -> dict[str, Any]:
        """外部占用资源（如其他渠道签约）：封存容量并重评估依赖该资源的候选。"""
        resource = self.repo.resources.get(resource_id)
        if resource is None:
            raise NotFoundError(f"资源不存在：{resource_id}")
        if reserved <= 0 or reserved > resource.capacity:
            raise ValidationError("占用容量必须在 1 到总容量之间")
        resource.reserved = reserved
        if resource.available_capacity() <= 0:
            resource.status = ResourceStatus.OCCUPIED
            resource.occupied_by_project_id = project_id
        self.repo.record(
            "resource_occupied",
            {
                "resource_id": resource_id,
                "project_id": project_id,
                "reserved": reserved,
                "available": resource.available_capacity(),
            },
        )
        affected = self.repo.open_candidates_using_resource(resource_id)
        results = []
        for c in affected:
            # 剩余容量仍满足候选规模的，不受影响；否则立即失效
            if resource.available_capacity() < c.required_scale:
                results.append(
                    self._invalidate_candidate(
                        c,
                        f"资源 {resource.name} 被项目 {project_id} 占用，"
                        f"剩余 {resource.available_capacity()} 小于需求 {c.required_scale}",
                    )
                )
        return {
            "resource": resource.to_dict(),
            "reevaluated_candidates": [r["candidate_id"] for r in results if r],
        }

    def add_exclusion(
        self,
        org_id: str,
        reason: str,
        scope: str = "org",
        target_org_id: str | None = None,
        *,
        exclusion_id: str | None = None,
    ) -> Exclusion:
        self._get_org(org_id)
        if scope not in ("org", "global"):
            raise ValidationError("排除范围只能是 org 或 global")
        if scope == "org" and not target_org_id:
            raise ValidationError("机构级排除必须指定 target_org_id")
        if not reason:
            raise ValidationError("排除原因不能为空")
        exclusion_id = exclusion_id or self._next_id("excl")
        exclusion = Exclusion(
            exclusion_id=exclusion_id,
            org_id=org_id,
            reason=reason,
            scope=scope,
            target_org_id=target_org_id,
        )
        self.repo.exclusions[exclusion_id] = exclusion
        self.repo.record(
            "exclusion_added",
            {
                "exclusion_id": exclusion_id,
                "org_id": org_id,
                "scope": scope,
                "target_org_id": target_org_id,
                "reason": reason,
            },
        )
        # 新增排除条件立即影响尚未签约的候选
        if scope == "global":
            affected = self.repo.open_candidates_involving(org_id)
        else:
            affected = [
                c
                for c in self.repo.open_candidates_involving(org_id)
                if target_org_id in (c.school_id, c.enterprise_id)
            ]
        for c in affected:
            self._invalidate_candidate(c, f"新增排除条件：{reason}")
        return exclusion

    # ======================================================================
    # 意向与需求版本
    # ======================================================================

    def register_intent(
        self,
        org_id: str,
        title: str,
        direction: str,
        demand: dict[str, Any],
        *,
        intent_id: str | None = None,
    ) -> Intent:
        """登记最初合作意向，demand 即需求第 1 版（不可变快照）。"""
        self._get_org(org_id)
        if direction not in ("supply", "demand"):
            raise ValidationError("意向方向只能是 supply 或 demand")
        intent_id = intent_id or self._next_id("intent")
        intent = Intent(
            intent_id=intent_id,
            org_id=org_id,
            title=title,
            direction=direction,
            demand_versions=[self._make_demand_version(1, demand)],
        )
        self.repo.intents[intent_id] = intent
        self.repo.record(
            "intent_registered",
            {
                "intent_id": intent_id,
                "org_id": org_id,
                "direction": direction,
                "title": title,
                "demand_version": 1,
            },
            intent_ids=[intent_id],
        )
        return intent

    @staticmethod
    def _make_demand_version(version: int, demand: dict[str, Any]) -> DemandVersion:
        types = demand.get("resource_types") or []
        if isinstance(types, str):
            types = [types]
        if not types:
            raise ValidationError("需求至少包含一种资源类型")
        scale = int(demand.get("scale", 0))
        if scale <= 0:
            raise ValidationError("需求规模必须为正整数")
        return DemandVersion(
            version=version,
            resource_types=list(types),
            scale=scale,
            region=str(demand.get("region", "")),
            term=str(demand.get("term", "")),
            detail=str(demand.get("detail", "")),
        )

    def revise_demand(self, intent_id: str, demand: dict[str, Any], note: str = "") -> dict[str, Any]:
        """需求变更：追加不可变新版本，并接替/重评估尚未签约的候选。

        - 引用旧版本的未签约候选标记为 SUPERSEDED（被接替）；
        - 随后用新版本重新撮合，生成新一代候选；
        - 已签约项目的冻结依据不受影响。
        """
        intent = self._get_intent(intent_id)
        if not intent.is_open():
            raise ConflictError(f"意向 {intent_id} 非开放状态，不能变更需求")
        new_version_no = len(intent.demand_versions) + 1
        new_version = self._make_demand_version(new_version_no, demand)
        intent.demand_versions.append(new_version)
        self.repo.record(
            "demand_revised",
            {
                "intent_id": intent_id,
                "old_version": new_version_no - 1,
                "new_version": new_version_no,
                "note": note,
            },
            intent_ids=[intent_id],
        )

        stale = [
            c
            for c in self.repo.candidates.values()
            if c.is_open() and intent_id in (c.supply_intent_id, c.demand_intent_id)
        ]
        superseded_ids: list[str] = []
        for c in stale:
            c.status = CandidateStatus.SUPERSEDED
            c.termination_reason = f"需求变更：意向 {intent_id} 升级到版本 {new_version_no}"
            self.repo.record(
                "candidate_superseded",
                {"candidate_id": c.candidate_id, "reason": c.termination_reason},
                intent_ids=[c.supply_intent_id, c.demand_intent_id],
                candidate_ids=[c.candidate_id],
            )
            self._emit(
                c,
                f"【候选作废】{c.candidate_id} 所依据的需求已改版",
                f"您确认所依据的需求版本已被版本 {new_version_no} 接替，"
                f"原候选 {c.candidate_id} 自动作废，系统将基于新需求重新撮合。原因：{note}",
            )
            superseded_ids.append(c.candidate_id)

        regenerated = self.run_matching()
        # 把接替关系精确接到“同一对意向 + 同一资源类型”的新候选上
        new_candidates = [self.repo.candidates[m["candidate_id"]] for m in regenerated]
        for old in self.repo.candidates.values():
            if old.candidate_id not in superseded_ids or old.superseded_by:
                continue
            for new in new_candidates:
                if (
                    new.is_open()
                    and {new.supply_intent_id, new.demand_intent_id}
                    == {old.supply_intent_id, old.demand_intent_id}
                    and new.resource_type == old.resource_type
                ):
                    old.superseded_by = new.candidate_id
                    break

        return {
            "intent_id": intent_id,
            "new_version": new_version.to_dict(),
            "superseded_candidates": superseded_ids,
            "regenerated_candidates": regenerated,
        }

    def withdraw_intent(self, intent_id: str, reason: str) -> dict[str, Any]:
        """撤回意向：关闭意向并重评估所有尚未签约的相关候选。"""
        intent = self._get_intent(intent_id)
        if not intent.is_open():
            raise ConflictError(f"意向 {intent_id} 已处于 {intent.status.value} 状态")
        intent.status = IntentStatus.WITHDRAWN
        intent.withdrawal_reason = reason
        self.repo.record(
            "intent_withdrawn",
            {"intent_id": intent_id, "reason": reason},
            intent_ids=[intent_id],
        )
        affected = [
            c
            for c in self.repo.candidates.values()
            if c.is_open() and intent_id in (c.supply_intent_id, c.demand_intent_id)
        ]
        results = [self._invalidate_candidate(c, f"意向 {intent_id} 撤回：{reason}") for c in affected]
        return {
            "intent": intent.to_dict(),
            "invalidated_candidates": [r["candidate_id"] for r in results if r],
        }

    # ======================================================================
    # 撮合与候选生命周期
    # ======================================================================

    def run_matching(self) -> list[dict[str, Any]]:
        """基于当前全部事实重新生成候选，跳过已存在的等价候选。"""
        outcomes = engine.enumerate_matches(self.repo)
        created: list[dict[str, Any]] = []
        for outcome in outcomes:
            if not outcome.eligible:
                continue
            candidate = self._build_candidate(outcome)
            if candidate is None:
                continue
            created.append(candidate.to_dict())
        created.sort(key=lambda c: (-c["score"], c["candidate_id"]))
        self.repo.record(
            "matching_run",
            {"created": [c["candidate_id"] for c in created], "count": len(created)},
        )
        return created

    def _build_candidate(self, outcome: engine.MatchOutcome) -> Candidate | None:
        supply = self.repo.intents[outcome.supply_intent_id]
        demand = self.repo.intents[outcome.demand_intent_id]
        supply_org = self.repo.organizations[supply.org_id]
        demand_org = self.repo.organizations[demand.org_id]

        # 撮合只在院校与企业之间进行；供给方决定 school/enterprise 归属
        if supply_org.kind is OrgKind.SCHOOL and demand_org.kind is OrgKind.ENTERPRISE:
            school_id, enterprise_id = supply.org_id, demand.org_id
            supply_intent_id, demand_intent_id = supply.intent_id, demand.intent_id
        elif supply_org.kind is OrgKind.ENTERPRISE and demand_org.kind is OrgKind.SCHOOL:
            school_id, enterprise_id = demand.org_id, supply.org_id
            supply_intent_id, demand_intent_id = supply.intent_id, demand.intent_id
        else:
            return None

        # 去重：同一对院校/企业 + 同一资源类型 + 同一需求版本组合只保留一个开放候选
        for existing in self.repo.candidates.values():
            if (
                existing.is_open()
                and existing.school_id == school_id
                and existing.enterprise_id == enterprise_id
                and existing.resource_type == outcome.resource_type
                and existing.supply_demand_version == supply.current_demand().version
                and existing.demand_demand_version == demand.current_demand().version
            ):
                return None

        assert outcome.resource_id is not None
        candidate = Candidate(
            candidate_id=self._next_id("cand"),
            school_id=school_id,
            enterprise_id=enterprise_id,
            supply_intent_id=supply_intent_id,
            demand_intent_id=demand_intent_id,
            resource_type=outcome.resource_type,
            required_scale=outcome.required_scale,
            offered_scale=outcome.offered_scale,
            resource_id=outcome.resource_id,
            score=outcome.score,
            reasons=list(outcome.reasons),
            supply_demand_version=supply.current_demand().version,
            demand_demand_version=demand.current_demand().version,
        )
        self.repo.candidates[candidate.candidate_id] = candidate
        self.repo.record(
            "candidate_proposed",
            {
                "candidate_id": candidate.candidate_id,
                "score": candidate.score,
                "reasons": candidate.reasons,
                "resource_id": candidate.resource_id,
            },
            intent_ids=[supply_intent_id, demand_intent_id],
            candidate_ids=[candidate.candidate_id],
        )
        self._emit(
            candidate,
            f"【新撮合候选】{candidate.candidate_id} 综合评分 {candidate.score}",
            "论坛秘书处撮合系统基于双方最新需求版本生成候选，理由：\n"
            + "\n".join(f"- {r}" for r in candidate.reasons)
            + "\n请由各自的授权联系人独立确认或拒绝。",
        )
        return candidate

    def _invalidate_candidate(self, candidate: Candidate, reason: str) -> dict[str, Any] | None:
        """重评估未通过：候选失效并通知双方，返回失效结果；已不开放则跳过。"""
        if not candidate.is_open():
            return None
        # 再用引擎做一次完整事实校验，汇总全部原因而非只看触发点
        full_reasons = engine.reevaluate_candidate(self.repo, candidate)
        final_reasons = full_reasons or [reason]
        candidate.status = CandidateStatus.INVALIDATED
        candidate.excluded = True
        candidate.exclusion_reasons = final_reasons
        candidate.termination_reason = "；".join(final_reasons)
        self.repo.record(
            "candidate_invalidated",
            {"candidate_id": candidate.candidate_id, "reasons": final_reasons},
            intent_ids=[candidate.supply_intent_id, candidate.demand_intent_id],
            candidate_ids=[candidate.candidate_id],
        )
        self._emit(
            candidate,
            f"【候选失效】{candidate.candidate_id} 不再具备撮合条件",
            "候选因以下原因自动失效，无需继续跟进：\n"
            + "\n".join(f"- {r}" for r in final_reasons),
        )
        return {"candidate_id": candidate.candidate_id, "reasons": final_reasons}

    def confirm_candidate(
        self, candidate_id: str, org_id: str, contact_id: str, *, reject: bool = False, note: str = ""
    ) -> dict[str, Any]:
        """一方独立确认/拒绝候选。双方都确认后候选进入 CONFIRMED。"""
        candidate = self._get_candidate(candidate_id)
        if not candidate.is_open():
            raise ConflictError(f"候选 {candidate_id} 当前状态 {candidate.status.value}，不能确认")
        if org_id not in (candidate.school_id, candidate.enterprise_id):
            raise ValidationError("机构不是候选的参与方")
        contact = self._get_contact(contact_id)
        if contact.org_id != org_id:
            raise ValidationError("联系人不属于该机构")
        if not contact.is_authorized():
            raise ConflictError("联系人授权已失效，不能代表机构确认")

        # 确认前即时重评估，防止在变更与确认之间出现时间差
        blocks = engine.reevaluate_candidate(self.repo, candidate)
        if blocks:
            self._invalidate_candidate(candidate, "确认时复核未通过")
            raise ConflictError("候选已在确认前失效：" + "；".join(blocks))

        decision = ConfirmSide.REJECTED if reject else ConfirmSide.CONFIRMED
        own_intent_id = self._intent_for_org(candidate, org_id)
        confirmation = Confirmation(
            side=org_id,
            contact_id=contact_id,
            decision=decision,
            reason=note,
            demand_version=(
                self.repo.intents[own_intent_id].current_demand().version
                if own_intent_id else None
            ),
        )
        candidate.confirmations[org_id] = confirmation
        self.repo.record(
            "candidate_confirmed" if not reject else "candidate_rejected_by_party",
            {
                "candidate_id": candidate_id,
                "org_id": org_id,
                "contact_id": contact_id,
                "decision": decision.value,
                "note": note,
            },
            intent_ids=[candidate.supply_intent_id, candidate.demand_intent_id],
            candidate_ids=[candidate_id],
        )

        if reject:
            candidate.status = CandidateStatus.REJECTED
            candidate.termination_reason = f"机构 {org_id} 拒绝候选：{note}"
            self._emit(
                candidate,
                f"【候选被拒绝】{candidate_id}",
                f"机构 {org_id} 的授权联系人已拒绝该候选：{note}",
            )
            return candidate.to_dict()

        if candidate.both_confirmed():
            candidate.status = CandidateStatus.CONFIRMED
            self.repo.record(
                "candidate_both_confirmed",
                {"candidate_id": candidate_id},
                intent_ids=[candidate.supply_intent_id, candidate.demand_intent_id],
                candidate_ids=[candidate_id],
            )
            self._emit(
                candidate,
                f"【双方已确认】{candidate_id} 可办理转正式项目",
                "院校与企业的授权联系人均已独立确认，撮合依据已锁定，等待秘书处办理转项目。",
            )
        else:
            self._emit(
                candidate,
                f"【等待对方确认】{candidate_id}",
                f"机构 {org_id} 已完成确认，等待另一方授权联系人独立确认。",
            )
        return candidate.to_dict()

    # ======================================================================
    # 转正式项目：冻结匹配依据
    # ======================================================================

    def convert_to_project(self, candidate_id: str, *, project_id: str | None = None) -> Project:
        candidate = self._get_candidate(candidate_id)
        if candidate.status is not CandidateStatus.CONFIRMED:
            raise ConflictError(
                f"候选 {candidate_id} 状态为 {candidate.status.value}，必须双方独立确认后才能转项目"
            )
        resource = self.repo.resources.get(candidate.resource_id)
        if resource is None or not resource.has_capacity(candidate.required_scale):
            blocks = engine.reevaluate_candidate(self.repo, candidate)
            self._invalidate_candidate(candidate, "转项目时容量复核未通过")
            raise ConflictError("转项目失败：" + "；".join(blocks or ["资源容量不足"]))

        project_id = project_id or self._next_id("proj")
        # 冻结：把签约时的需求版本、理由、确认人、容量快照全部固化
        frozen_basis = {
            "frozen_at_event_seq": self.repo._seq + 1,
            "candidate_id": candidate.candidate_id,
            "score": candidate.score,
            "reasons": list(candidate.reasons),
            "resource": resource.to_dict(),
            "required_scale": candidate.required_scale,
            "supply_demand_version": self.repo.intents[
                candidate.supply_intent_id
            ].demand_at(candidate.supply_demand_version).to_dict(),
            "demand_demand_version": self.repo.intents[
                candidate.demand_intent_id
            ].demand_at(candidate.demand_demand_version).to_dict(),
            "confirmations": {k: v.to_dict() for k, v in candidate.confirmations.items()},
        }
        project = Project(
            project_id=project_id,
            candidate_id=candidate.candidate_id,
            school_id=candidate.school_id,
            enterprise_id=candidate.enterprise_id,
            resource_id=candidate.resource_id,
            resource_type=candidate.resource_type,
            contracted_scale=candidate.required_scale,
            frozen_basis=frozen_basis,
        )
        self.repo.projects[project_id] = project

        resource.reserved += candidate.required_scale
        if resource.available_capacity() <= 0:
            resource.status = ResourceStatus.OCCUPIED
            resource.occupied_by_project_id = project_id

        # 正式项目占用容量后，竞争同一资源且剩余容量不再满足的未签约候选失效
        competing = [
            other
            for other in self.repo.open_candidates_using_resource(candidate.resource_id)
            if other.candidate_id != candidate.candidate_id
            and resource.available_capacity() < other.required_scale
        ]
        for other in competing:
            self._invalidate_candidate(
                other,
                f"资源 {resource.name} 已被正式项目 {project_id} 占用，"
                f"剩余 {resource.available_capacity()} 小于需求 {other.required_scale}",
            )

        candidate.status = CandidateStatus.CONTRACTED
        candidate.project_id = project_id
        for intent_id in (candidate.supply_intent_id, candidate.demand_intent_id):
            intent = self.repo.intents[intent_id]
            intent.status = IntentStatus.CONVERTED
            intent.converted_project_id = project_id

        self.repo.record(
            "project_created",
            {"project_id": project_id, "candidate_id": candidate_id, "frozen": True},
            intent_ids=[candidate.supply_intent_id, candidate.demand_intent_id],
            candidate_ids=[candidate_id],
            project_id=project_id,
        )
        self._emit(
            candidate,
            f"【正式项目已建立】{project_id}",
            f"候选 {candidate_id} 已转为正式项目，签约所依据的需求版本与资源容量已冻结，"
            "后续任何变更不影响本项目的成立依据。",
        )
        for msg in self.repo.messages.values():
            if msg.candidate_id == candidate.candidate_id:
                msg.project_id = project_id
        return project

    def terminate_project(self, project_id: str, reason: str) -> Project:
        """终止正式项目：记录终止原因并释放占用容量（已冻结依据仍保留）。"""
        project = self.repo.projects.get(project_id)
        if project is None:
            raise NotFoundError(f"项目不存在：{project_id}")
        if project.status is ProjectStatus.TERMINATED:
            raise ConflictError("项目已终止")
        project.status = ProjectStatus.TERMINATED
        project.termination_reason = reason

        resource = self.repo.resources.get(project.resource_id)
        if resource is not None:
            resource.reserved = max(0, resource.reserved - project.contracted_scale)
            if resource.status is ResourceStatus.OCCUPIED and resource.reserved < resource.capacity:
                resource.status = ResourceStatus.AVAILABLE
                resource.occupied_by_project_id = None

        candidate = self.repo.candidates.get(project.candidate_id)
        if candidate is not None:
            candidate.status = CandidateStatus.TERMINATED
            candidate.termination_reason = reason

        self.repo.record(
            "project_terminated",
            {"project_id": project_id, "reason": reason, "capacity_released": True},
            intent_ids=[candidate.supply_intent_id, candidate.demand_intent_id] if candidate else [],
            candidate_ids=[project.candidate_id],
            project_id=project_id,
        )
        if candidate is not None:
            self._emit(
                candidate,
                f"【项目终止】{project_id}",
                f"项目终止原因：{reason}\n冻结的签约依据仍保留备查，占用资源容量已释放。",
            )
        return project

    # ======================================================================
    # 查询与谱系追踪
    # ======================================================================

    def get_lineage(self, *, intent_id: str | None = None, candidate_id: str | None = None,
                    project_id: str | None = None) -> list[dict[str, Any]]:
        """从任一入口（意向/候选/项目）追踪完整转换谱系。"""
        events = self.repo.lineage()
        if intent_id:
            events = [e for e in events if intent_id in e.intent_ids]
        if candidate_id:
            events = [e for e in events if candidate_id in e.candidate_ids]
        if project_id:
            events = [e for e in events if e.project_id == project_id]
        return [e.to_dict() for e in events]

    def timeline(self, intent_id: str) -> dict[str, Any]:
        """一份面向使用者的追踪视图：从最初意向直到签约或终止原因。"""
        intent = self._get_intent(intent_id)
        related_candidates = [
            c for c in self.repo.candidates.values()
            if intent_id in (c.supply_intent_id, c.demand_intent_id)
        ]
        projects = []
        for c in related_candidates:
            if c.project_id and c.project_id in self.repo.projects:
                projects.append(self.repo.projects[c.project_id].to_dict())

        terminal_reason = ""
        if intent.status is IntentStatus.WITHDRAWN:
            terminal_reason = f"意向撤回：{intent.withdrawal_reason}"
        elif intent.status is IntentStatus.CONVERTED and intent.converted_project_id:
            proj = self.repo.projects[intent.converted_project_id]
            if proj.status is ProjectStatus.TERMINATED:
                terminal_reason = f"项目 {proj.project_id} 终止：{proj.termination_reason}"
            else:
                terminal_reason = f"已签约为项目 {proj.project_id}（匹配依据已冻结）"

        return {
            "intent": intent.to_dict(),
            "candidates": [c.to_dict() for c in sorted(related_candidates, key=lambda c: c.candidate_id)],
            "projects": projects,
            "lineage": self.get_lineage(intent_id=intent_id),
            "terminal_state": intent.status.value,
            "terminal_reason": terminal_reason,
        }

    def state_snapshot(self) -> dict[str, Any]:
        """秘书处总览：当前全部聚合状态。"""
        return {
            "organizations": [o.to_dict() for o in self.repo.organizations.values()],
            "contacts": [c.to_dict() for c in self.repo.contacts.values()],
            "capabilities": [c.to_dict() for c in self.repo.capabilities.values()],
            "resources": [r.to_dict() for r in self.repo.resources.values()],
            "exclusions": [e.to_dict() for e in self.repo.exclusions.values()],
            "intents": [i.to_dict() for i in self.repo.intents.values()],
            "candidates": [c.to_dict() for c in self.repo.candidates.values()],
            "projects": [p.to_dict() for p in self.repo.projects.values()],
            "messages": [m.to_dict() for m in self.repo.messages.values()],
            "lineage": [e.to_dict() for e in self.repo.lineage()],
        }
