"""撮合后端的端到端回归测试。"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matchmaking import (  # noqa: E402
    ConflictError,
    NotFoundError,
    Repository,
    Service,
    ValidationError,
)
from matchmaking.api import create_server  # noqa: E402


DEMAND = {
    "resource_types": ["数控工位"],
    "scale": 30,
    "region": "宁波",
    "term": "2026秋",
    "detail": "新型学徒制",
}


def build_world() -> Service:
    """构造标准测试世界：秘书处 + 2 企业 + 3 院校 + 共享/自有资源。"""
    svc = Service()
    svc.register_organization("SEC", "秘书处", "论坛秘书处")
    svc.register_organization("E1", "企业一", "合作企业")
    svc.register_organization("E2", "企业二", "合作企业")
    svc.register_organization("S1", "院校一", "职业院校")
    svc.register_organization("S2", "院校二", "职业院校")
    svc.register_organization("S3", "院校三", "职业院校")
    svc.authorize_contact("E1", "李经理", "li@e1.test")
    svc.authorize_contact("E2", "赵主管", "zhao@e2.test")
    svc.authorize_contact("S1", "陈老师", "chen@s1.test")
    svc.authorize_contact("S2", "林老师", "lin@s2.test")
    svc.authorize_contact("S3", "周老师", "zhou@s3.test")
    svc.register_resource("SEC", "数控工位", "共享实训中心", 40, resource_id="RSH")
    svc.register_resource("S2", "数控工位", "S2 自有车间", 60, resource_id="R2")
    for sid in ("S1", "S2", "S3"):
        svc.register_capability(sid, "数控工位", scale=40, region="宁波")
    return svc


def add_intents(svc: Service, demand_scale: int = 30):
    svc.register_intent("E1", "E1 需求", "demand", {**DEMAND, "scale": demand_scale},
                        intent_id="IE1")
    svc.register_intent("E2", "E2 需求", "demand", {**DEMAND, "scale": 20},
                        intent_id="IE2")
    for sid in ("S1", "S2", "S3"):
        svc.register_intent(sid, f"{sid} 供给", "supply",
                            {**DEMAND, "scale": 40}, intent_id=f"I{sid}")


def find_candidate(svc: Service, school: str, enterprise: str, status: str = "proposed"):
    return next(
        c for c in svc.repo.candidates.values()
        if c.school_id == school and c.enterprise_id == enterprise
        and c.status.value == status
    )


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Service()

    def test_validation_errors(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.authorize_contact("NOPE", "张三", "a@b.c")
        self.svc.register_organization("O1", "机构", "合作企业")
        with self.assertRaises(ValidationError):
            self.svc.authorize_contact("O1", "", "a@b.c")
        with self.assertRaises(ValidationError):
            self.svc.authorize_contact("O1", "张三", "bad-email")
        with self.assertRaises(ConflictError):
            self.svc.register_organization("O1", "重复", "合作企业")
        with self.assertRaises(ValidationError):
            self.svc.register_resource("O1", "工位", "零容量", 0)
        with self.assertRaises(ValidationError):
            self.svc.register_intent("O1", "意向", "demand",
                                     {"resource_types": [], "scale": 0})

    def test_capacity_and_exclusion_validation(self) -> None:
        self.svc.register_organization("O1", "机构", "合作企业")
        with self.assertRaises(ValidationError):
            self.svc.add_exclusion("O1", "原因", scope="org")  # 缺 target
        with self.assertRaises(ValidationError):
            self.svc.add_exclusion("O1", "", scope="global")


class MatchingTest(unittest.TestCase):
    def test_candidates_carry_reasons_and_score(self) -> None:
        svc = build_world()
        add_intents(svc)
        created = svc.run_matching()
        self.assertEqual(len(created), 6)  # 3 院校 × 2 企业，无排除条件
        for c in created:
            self.assertGreater(c["score"], 0)
            self.assertTrue(c["reasons"])
            self.assertIn("资源类型精确匹配", c["reasons"][0])

    def test_exclusion_blocks_pair(self) -> None:
        svc = build_world()
        svc.add_exclusion("S3", "历史违约", scope="org", target_org_id="E2")
        add_intents(svc)
        created = svc.run_matching()
        pairs = {(c["school_id"], c["enterprise_id"]) for c in created}
        self.assertNotIn(("S3", "E2"), pairs)
        self.assertEqual(len(created), 5)

    def test_enterprise_can_supply_to_school_demand(self) -> None:
        """撮合不假设院校永远供给：企业供给、院校需求时同样成立。"""
        svc = Service()
        svc.register_organization("E1", "企业", "合作企业")
        svc.register_organization("S1", "院校", "职业院校")
        ce = svc.authorize_contact("E1", "李", "li@e.test")
        cs = svc.authorize_contact("S1", "陈", "c@s.test")
        svc.register_resource("E1", "导师名额", "企业导师库", 10, resource_id="RE")
        svc.register_capability("E1", "导师名额", scale=10, region="宁波")
        svc.register_intent("E1", "提供导师", "supply",
                            {"resource_types": ["导师名额"], "scale": 10, "region": "宁波",
                             "term": "秋", "detail": ""}, intent_id="IS")
        svc.register_intent("S1", "需要导师", "demand",
                            {"resource_types": ["导师名额"], "scale": 5, "region": "宁波",
                             "term": "秋", "detail": ""}, intent_id="ID")
        created = svc.run_matching()
        self.assertEqual(len(created), 1)
        c = created[0]
        self.assertEqual(c["school_id"], "S1")
        self.assertEqual(c["enterprise_id"], "E1")
        self.assertEqual(c["supply_intent_id"], "IS")
        self.assertEqual(c["demand_intent_id"], "ID")
        # 双方独立确认与转项目同样走通，通知按机构实际意向投递
        svc.confirm_candidate(c["candidate_id"], "E1", ce.contact_id)
        svc.confirm_candidate(c["candidate_id"], "S1", cs.contact_id)
        project = svc.convert_to_project(c["candidate_id"], project_id="P1")
        self.assertEqual(project.status.value, "active")
        mails = {m.recipient_org_id: m for m in svc.repo.messages.values()
                 if m.subject.startswith("【正式项目")}
        self.assertEqual(mails["E1"].intent_id, "IS")
        self.assertEqual(mails["S1"].intent_id, "ID")

    def test_same_shared_resource_not_promised_twice(self) -> None:
        """核心冲突防线：同一批共享资源被正式项目占用后，竞争候选失效。"""
        svc = build_world()
        add_intents(svc, demand_scale=35)
        svc.run_matching()
        c_s1 = find_candidate(svc, "S1", "E1")
        c_s3 = find_candidate(svc, "S3", "E1")
        c_s2 = find_candidate(svc, "S2", "E1")
        self.assertEqual(c_s1.resource_id, "RSH")
        self.assertEqual(c_s3.resource_id, "RSH")
        self.assertEqual(c_s2.resource_id, "R2")  # 自有车间优先于共享

        contacts = {o: c.contact_id for o, c in
                    ((o, next(x for x in svc.repo.contacts_of(o)))
                     for o in ("S1", "E1"))}
        svc.confirm_candidate(c_s1.candidate_id, "S1", contacts["S1"])
        svc.confirm_candidate(c_s1.candidate_id, "E1", contacts["E1"])
        project = svc.convert_to_project(c_s1.candidate_id, project_id="P1")

        self.assertEqual(project.contracted_scale, 35)
        self.assertEqual(svc.repo.resources["RSH"].available_capacity(), 5)
        # 同样需要 35 个共享工位的 S3×E1 自动失效
        self.assertFalse(c_s3.is_open())
        self.assertEqual(c_s3.status.value, "invalidated")
        self.assertIn("剩余容量 5", c_s3.exclusion_reasons[0])
        # 使用自有车间的 S2×E1 不受影响
        self.assertTrue(c_s2.is_open())

    def test_partial_occupation_keeps_small_candidate(self) -> None:
        svc = build_world()
        add_intents(svc)  # E2 仅需 20
        svc.run_matching()
        c_e2 = find_candidate(svc, "S1", "E2")
        # 外部渠道先占用 30/40，剩 10：需要 20 的候选失效，需要更小则保留
        svc.occupy_resource("RSH", "EXT-PRJ", reserved=30)
        self.assertEqual(c_e2.status.value, "invalidated")


class ConfirmationTest(unittest.TestCase):
    def test_both_sides_must_confirm_independently(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S1", "E1")
        contact_s1 = svc.repo.contacts_of("S1")[0].contact_id
        contact_e1 = svc.repo.contacts_of("E1")[0].contact_id

        with self.assertRaises(ConflictError):
            svc.convert_to_project(c.candidate_id)  # 未确认不能转项目

        svc.confirm_candidate(c.candidate_id, "S1", contact_s1)
        self.assertEqual(c.status.value, "proposed")  # 仅一方确认
        with self.assertRaises(ValidationError):
            svc.confirm_candidate(c.candidate_id, "E1", contact_s1)  # 联系人不属于机构
        with self.assertRaises(ValidationError):
            svc.confirm_candidate(c.candidate_id, "S2", contact_s1)  # 非参与方
        svc.confirm_candidate(c.candidate_id, "E1", contact_e1)
        self.assertEqual(c.status.value, "confirmed")
        self.assertEqual(len(c.confirmations), 2)

    def test_revoked_contact_cannot_confirm(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S2", "E1")
        contact = svc.repo.contacts_of("S2")[0]
        svc.revoke_contact(contact.contact_id, "离职")
        with self.assertRaises(ConflictError):
            svc.confirm_candidate(c.candidate_id, "S2", contact.contact_id)

    def test_party_rejection_closes_candidate(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S1", "E1")
        contact = svc.repo.contacts_of("S1")[0].contact_id
        result = svc.confirm_candidate(c.candidate_id, "S1", contact,
                                       reject=True, note="档期不合")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("档期不合", result["termination_reason"])


class ReevaluationTriggerTest(unittest.TestCase):
    def test_withdraw_intent_invalidates_candidates(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S1", "E2")
        result = svc.withdraw_intent("IE2", "企业取消计划")
        self.assertIn(c.candidate_id, result["invalidated_candidates"])
        self.assertEqual(c.status.value, "invalidated")
        with self.assertRaises(ConflictError):
            svc.withdraw_intent("IE2", "重复撤回")

    def test_contact_revocation_invalidates_involved_candidates(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        targets = [find_candidate(svc, "S3", e) for e in ("E1", "E2")]
        contact = svc.repo.contacts_of("S3")[0].contact_id
        result = svc.revoke_contact(contact, "授权终止")
        for c in targets:
            self.assertIn(c.candidate_id, result["reevaluated_candidates"])
            self.assertEqual(c.status.value, "invalidated")
            self.assertTrue(any("授权联系人" in r for r in c.exclusion_reasons))

    def test_demand_revision_supersedes_and_regenerates(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        old = find_candidate(svc, "S1", "E1")
        result = svc.revise_demand(
            "IE1", {**DEMAND, "scale": 35, "detail": "扩招"}, note="规模上调")
        self.assertIn(old.candidate_id, result["superseded_candidates"])
        self.assertEqual(old.status.value, "superceded")
        self.assertTrue(old.superseded_by)
        new = svc.repo.candidates[old.superseded_by]
        self.assertEqual(new.demand_demand_version, 2)
        self.assertEqual(new.required_scale, 35)
        # 无关候选（E2 仍是 v1）保持开放，不产生重复
        self.assertTrue(find_candidate(svc, "S1", "E2").is_open())

    def test_confirmed_candidate_superseded_blocks_conversion(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S1", "E1")
        s1 = svc.repo.contacts_of("S1")[0].contact_id
        e1 = svc.repo.contacts_of("E1")[0].contact_id
        svc.confirm_candidate(c.candidate_id, "S1", s1)
        svc.confirm_candidate(c.candidate_id, "E1", e1)
        svc.revise_demand("IE1", {**DEMAND, "scale": 40})
        with self.assertRaises(ConflictError):
            svc.convert_to_project(c.candidate_id)

    def test_resource_occupied_trigger_via_service(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S3", "E1")  # 用共享资源，需 30
        svc.occupy_resource("RSH", "EXT", reserved=40)
        self.assertEqual(c.status.value, "invalidated")
        self.assertTrue(any("占用" in r for r in c.exclusion_reasons))


class ProjectFreezeTest(unittest.TestCase):
    def _sign(self, svc: Service, school: str = "S1", enterprise: str = "E1"):
        c = find_candidate(svc, school, enterprise)
        cs = svc.repo.contacts_of(school)[0].contact_id
        ce = svc.repo.contacts_of(enterprise)[0].contact_id
        svc.confirm_candidate(c.candidate_id, school, cs)
        svc.confirm_candidate(c.candidate_id, enterprise, ce)
        return svc.convert_to_project(c.candidate_id, project_id="P1"), c

    def test_basis_frozen_and_unaffected_by_later_changes(self) -> None:
        svc = build_world()
        add_intents(svc, demand_scale=30)
        svc.run_matching()
        project, candidate = self._sign(svc)
        frozen_capacity = project.frozen_basis["resource"]["capacity"]
        frozen_version = project.frozen_basis["demand_demand_version"]["version"]

        # 此后：撤回意向被拒（已转项目）、资源继续被占用、联系人变动
        with self.assertRaises(ConflictError):
            svc.withdraw_intent("IE1", "签约后不能撤回")
        svc.revoke_contact(svc.repo.contacts_of("S1")[0].contact_id, "离职")
        svc.occupy_resource("RSH", "EXT-2", reserved=40)

        self.assertEqual(project.status.value, "active")
        self.assertEqual(project.frozen_basis["resource"]["capacity"], frozen_capacity)
        self.assertEqual(
            project.frozen_basis["demand_demand_version"]["version"], frozen_version)
        self.assertEqual(candidate.status.value, "contracted")

    def test_terminate_releases_capacity_and_keeps_reason(self) -> None:
        svc = build_world()
        add_intents(svc, demand_scale=30)
        svc.run_matching()
        project, candidate = self._sign(svc)
        self.assertEqual(svc.repo.resources["RSH"].available_capacity(), 10)
        svc.terminate_project("P1", "学期顺延")
        self.assertEqual(project.status.value, "terminated")
        self.assertEqual(project.termination_reason, "学期顺延")
        self.assertEqual(candidate.status.value, "terminated")
        self.assertEqual(svc.repo.resources["RSH"].available_capacity(), 40)
        with self.assertRaises(ConflictError):
            svc.terminate_project("P1", "重复终止")


class LineageAndNotificationTest(unittest.TestCase):
    def test_timeline_from_intent_to_signature(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S1", "E1")
        cs = svc.repo.contacts_of("S1")[0].contact_id
        ce = svc.repo.contacts_of("E1")[0].contact_id
        svc.confirm_candidate(c.candidate_id, "S1", cs)
        svc.confirm_candidate(c.candidate_id, "E1", ce)
        svc.convert_to_project(c.candidate_id, project_id="P1")

        timeline = svc.timeline("IE1")
        kinds = [e["kind"] for e in timeline["lineage"]]
        self.assertEqual(kinds[0], "intent_registered")
        self.assertIn("candidate_both_confirmed", kinds)
        self.assertEqual(kinds[-1], "project_created")
        self.assertEqual(timeline["terminal_state"], "converted")
        self.assertIn("P1", timeline["terminal_reason"])
        self.assertEqual(timeline["projects"][0]["project_id"], "P1")

    def test_timeline_records_termination_reason(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S1", "E1")
        cs = svc.repo.contacts_of("S1")[0].contact_id
        ce = svc.repo.contacts_of("E1")[0].contact_id
        svc.confirm_candidate(c.candidate_id, "S1", cs)
        svc.confirm_candidate(c.candidate_id, "E1", ce)
        svc.convert_to_project(c.candidate_id, project_id="P1")
        svc.terminate_project("P1", "政策调整")
        timeline = svc.timeline("IE1")
        self.assertIn("政策调整", timeline["terminal_reason"])

    def test_revoked_contact_receives_no_further_mail(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        contact = svc.repo.contacts_of("S3")[0]
        before = [m for m in svc.repo.messages.values()
                  if m.recipient_contact_id == contact.contact_id]
        svc.revoke_contact(contact.contact_id, "离职")
        # 失效后触发的任何通知都不再投递给该联系人
        svc.withdraw_intent("IE2", "取消")
        after = [m for m in svc.repo.messages.values()
                 if m.recipient_contact_id == contact.contact_id]
        self.assertEqual(len(before), len(after))
        # 但失效通知本身会发给对方（企业联系人）
        self.assertTrue(any("失效" in m.subject for m in svc.repo.messages.values()))

    def test_lineage_filter_by_project(self) -> None:
        svc = build_world()
        add_intents(svc)
        svc.run_matching()
        c = find_candidate(svc, "S1", "E1")
        cs = svc.repo.contacts_of("S1")[0].contact_id
        ce = svc.repo.contacts_of("E1")[0].contact_id
        svc.confirm_candidate(c.candidate_id, "S1", cs)
        svc.confirm_candidate(c.candidate_id, "E1", ce)
        svc.convert_to_project(c.candidate_id, project_id="P1")
        events = svc.get_lineage(project_id="P1")
        self.assertTrue(events)
        self.assertTrue(all(e["project_id"] == "P1" for e in events))


class DeterminismTest(unittest.TestCase):
    def test_same_inputs_same_scores_and_order(self) -> None:
        def run():
            svc = build_world()
            add_intents(svc)
            return [(c["school_id"], c["enterprise_id"], c["score"])
                    for c in svc.run_matching()]
        self.assertEqual(run(), run())


class HttpApiTest(unittest.TestCase):
    def test_api_smoke(self) -> None:
        server = create_server("127.0.0.1", 0)
        port = server.server_address[1]
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(path: str, body: dict):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read())

            def get(path: str):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
                    return json.loads(resp.read())

            post("/orgs", {"org_id": "E1", "name": "企业", "kind": "合作企业"})
            post("/orgs", {"org_id": "S1", "name": "院校", "kind": "职业院校"})
            post("/orgs/S1/contacts", {"name": "陈", "email": "c@s.test"})
            post("/orgs/E1/contacts", {"name": "李", "email": "l@e.test"})
            post("/orgs/S1/resources",
                 {"resource_type": "工位", "name": "车间", "capacity": 20})
            post("/orgs/S1/capabilities",
                 {"resource_type": "工位", "scale": 20, "region": "宁波"})
            demand = {"resource_types": ["工位"], "scale": 10, "region": "宁波",
                      "term": "秋", "detail": ""}
            post("/orgs/E1/intents",
                 {"title": "需求", "direction": "demand", "demand": demand})
            post("/orgs/S1/intents",
                 {"title": "供给", "direction": "supply", "demand": {**demand, "scale": 20}})
            created = post("/matching/run", {})
            self.assertEqual(len(created), 1)
            cid = created[0]["candidate_id"]
            post(f"/candidates/{cid}/confirm",
                 {"org_id": "S1", "contact_id": next(
                     x for x in get("/snapshot")["contacts"] if x["org_id"] == "S1"
                 )["contact_id"]})
            post(f"/candidates/{cid}/confirm",
                 {"org_id": "E1", "contact_id": next(
                     x for x in get("/snapshot")["contacts"] if x["org_id"] == "E1"
                 )["contact_id"]})
            project = post(f"/candidates/{cid}/project", {"project_id": "P1"})
            self.assertEqual(project["status"], "active")
            timeline = get("/lineage?project_id=P1")
            self.assertTrue(all(e["project_id"] == "P1" for e in timeline))
            # 404 映射

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post("/candidates/nope/project", {})
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
