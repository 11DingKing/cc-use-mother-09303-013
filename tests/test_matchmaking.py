"""撮合后端端到端场景测试：资源冲突、联系人授权、重评估、冻结与谱系追踪。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matchmaking import (
    Repository, MatchmakingService,
)
from matchmaking.models import (
    CAND_CONFIRMED, CAND_CONVERTED, CAND_EXPIRED, CAND_PENDING, CAND_REJECTED,
    PROJECT_ACTIVE, PROJECT_TERMINATED,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0

    def __call__(self) -> str:
        self.t += 1
        return f"2026-09-29T00:{self.t:02d}:00+00:00"


class MatchmakingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = MatchmakingService(Repository(), FakeClock())
        self._seed()

    def _seed(self) -> None:
        s = self.svc
        self.forum = s.register_organization("forum", "论坛秘书处")
        self.ent = s.register_organization("enterprise", "华数智能装备有限公司")
        self.c1 = s.register_organization("college", "第一职业技术学院")
        self.c2 = s.register_organization("college", "第二工业职业学院")
        self.c3 = s.register_organization("college", "第三机电职业学院")

        self.ec = s.add_contact(self.ent.id, "王经理", "wang@huashu.example")
        self.cc1 = s.add_contact(self.c1.id, "赵老师", "zhao@c1.example")
        self.cc2 = s.add_contact(self.c2.id, "钱老师", "qian@c2.example")
        self.cc3 = s.add_contact(self.c3.id, "孙老师", "sun@c3.example")

        for c in (self.c1, self.c2, self.c3):
            s.add_capability(c.id, "工业机器人实训", "智能制造",
                             ["工业机器人", "PLC"])
        # 论坛共享实训资源池：总共仅 40 工位，而需求要 30——
        # 预留机制保证不可能同时向三所院校承诺同一批资源。
        self.pool = s.add_resource(self.forum.id, "论坛共享智能制造实训中心",
                                   40, "工位")

    def _demand(self):
        s = self.svc
        intent = s.register_intent(self.ent.id, "寻求工业机器人实训合作")
        demand = s.create_demand(
            intent.id, title="工业机器人产线实训",
            capability_categories=["智能制造"],
            capability_tags=["工业机器人"], required_qty=30)
        return intent, demand

    # ---- 资源冲突：同一批资源不能被重复承诺 -------------------------------

    def test_shared_pool_cannot_be_promised_to_all_colleges(self) -> None:
        _, demand = self._demand()
        candidates = self.svc.list_candidates(demand.id)
        # 第一家候选预留 30，剩 10；后续院校容量不足被记录为未撮合。
        self.assertEqual(len(candidates), 1)
        rejected = [r for r in self.svc.repo.rejected_matches.values()
                    if r.demand_id == demand.id]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(r.reason_code == "insufficient_capacity" for r in rejected))

    def test_carries_reasons(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        codes = {r.code for r in cand.reasons}
        self.assertIn("capability_category", codes)
        self.assertIn("capability_tags", codes)
        self.assertIn("contact_authorized", codes)
        self.assertGreater(cand.score, 0)

    # ---- 双方独立确认 ------------------------------------------------------

    def test_double_sided_independent_confirmation(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.confirm_candidate(cand.id, "enterprise", self.ec.id)
        self.assertEqual(cand.status, CAND_PENDING)
        # 未授权的联系人不能代为确认
        with self.assertRaises(Exception):
            self.svc.confirm_candidate(cand.id, "college", self.cc2.id)
        self.svc.confirm_candidate(cand.id, "college", self.cc1.id)
        self.assertEqual(cand.status, CAND_CONFIRMED)
        self.assertIsNotNone(cand.enterprise_confirmation)
        self.assertIsNotNone(cand.college_confirmation)

    def test_rejection_releases_capacity_and_allows_next_college(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        first_college = cand.college_org_id
        self.svc.reject_candidate(cand.id, "college", "本学期排期冲突")
        self.assertEqual(cand.status, CAND_REJECTED)
        # 释放 30 工位后，撮合自动补上下一家院校。
        new_cands = [c for c in self.svc.list_candidates(demand.id) if c.is_open()]
        self.assertEqual(len(new_cands), 1)
        self.assertNotEqual(new_cands[0].college_org_id, first_college)

    # ---- 撤回意向：未签约候选全部失效 --------------------------------------

    def test_withdraw_intent_expires_open_candidates(self) -> None:
        intent, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.confirm_candidate(cand.id, "enterprise", self.ec.id)
        self.svc.withdraw_intent(intent.id, "企业战略调整")
        self.assertEqual(cand.status, CAND_EXPIRED)
        self.assertEqual(cand.expire_reason, "intent_withdrawn")
        # 预留容量已释放，可被其他需求使用
        from matchmaking.engine import available_qty
        self.assertEqual(available_qty(self.svc.repo, self.pool.id), 40)

    # ---- 联系人失效 --------------------------------------------------------

    def test_contact_invalidation_expires_candidate(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.invalidate_contact(self.cc1.id, "离职")
        self.assertEqual(cand.status, CAND_EXPIRED)
        self.assertEqual(cand.expire_reason, "college_contact_invalid")
        # 院校联系人恢复后，下一轮撮合可再次成候选
        self.svc.add_contact(self.c1.id, "新老师", "new@c1.example")
        self.svc.run_matching()
        reopened = [c for c in self.svc.list_candidates(demand.id) if c.is_open()]
        self.assertEqual(len(reopened), 1)

    def test_no_candidate_without_enterprise_contact(self) -> None:
        _, demand = self._demand()
        self.svc.invalidate_contact(self.ec.id, "邮箱退信")
        rejected = [r for r in self.svc.repo.rejected_matches.values()
                    if r.demand_id == demand.id and r.reason_code ==
                    "enterprise_contact_unauthorized"]
        self.assertTrue(rejected)

    # ---- 需求变更：旧版本候选失效，依据新版本重新撮合 ------------------------

    def test_demand_revision_expires_and_rematches(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        old_version = cand.demand_version_no
        self.svc.revise_demand(
            demand.id, title="工业机器人产线实训（增量版）",
            capability_categories=["智能制造"], capability_tags=["工业机器人"],
            required_qty=35, note="增加 5 工位")
        self.assertEqual(cand.status, CAND_EXPIRED)
        self.assertEqual(cand.expire_reason, "demand_revised")
        self.assertEqual(demand.current_version_no, old_version + 1)
        new_cand = next(c for c in self.svc.list_candidates(demand.id) if c.is_open())
        self.assertEqual(new_cand.demand_version_no, old_version + 1)
        self.assertEqual(new_cand.required_qty, 35)

    # ---- 资源被占用/容量下调 ------------------------------------------------

    def test_resource_occupied_expires_candidate(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        # 共享实训中心 15 个工位被其他活动占用（设备改造，总容量 40→25），
        # 候选预留的 30 工位已无法兑现 → 候选失效并释放预留。
        self.svc.reduce_resource_capacity(self.pool.id, 25, "场地改造被占用")
        self.assertEqual(cand.status, CAND_EXPIRED)
        self.assertEqual(cand.expire_reason, "resource_occupied")
        # 释放后总容量仍不足，各院校在本轮重评估中均无法成候选。
        reopened = [c for c in self.svc.list_candidates(demand.id) if c.is_open()]
        self.assertEqual(reopened, [])
        rejected = [r for r in self.svc.repo.rejected_matches.values()
                    if r.demand_id == demand.id and r.reason_code == "insufficient_capacity"]
        self.assertTrue(rejected)

    def test_external_commitment_consumes_remaining_capacity(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        # 仅剩 10 空闲工位；外部承诺占用 10 不影响已预留候选。
        self.svc.occupy_resource(self.pool.id, 10, "论坛其他活动")
        self.assertTrue(cand.is_open())
        # 候选被拒后 30 预留回到池中，自动补位候选仍可使用这 30 工位。
        first_college = cand.college_org_id
        self.svc.reject_candidate(cand.id, "enterprise", "预算冻结")
        new_cands = [c for c in self.svc.list_candidates(demand.id) if c.is_open()]
        self.assertEqual(len(new_cands), 1)
        self.assertNotEqual(new_cands[0].college_org_id, first_college)

    # ---- 排除条件 ----------------------------------------------------------

    def test_exclusion_blocks_and_expires(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.add_exclusion(self.ent.id, cand.college_org_id, "往届合作纠纷")
        self.assertEqual(cand.status, CAND_EXPIRED)
        self.assertEqual(cand.expire_reason, "exclusion_added")
        rejected = [r for r in self.svc.repo.rejected_matches.values()
                    if r.demand_id == demand.id and r.reason_code == "exclusion"]
        self.assertTrue(rejected)

    # ---- 转项目冻结匹配依据 ------------------------------------------------

    def test_conversion_freezes_basis(self) -> None:
        intent, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.confirm_candidate(cand.id, "enterprise", self.ec.id)
        self.svc.confirm_candidate(cand.id, "college", self.cc1.id)
        project = self.svc.convert_to_project(cand.id)
        self.assertEqual(cand.status, CAND_CONVERTED)
        # 冻结快照保留撮合理由、需求版本、双边确认与资源预留
        basis = project.frozen_basis
        self.assertEqual(basis["demand_version"]["version_no"], 1)
        self.assertTrue(basis["confirmations"]["enterprise"])
        self.assertTrue(basis["confirmations"]["college"])
        self.assertTrue(basis["resource_allocations"])
        # 此后意向撤回、需求变更、联系人失效均不影响已转项目
        self.svc.withdraw_intent(intent.id, "测试不应波及项目")
        self.svc.invalidate_contact(self.ec.id, "离职")
        self.assertEqual(project.status, "negotiating")
        self.svc.sign_project(project.id)
        self.assertEqual(project.status, PROJECT_ACTIVE)

    def test_termination_records_reason_and_frees_capacity(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.confirm_candidate(cand.id, "enterprise", self.ec.id)
        self.svc.confirm_candidate(cand.id, "college", self.cc1.id)
        project = self.svc.convert_to_project(cand.id)
        self.svc.sign_project(project.id)
        self.svc.terminate_project(project.id, "审批未通过")
        self.assertEqual(project.status, PROJECT_TERMINATED)
        self.assertEqual(project.terminate_reason, "审批未通过")
        from matchmaking.engine import available_qty
        self.assertEqual(available_qty(self.svc.repo, self.pool.id), 40)

    # ---- 转换谱系：从意向追踪到签约/终止 ------------------------------------

    def test_full_lineage_trace(self) -> None:
        intent, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.confirm_candidate(cand.id, "enterprise", self.ec.id)
        self.svc.confirm_candidate(cand.id, "college", self.cc1.id)
        project = self.svc.convert_to_project(cand.id)
        self.svc.sign_project(project.id)

        trace = self.svc.trace_intent(intent.id)
        demand_trace = trace["demands"][0]
        event_types = [e["event_type"] for e in demand_trace["timeline"]]
        for expected in ("demand_created", "candidate_created", "candidate_confirmation",
                         "candidate_confirmed", "project_created", "project_signed"):
            self.assertIn(expected, event_types)
        self.assertEqual(demand_trace["projects"][0]["status"], PROJECT_ACTIVE)

    def test_cannot_convert_without_both_confirmations(self) -> None:
        _, demand = self._demand()
        cand = self.svc.list_candidates(demand.id)[0]
        self.svc.confirm_candidate(cand.id, "enterprise", self.ec.id)
        with self.assertRaises(Exception):
            self.svc.convert_to_project(cand.id)


class ScopedContactTest(unittest.TestCase):
    """联系人按需求授权的场景。"""

    def test_authorization_scoped_to_demand(self) -> None:
        svc = MatchmakingService(Repository(), FakeClock())
        ent = svc.register_organization("enterprise", "企业")
        col = svc.register_organization("college", "院校")
        cap = svc.add_capability(col.id, "实训", "数控")
        svc.add_resource(col.id, "数控车间", 20, "工位", cap.id)
        college_contact = svc.add_contact(col.id, "老师", "t@col.example")
        intent = svc.register_intent(ent.id, "合作")
        demand = svc.create_demand(
            intent.id, title="数控实训", capability_categories=["数控"],
            required_qty=10)
        # 企业联系人只被授权给另一个需求 → 本需求无候选
        other = svc.create_demand(
            intent.id, title="另一个需求", capability_categories=["数控"],
            required_qty=1)
        scoped = svc.add_contact(ent.id, "受限联系人", "x@ent.example",
                                 authorized_demand_ids=[other.id])
        svc.run_matching()
        cand_demands = {c.demand_id for c in svc.list_candidates()}
        self.assertNotIn(demand.id, cand_demands)
        self.assertIn(other.id, cand_demands)


if __name__ == "__main__":
    unittest.main()
