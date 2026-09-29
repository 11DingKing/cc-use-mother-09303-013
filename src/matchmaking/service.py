"""应用服务层：登记、撮合、双边确认、重评估、转项目冻结与谱系追踪。

所有写操作在 ``repo.lock`` 内执行并落盘；每个状态变化都会写时间线事件，
从而支持从最初意向一路追踪到签约或终止原因。
"""
from __future__ import annotations

from typing import Any

from . import engine
from .errors import ConflictError, NotFoundError, StateError, ValidationError
from .models import (
    ALLOC_COMMITTED, ALLOC_RELEASED, CAND_CONFIRMED, CAND_CONVERTED, CAND_EXPIRED,
    CAND_PENDING, CAND_REJECTED, CONTACT_ACTIVE, CONTACT_INVALID,
    DEMAND_ACTIVE, DEMAND_WITHDRAWN, INTENT_ACTIVE, INTENT_WITHDRAWN,
    ORG_COLLEGE, ORG_ENTERPRISE, PARTY_COLLEGE, PARTY_ENTERPRISE,
    PROJECT_ACTIVE, PROJECT_NEGOTIATING, PROJECT_TERMINATED,
    VERSION_ACTIVE, VERSION_SUPERSEDED,
    Allocation, Candidate, Capability, Contact, Demand, DemandVersion, Exclusion,
    Intent, MatchRun, Organization, Project, RejectedMatch, Resource, TimelineEvent,
)
from .repository import Repository
from .utils import Clock, default_clock, new_id

_EXPIRE_MESSAGES = {
    engine.EXPIRE_INTENT_WITHDRAWN: "企业已撤回合作意向",
    engine.EXPIRE_DEMAND_WITHDRAWN: "需求已被撤回",
    engine.EXPIRE_DEMAND_REVISED: "需求发生变更，候选依据的旧版本失效",
    engine.EXPIRE_ENTERPRISE_CONTACT: "企业侧授权联系人已失效",
    engine.EXPIRE_COLLEGE_CONTACT: "院校侧授权联系人已失效",
    engine.EXPIRE_EXCLUSION: "双方新增了生效中的排除条件",
    engine.EXPIRE_CAPACITY: "实训资源被占用，剩余容量不再满足需求",
}


class MatchmakingService:
    def __init__(self, repo: Repository, clock: Clock = default_clock) -> None:
        self.repo = repo
        self.clock = clock

    # ---- 内部工具 --------------------------------------------------------

    def _event(self, demand_id: str, event_type: str, summary: str,
               actor: str = "system", run_id: str | None = None,
               refs: dict[str, Any] | None = None) -> TimelineEvent:
        ev = TimelineEvent(id=new_id("evt"), demand_id=demand_id, at=self.clock(),
                           event_type=event_type, summary=summary, actor=actor,
                           run_id=run_id, refs=refs or {})
        self.repo.timeline.append(ev)
        return ev

    def _get(self, store: dict, oid: str, label: str):
        obj = store.get(oid)
        if obj is None:
            raise NotFoundError(f"{label}不存在：{oid}")
        return obj

    def _org(self, oid: str) -> Organization:
        return self._get(self.repo.organizations, oid, "机构")

    def _demand_versions(self, demand_id: str) -> list[DemandVersion]:
        return sorted((v for v in self.repo.demand_versions.values()
                       if v.demand_id == demand_id), key=lambda v: v.version_no)

    def _scope_for_org(self, org_id: str) -> set[str]:
        """联系人/排除条件变化时受影响的需求：本机构企业侧需求 + 与本机构有开放候选的需求。"""
        scope = {d.id for d in self.repo.demands.values() if d.org_id == org_id}
        for c in self.repo.candidates.values():
            if c.is_open() and org_id in (c.enterprise_org_id, c.college_org_id):
                scope.add(c.demand_id)
        return scope

    # ---- 登记：机构、联系人、能力、资源、排除条件 --------------------------

    def register_organization(self, kind: str, name: str) -> Organization:
        if kind not in (ORG_COLLEGE, ORG_ENTERPRISE, "forum"):
            raise ValidationError(f"未知机构类型：{kind}")
        if not name.strip():
            raise ValidationError("机构名称不能为空")
        with self.repo.lock:
            org = Organization(id=new_id("org"), kind=kind, name=name, created_at=self.clock())
            self.repo.organizations[org.id] = org
            self.repo.save()
            return org

    def add_contact(self, org_id: str, name: str, email: str,
                    authorized_demand_ids: list[str] | None = None) -> Contact:
        with self.repo.lock:
            self._org(org_id)
            if not name.strip() or "@" not in email:
                raise ValidationError("联系人姓名与有效邮箱必填")
            contact = Contact(id=new_id("contact"), org_id=org_id, name=name, email=email,
                              authorized_demand_ids=authorized_demand_ids,
                              created_at=self.clock())
            self.repo.contacts[contact.id] = contact
            org = self.repo.organizations[org_id]
            if org.kind == ORG_COLLEGE:
                scope = {d.id for d in self.repo.demands.values() if d.status == DEMAND_ACTIVE}
            else:
                scope = self._scope_for_org(org_id)
            self.repo.save()
            self._run_matching_locked("contact_added", scope)
            return contact

    def invalidate_contact(self, contact_id: str, reason: str) -> Contact:
        with self.repo.lock:
            contact = self._get(self.repo.contacts, contact_id, "联系人")
            if contact.status == CONTACT_INVALID:
                raise ConflictError("联系人已处于失效状态")
            contact.status = CONTACT_INVALID
            contact.invalidated_at = self.clock()
            contact.invalid_reason = reason or "未说明"
            scope = self._scope_for_org(contact.org_id)
            self.repo.save()
            self._run_matching_locked("contact_invalidated", scope)
            return contact

    def add_capability(self, org_id: str, name: str, category: str,
                       tags: list[str] | None = None) -> Capability:
        with self.repo.lock:
            org = self._org(org_id)
            if org.kind != ORG_COLLEGE:
                raise ValidationError("能力只能登记在职业院校名下")
            cap = Capability(id=new_id("cap"), org_id=org_id, name=name, category=category,
                             tags=tags or [], created_at=self.clock())
            self.repo.capabilities[cap.id] = cap
            self.repo.save()
            self._run_matching_locked("capability_added",
                                      {d.id for d in self.repo.demands.values()
                                       if d.status == DEMAND_ACTIVE})
            return cap

    def add_resource(self, owner_org_id: str, name: str, total_qty: int, unit: str,
                     capability_id: str | None = None) -> Resource:
        with self.repo.lock:
            self._org(owner_org_id)
            if total_qty <= 0:
                raise ValidationError("资源容量必须为正整数")
            if capability_id:
                cap = self._get(self.repo.capabilities, capability_id, "能力")
                if cap.org_id != owner_org_id:
                    raise ValidationError("资源只能挂接到本机构的能力")
            res = Resource(id=new_id("res"), owner_org_id=owner_org_id, name=name,
                           total_qty=total_qty, unit=unit, capability_id=capability_id,
                           created_at=self.clock())
            self.repo.resources[res.id] = res
            self.repo.save()
            self._run_matching_locked("resource_added",
                                      {d.id for d in self.repo.demands.values()
                                       if d.status == DEMAND_ACTIVE})
            return res

    def reduce_resource_capacity(self, resource_id: str, new_total_qty: int,
                                 reason: str) -> Resource:
        """容量下调（设备故障、场地改造等）：预留在容量之内的候选若不再满足即失效。"""
        with self.repo.lock:
            res = self._get(self.repo.resources, resource_id, "资源")
            committed = sum(al.qty for al in self.repo.allocations.values()
                            if al.resource_id == resource_id and al.status == ALLOC_COMMITTED)
            if new_total_qty < 0 or new_total_qty < committed:
                raise ConflictError(
                    f"新容量 {new_total_qty} 不能为负，且不能低于已承诺占用量 {committed}")
            res.total_qty = new_total_qty
            scope = {c.demand_id for c in self.repo.candidates.values()
                     if c.is_open() and resource_id in c.matched_resource_ids}
            scope |= {d.id for d in self.repo.demands.values() if d.status == DEMAND_ACTIVE}
            self.repo.save()
            self._run_matching_locked("resource_capacity_reduced", scope)
            return res

    def occupy_resource(self, resource_id: str, qty: int, reference: str) -> Allocation:
        """资源被候选之外的用途（已签约项目、校内占用等）正式占用。"""
        with self.repo.lock:
            res = self._get(self.repo.resources, resource_id, "资源")
            if qty <= 0:
                raise ValidationError("占用数量必须为正整数")
            if engine.available_qty(self.repo, resource_id) < qty:
                raise ConflictError(
                    f"资源 {res.name} 可用容量不足：需要 {qty}，"
                    f"剩余 {engine.available_qty(self.repo, resource_id)}")
            al = Allocation(id=new_id("alloc"), resource_id=resource_id, qty=qty,
                            status=ALLOC_COMMITTED, reference=reference, created_at=self.clock())
            self.repo.allocations[al.id] = al
            scope = {c.demand_id for c in self.repo.candidates.values()
                     if c.is_open() and resource_id in c.matched_resource_ids}
            for d in list(self.repo.demands.values()):
                if d.status == DEMAND_ACTIVE:
                    scope.add(d.id)
            self.repo.save()
            self._run_matching_locked("resource_occupied", scope)
            return al

    def add_exclusion(self, org_a_id: str, org_b_id: str, reason: str) -> Exclusion:
        with self.repo.lock:
            self._org(org_a_id)
            self._org(org_b_id)
            if org_a_id == org_b_id:
                raise ValidationError("不能对同一机构设置排除条件")
            if not reason.strip():
                raise ValidationError("排除原因必填")
            existing = next((e for e in self.repo.exclusions.values()
                             if e.active and e.involves(org_a_id, org_b_id)), None)
            if existing:
                raise ConflictError("双方已存在生效中的排除条件")
            ex = Exclusion(id=new_id("excl"), org_a_id=org_a_id, org_b_id=org_b_id,
                           reason=reason, created_at=self.clock())
            self.repo.exclusions[ex.id] = ex
            scope = self._scope_for_org(org_a_id) | self._scope_for_org(org_b_id)
            self.repo.save()
            self._run_matching_locked("exclusion_added", scope)
            return ex

    # ---- 登记：意向与需求版本 ---------------------------------------------

    def register_intent(self, enterprise_org_id: str, summary: str,
                        target_college_org_id: str | None = None) -> Intent:
        with self.repo.lock:
            org = self._org(enterprise_org_id)
            if org.kind != ORG_ENTERPRISE:
                raise ValidationError("合作意向只能由合作企业登记")
            intent = Intent(id=new_id("intent"), org_id=enterprise_org_id, summary=summary,
                            target_college_org_id=target_college_org_id,
                            created_at=self.clock())
            self.repo.intents[intent.id] = intent
            self.repo.save()
            return intent

    def create_demand(self, intent_id: str, *, title: str,
                      capability_categories: list[str], capability_tags: list[str] | None = None,
                      required_qty: int, location: str | None = None,
                      note: str = "") -> Demand:
        with self.repo.lock:
            intent = self._get(self.repo.intents, intent_id, "合作意向")
            if intent.status != INTENT_ACTIVE:
                raise StateError("意向已撤回，不能新增需求")
            return self._create_demand_locked(intent, title, capability_categories,
                                              capability_tags or [], required_qty, location, note)

    def _create_demand_locked(self, intent: Intent, title, categories, tags,
                              required_qty, location, note) -> Demand:
        if not title.strip() or not categories:
            raise ValidationError("需求标题与至少一个能力类别必填")
        if required_qty <= 0:
            raise ValidationError("需求数量必须为正整数")
        at = self.clock()
        demand = Demand(id=new_id("dem"), intent_id=intent.id, org_id=intent.org_id,
                        current_version_no=1, created_at=at)
        version = DemandVersion(id=new_id("dv"), demand_id=demand.id, version_no=1, title=title,
                                capability_categories=list(categories),
                                capability_tags=list(tags), required_qty=required_qty,
                                location=location, note=note, created_at=at)
        self.repo.demands[demand.id] = demand
        self.repo.demand_versions[version.id] = version
        self._event(demand.id, "demand_created", f"企业登记需求《{title}》v1",
                    refs={"intent_id": intent.id, "version_no": 1})
        self.repo.save()
        self._run_matching_locked("demand_created", {demand.id})
        return demand

    def revise_demand(self, demand_id: str, *, title: str,
                      capability_categories: list[str], capability_tags: list[str] | None = None,
                      required_qty: int, location: str | None = None,
                      note: str = "") -> DemandVersion:
        """需求变更：旧版本封存为 superseded，产生新版本并重评估开放候选。"""
        with self.repo.lock:
            demand = self._get(self.repo.demands, demand_id, "需求")
            if demand.status != DEMAND_ACTIVE:
                raise StateError("需求已撤回，不能修订")
            if not title.strip() or not capability_categories:
                raise ValidationError("需求标题与至少一个能力类别必填")
            if required_qty <= 0:
                raise ValidationError("需求数量必须为正整数")
            at = self.clock()
            for v in self._demand_versions(demand_id):
                if v.version_no == demand.current_version_no:
                    v.status = VERSION_SUPERSEDED
            new_no = demand.current_version_no + 1
            version = DemandVersion(id=new_id("dv"), demand_id=demand_id, version_no=new_no,
                                    title=title,
                                    capability_categories=list(capability_categories),
                                    capability_tags=list(capability_tags or []), required_qty=required_qty,
                                    location=location, note=note, created_at=at)
            demand.current_version_no = new_no
            self.repo.demand_versions[version.id] = version
            self._event(demand_id, "demand_revised",
                        f"需求变更为《{title}》v{new_no}，旧版本候选将重新评估",
                        refs={"version_no": new_no})
            self.repo.save()
            self._run_matching_locked("demand_revised", {demand_id})
            return version

    def withdraw_intent(self, intent_id: str, reason: str) -> Intent:
        with self.repo.lock:
            intent = self._get(self.repo.intents, intent_id, "合作意向")
            if intent.status != INTENT_ACTIVE:
                raise StateError("意向已处于撤回状态")
            intent.status = INTENT_WITHDRAWN
            intent.withdrawn_at = self.clock()
            intent.withdraw_reason = reason or "未说明"
            demand_ids = [d.id for d in self.repo.demands.values() if d.intent_id == intent.id]
            for d in self.repo.demands.values():
                if d.intent_id == intent.id and d.status == DEMAND_ACTIVE:
                    d.status = DEMAND_WITHDRAWN
            for did in demand_ids:
                self._event(did, "intent_withdrawn",
                            f"企业撤回合作意向：{intent.withdraw_reason}",
                            refs={"intent_id": intent.id})
            self.repo.save()
            self._run_matching_locked("intent_withdrawn", set(demand_ids))
            return intent

    def withdraw_demand(self, demand_id: str, reason: str) -> Demand:
        with self.repo.lock:
            demand = self._get(self.repo.demands, demand_id, "需求")
            if demand.status != DEMAND_ACTIVE:
                raise StateError("需求已处于撤回状态")
            demand.status = DEMAND_WITHDRAWN
            self._event(demand_id, "demand_withdrawn", f"需求被撤回：{reason or '未说明'}")
            self.repo.save()
            self._run_matching_locked("demand_withdrawn", {demand_id})
            return demand

    # ---- 撮合与重评估 -----------------------------------------------------

    def run_matching(self) -> MatchRun:
        """全量撮合：重评估所有尚未签约的候选，并为有效需求补生成候选。"""
        with self.repo.lock:
            scope = {d.id for d in self.repo.demands.values()}
            return self._run_matching_locked("manual", scope)

    def _run_matching_locked(self, trigger: str, scope: set[str]) -> MatchRun:
        at = self.clock()
        run = MatchRun(id=new_id("run"), trigger=trigger, created_at=at,
                       demand_ids=sorted(scope))
        self.repo.runs[run.id] = run

        # 1) 重评估范围内尚未签约的候选，失效即过期并释放预留。
        for cand in list(self.repo.candidates.values()):
            if cand.demand_id not in scope or not cand.is_open():
                continue
            code = engine.candidate_still_valid(self.repo, cand)
            if code:
                cand.status = CAND_EXPIRED
                cand.expired_at = at
                cand.expire_reason = code
                cand.updated_at = at
                engine.release_holds(self.repo, cand.id, at, code)
                run.candidates_expired += 1
                self._event(cand.demand_id, "candidate_expired",
                            f"与{cand.college_org_id}的候选失效：{_EXPIRE_MESSAGES[code]}",
                            run_id=run.id,
                            refs={"candidate_id": cand.id, "reason_code": code,
                                  "college_org_id": cand.college_org_id})

        # 2) 为仍有效的需求评估各院校，生成新候选（不与开放候选重复）。
        for demand in list(self.repo.demands.values()):
            if demand.id not in scope or demand.status != DEMAND_ACTIVE:
                continue
            intent = self.repo.intents.get(demand.intent_id)
            if intent is None or intent.status != INTENT_ACTIVE:
                continue
            version = next((v for v in self._demand_versions(demand.id)
                            if v.version_no == demand.current_version_no), None)
            if version is None:
                continue
            existing = engine.open_pair_candidates(self.repo, demand.id)
            blocked = engine.blocked_pair_colleges(self.repo, demand.id,
                                                   demand.current_version_no)
            for college in sorted((o for o in self.repo.organizations.values()
                                   if o.kind == ORG_COLLEGE),
                                  key=lambda o: (o.created_at, o.id)):
                if college.id in existing or college.id in blocked:
                    continue
                result = engine.evaluate_pair(self.repo, demand, version, college.id)
                if result["ok"]:
                    cand = engine.build_candidate(
                        self.repo, run_id=run.id, demand=demand, version=version,
                        college_org_id=college.id, result=result, at=at)
                    run.candidates_created += 1
                    self._event(demand.id, "candidate_created",
                                f"生成候选关系：{college.name}（综合评分 {cand.score}）",
                                run_id=run.id,
                                refs={"candidate_id": cand.id, "run_id": run.id,
                                      "college_org_id": college.id, "score": cand.score,
                                      "reasons": [r.to_dict() for r in cand.reasons]})
                else:
                    rm = RejectedMatch(
                        id=new_id("rej"), run_id=run.id, demand_id=demand.id,
                        enterprise_org_id=demand.org_id, college_org_id=college.id,
                        reason_code=result["code"], reason_message=result["message"],
                        created_at=at)
                    self.repo.rejected_matches[rm.id] = rm
                    run.rejected += 1

        self._event_scope_summary(run, scope)
        self.repo.save()
        return run

    def _event_scope_summary(self, run: MatchRun, scope: set[str]) -> None:
        if not (run.candidates_created or run.candidates_expired or run.rejected):
            return
        summary = (f"撮合批次 {run.trigger}：新增候选 {run.candidates_created}，"
                   f"失效 {run.candidates_expired}，硬性条件未通过 {run.rejected}")
        # 摘要挂到范围内第一条需求，保证批次可在时间线上检索。
        for did in sorted(scope):
            self._event(did, "match_run", summary, run_id=run.id,
                        refs={"run_id": run.id, "candidates_created": run.candidates_created,
                              "candidates_expired": run.candidates_expired,
                              "rejected": run.rejected})
            break

    # ---- 双边独立确认 -----------------------------------------------------

    def confirm_candidate(self, candidate_id: str, party: str, contact_id: str,
                          note: str = "") -> Candidate:
        """企业或院校独立确认；任一方确认前都可拒绝，双方齐备才进入 confirmed。"""
        with self.repo.lock:
            cand = self._get(self.repo.candidates, candidate_id, "候选关系")
            if party not in (PARTY_ENTERPRISE, PARTY_COLLEGE):
                raise ValidationError("确认方必须是 enterprise 或 college")
            if not cand.is_open():
                raise StateError(f"候选当前状态 {cand.status}，不可确认")
            contact = self._get(self.repo.contacts, contact_id, "联系人")
            if contact.status != CONTACT_ACTIVE:
                raise ConflictError("联系人已失效，不能代表确认")
            if party == PARTY_ENTERPRISE:
                if contact.org_id != cand.enterprise_org_id:
                    raise ConflictError("联系人不属于候选企业")
                demand = self._get(self.repo.demands, cand.demand_id, "需求")
                if not contact.covers(demand.id):
                    raise ConflictError("联系人未获授权确认该需求")
            else:
                if contact.org_id != cand.college_org_id:
                    raise ConflictError("联系人不属于候选院校")
            if cand.confirmation_of(party) is not None:
                raise ConflictError(f"{party} 已确认过该候选")

            snapshot = {"party": party, "contact_id": contact.id, "contact_name": contact.name,
                        "email": contact.email, "note": note, "at": self.clock()}
            if party == PARTY_ENTERPRISE:
                cand.enterprise_confirmation = snapshot
            else:
                cand.college_confirmation = snapshot
            cand.updated_at = self.clock()
            self._event(cand.demand_id, "candidate_confirmation",
                        f"{'企业' if party == PARTY_ENTERPRISE else '院校'}方已独立确认候选",
                        actor=contact.id,
                        refs={"candidate_id": cand.id, "party": party})
            if cand.enterprise_confirmation and cand.college_confirmation:
                cand.status = CAND_CONFIRMED
                self._event(cand.demand_id, "candidate_confirmed",
                            "双方均已确认，候选进入洽谈，可转为正式项目",
                            refs={"candidate_id": cand.id})
            self.repo.save()
            return cand

    def reject_candidate(self, candidate_id: str, party: str, reason: str) -> Candidate:
        with self.repo.lock:
            cand = self._get(self.repo.candidates, candidate_id, "候选关系")
            if party not in (PARTY_ENTERPRISE, PARTY_COLLEGE):
                raise ValidationError("拒绝方必须是 enterprise 或 college")
            if not cand.is_open():
                raise StateError(f"候选当前状态 {cand.status}，不可拒绝")
            cand.status = CAND_REJECTED
            cand.reject_party = party
            cand.reject_reason = reason or "未说明"
            cand.rejected_at = self.clock()
            cand.updated_at = cand.rejected_at
            engine.release_holds(self.repo, cand.id, cand.rejected_at, "candidate_rejected")
            self._event(cand.demand_id, "candidate_rejected",
                        f"{'企业' if party == PARTY_ENTERPRISE else '院校'}方拒绝候选：{cand.reject_reason}",
                        refs={"candidate_id": cand.id, "party": party})
            self.repo.save()
            self._run_matching_locked("candidate_rejected", {cand.demand_id})
            return cand

    # ---- 转正式项目：冻结匹配依据 ------------------------------------------

    def convert_to_project(self, candidate_id: str) -> Project:
        with self.repo.lock:
            cand = self._get(self.repo.candidates, candidate_id, "候选关系")
            if cand.status != CAND_CONFIRMED:
                raise StateError("只有双方均已确认的候选才能转为正式项目")
            demand = self._get(self.repo.demands, cand.demand_id, "需求")
            version = next((v for v in self._demand_versions(demand.id)
                            if v.version_no == cand.demand_version_no), None)
            at = self.clock()
            cand_snapshot = cand.to_dict()
            project = Project(id=new_id("proj"), candidate_id=cand.id, demand_id=demand.id,
                              enterprise_org_id=cand.enterprise_org_id,
                              college_org_id=cand.college_org_id, frozen_basis={},
                              status=PROJECT_NEGOTIATING, created_at=at)
            # 预留转为承诺占用并挂到项目，随后进入冻结快照。
            held = [al for al in self.repo.allocations.values()
                    if al.candidate_id == cand.id and al.occupies_capacity()]
            for al in held:
                al.status = ALLOC_COMMITTED
                al.project_id = project.id
            # 冻结快照：此后意向撤回、需求变更、联系人失效都不再影响项目。
            project.frozen_basis = {
                "frozen_at": at,
                "candidate": cand_snapshot,
                "demand_version": version.to_dict() if version else None,
                "confirmations": {
                    "enterprise": cand.enterprise_confirmation,
                    "college": cand.college_confirmation,
                },
                "reasons": [r.to_dict() for r in cand.reasons],
                "resource_allocations": [al.to_dict() for al in held],
            }
            self.repo.projects[project.id] = project
            cand.status = CAND_CONVERTED
            cand.project_id = project.id
            cand.updated_at = at
            self._event(demand.id, "project_created",
                        "候选转为正式项目，匹配依据已冻结，进入洽谈",
                        refs={"candidate_id": cand.id, "project_id": project.id})
            self.repo.save()
            return project

    def sign_project(self, project_id: str) -> Project:
        with self.repo.lock:
            project = self._get(self.repo.projects, project_id, "项目")
            if project.status != PROJECT_NEGOTIATING:
                raise StateError(f"项目当前状态 {project.status}，不能签约")
            project.status = PROJECT_ACTIVE
            project.signed_at = self.clock()
            self._event(project.demand_id, "project_signed", "项目已正式签约",
                        refs={"project_id": project.id})
            self.repo.save()
            return project

    def terminate_project(self, project_id: str, reason: str) -> Project:
        with self.repo.lock:
            project = self._get(self.repo.projects, project_id, "项目")
            if project.status == PROJECT_TERMINATED:
                raise StateError("项目已终止")
            at = self.clock()
            project.status = PROJECT_TERMINATED
            project.terminated_at = at
            project.terminate_reason = reason or "未说明"
            for al in self.repo.allocations.values():
                if al.project_id == project.id and al.status == ALLOC_COMMITTED:
                    al.status = ALLOC_RELEASED
                    al.released_at = at
                    al.release_reason = "project_terminated"
            self._event(project.demand_id, "project_terminated",
                        f"项目终止：{project.terminate_reason}",
                        refs={"project_id": project.id})
            self.repo.save()
            return project

    # ---- 查询与追溯 -------------------------------------------------------

    def list_candidates(self, demand_id: str | None = None,
                        status: str | None = None) -> list[Candidate]:
        with self.repo.lock:
            result = list(self.repo.candidates.values())
            if demand_id:
                result = [c for c in result if c.demand_id == demand_id]
            if status:
                result = [c for c in result if c.status == status]
            return sorted(result, key=lambda c: (c.created_at, c.id))

    def get_timeline(self, demand_id: str) -> list[TimelineEvent]:
        with self.repo.lock:
            self._get(self.repo.demands, demand_id, "需求")
            return [e for e in self.repo.timeline if e.demand_id == demand_id]

    def trace_intent(self, intent_id: str) -> dict[str, Any]:
        """从最初意向到签约/终止的完整谱系。"""
        with self.repo.lock:
            intent = self._get(self.repo.intents, intent_id, "合作意向")
            demands = [d for d in self.repo.demands.values() if d.intent_id == intent.id]
            trace_demands = []
            for d in sorted(demands, key=lambda x: x.created_at):
                versions = [v.to_dict() for v in self._demand_versions(d.id)]
                cands = [c.to_dict() for c in self.repo.candidates.values()
                         if c.demand_id == d.id]
                rejected = [r.to_dict() for r in self.repo.rejected_matches.values()
                            if r.demand_id == d.id]
                projects = [p.to_dict() for p in self.repo.projects.values()
                            if p.demand_id == d.id]
                events = [e.to_dict() for e in self.repo.timeline if e.demand_id == d.id]
                trace_demands.append({
                    "demand": d.to_dict(), "versions": versions, "candidates": cands,
                    "rejected_matches": rejected, "projects": projects, "timeline": events,
                })
            return {"intent": intent.to_dict(), "demands": trace_demands}
