"""端到端业务场景演示（可直接运行：python3 tools/demo_scenario.py）。

剧情：
论坛结束后，企业 E1 被三所院校 S1/S2/S3 分别承诺使用同一批秘书处共享
实训资源；同时还有第二家企业 E2。本脚本演示后端如何：
1. 保存机构能力、需求版本、排除条件、资源容量与授权联系人；
2. 生成带理由的候选，由双方授权联系人独立确认；
3. 撤回意向、联系人失效、需求变更、资源被占用时重新评估未签约候选；
4. 转正式项目后冻结匹配依据；
5. 从最初意向一直追踪到签约或终止原因。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matchmaking import Service  # noqa: E402


def show(title: str, payload) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("-" * 72)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    svc = Service()

    # --- 1. 机构与秘书处 --------------------------------------------------
    svc.register_organization("SEC", "论坛秘书处", "论坛秘书处")
    svc.register_organization("E1", "华瀚智能制造有限公司", "合作企业")
    svc.register_organization("E2", "云启数字科技有限公司", "合作企业")
    svc.register_organization("S1", "江北职业技术学院", "职业院校")
    svc.register_organization("S2", "临港工业职业学院", "职业院校")
    svc.register_organization("S3", "西河装备制造学院", "职业院校")

    # 授权联系人
    svc.authorize_contact("SEC", "秘书处王老师", "sec@forum.example")
    c_e1 = svc.authorize_contact("E1", "企业李经理", "li@e1.example")
    svc.authorize_contact("E2", "企业赵主管", "zhao@e2.example")
    c_s1 = svc.authorize_contact("S1", "S1 陈老师", "chen@s1.example")
    c_s2 = svc.authorize_contact("S2", "S2 林老师", "lin@s2.example")
    svc.authorize_contact("S3", "S3 周老师", "zhou@s3.example")

    # --- 2. 资源容量：秘书处共享的同一批实训工位（总量仅 40） -------------
    shared = svc.register_resource(
        "SEC", "数控实训工位", "论坛共享数控实训中心（一期）", capacity=40,
        resource_id="RES-SHARED-01",
    )
    svc.register_resource("S2", "数控实训工位", "S2 自有实训车间", capacity=60,
                          resource_id="RES-S2-OWN")

    # --- 3. 机构能力 -------------------------------------------------------
    for sid in ("S1", "S2", "S3"):
        svc.register_capability(sid, "数控实训工位", scale=40, region="宁波",
                                detail="可承接企业新型学徒制实训")
    svc.register_capability("E1", "数控实训工位", scale=40, region="宁波",
                            detail="可提供真实生产订单与导师")

    # --- 4. 排除条件：S3 与 E2 既往合作违约，互相排除 ----------------------
    svc.add_exclusion("S3", "2024 年实训安全协议违约，暂停合作",
                      scope="org", target_org_id="E2")

    # --- 5. 最初意向（需求第 1 版） ---------------------------------------
    demand_v1 = {
        "resource_types": ["数控实训工位"],
        "scale": 30, "region": "宁波", "term": "2026 秋季学期",
        "detail": "30 个工位用于新型学徒制，需真实产线任务",
    }
    supply_v1 = {
        "resource_types": ["数控实训工位"],
        "scale": 40, "region": "宁波", "term": "2026 秋季学期",
        "detail": "可协调共享实训中心 40 个工位",
    }
    svc.register_intent("E1", "寻求数控实训工位 30 个", "demand", demand_v1,
                        intent_id="INT-E1")
    svc.register_intent("E2", "寻求数控实训工位 20 个", "demand",
                        {**demand_v1, "scale": 20}, intent_id="INT-E2")
    svc.register_intent("S1", "提供共享数控工位", "supply", supply_v1,
                        intent_id="INT-S1")
    svc.register_intent("S2", "提供数控工位（含自有车间）", "supply", supply_v1,
                        intent_id="INT-S2")
    svc.register_intent("S3", "提供共享数控工位", "supply", supply_v1,
                        intent_id="INT-S3")

    # --- 6. 生成带理由的候选关系 ------------------------------------------
    candidates = svc.run_matching()
    pairs = sorted((c["school_id"], c["enterprise_id"]) for c in candidates)
    show("① 撮合生成候选（带评分与理由；S3×E2 被排除条件拦截，故只有 5 对）",
         {"eligible_pairs": pairs,
          "candidates": [{"candidate_id": c["candidate_id"], "school": c["school_id"],
                          "enterprise": c["enterprise_id"], "score": c["score"],
                          "resource_id": c["resource_id"], "reasons": c["reasons"]}
                         for c in candidates]})

    def find(school: str, enterprise: str, status: str = "proposed"):
        return next(c for c in svc.repo.candidates.values()
                    if c.school_id == school and c.enterprise_id == enterprise
                    and c.status.value == status)

    cand_s1_v1 = find("S1", "E1")
    cand_s3_v1 = find("S3", "E1")
    assert cand_s1_v1.resource_id == cand_s3_v1.resource_id == "RES-SHARED-01"
    # S3×E2 因排除条件从未生成候选（见 ① 的 5 对列表）

    # --- 7. 需求变更（触发点之一）：E2 需求升级 v1→v2 ----------------------
    revise_e2 = svc.revise_demand(
        "INT-E2",
        {**demand_v1, "scale": 35, "term": "2027 春季学期",
         "detail": "扩招，35 个工位，春季学期入岗"},
        note="企业扩招，规模与档期调整",
    )
    show("② E2 需求变更 v1→v2：旧候选标记 SUPERSEDED 并链接新一代候选",
         {"superseded": [
             {"candidate_id": cid,
              "superseded_by": svc.repo.candidates[cid].superseded_by}
             for cid in revise_e2["superseded_candidates"]],
          "regenerated": [
              {"candidate_id": m["candidate_id"], "school": m["school_id"],
               "enterprise": m["enterprise_id"],
               "demand_version": m["demand_demand_version"],
               "required_scale": m["required_scale"]}
              for m in revise_e2["regenerated_candidates"]]})

    # --- 8. 需求变更（触发点之二）：E1 需求升级 v1→v2（30→35） -------------
    revise_e1 = svc.revise_demand(
        "INT-E1",
        {**demand_v1, "scale": 35, "detail": "新增 5 个工位用于师资跟岗"},
        note="增加师资跟岗名额",
    )
    show("③ E1 需求变更 v1→v2：S1/S2/S3×E1 的 v1 候选全部被接替",
         {"superseded_count": len(revise_e1["superseded_candidates"]),
          "regenerated_pairs": sorted(
              (m["school_id"], m["enterprise_id"]) for m in revise_e1["regenerated_candidates"])})

    # --- 9. 双方独立确认新一代 S1×E1 ---------------------------------------
    cand_s1 = find("S1", "E1")
    cand_s3 = find("S3", "E1")
    svc.confirm_candidate(cand_s1.candidate_id, "S1", c_s1.contact_id,
                          note="院校方确认可用共享中心")
    after_school = svc.confirm_candidate(
        cand_s1.candidate_id, "E1", c_e1.contact_id, note="企业方确认产线任务与规模")
    show("④ S1×E1 双方授权联系人独立确认（秘书处/单方都不能代签）",
         {"status": after_school["status"],
          "confirmations": after_school["confirmations"]})

    # --- 10. 转正式项目：冻结匹配依据并占用共享资源 35/40 -------------------
    project = svc.convert_to_project(cand_s1.candidate_id, project_id="PRJ-001")
    show("⑤ 转正式项目 PRJ-001：需求版本/评分理由/确认人/容量快照整体冻结",
         {"project_id": project.project_id, "status": project.status.value,
          "frozen_demand_version": project.frozen_basis["demand_demand_version"]["version"],
          "frozen_score": project.frozen_basis["score"],
          "frozen_resource_snapshot": project.frozen_basis["resource"]})

    # --- 11. 资源被占用触发重评估 ------------------------------------------
    s3_now = svc.repo.candidates[cand_s3.candidate_id]
    s2_e1 = find("S2", "E1")  # S2 用自有车间，容量 60，不受共享资源影响
    show("⑥ 共享资源被正式项目占用（剩 5）→ 依赖它的 S3×E1 失效；"
         "S2×E1 用自有车间仍有效",
         {"shared_resource_live": shared.to_dict(),
          "S3xE1": {"status": s3_now.status.value, "reasons": s3_now.exclusion_reasons},
          "S2xE1": {"status": s2_e1.status.value, "resource_id": s2_e1.resource_id}})

    # --- 12. 联系人失效触发重评估：S2 联系人离职 ----------------------------
    revoke = svc.revoke_contact(c_s2.contact_id, "联系人离职，授权终止")
    s2_after = svc.repo.candidates[s2_e1.candidate_id]
    show("⑦ S2 授权联系人失效 → S2×E1 未签约候选立即失效并通知企业停止跟进",
         {"reevaluated": revoke["reevaluated_candidates"],
          "S2xE1_status": s2_after.status.value,
          "reasons": s2_after.exclusion_reasons})

    # --- 13. 撤回意向触发重评估：E2 撤回 ------------------------------------
    withdraw = svc.withdraw_intent("INT-E2", "企业经营调整，本年度取消实训计划")
    show("⑧ E2 撤回意向 → 仍未签约的 E2 候选全部失效（已失效的不重复处理）",
         withdraw)

    # --- 14. 冻结依据 vs 此后的事实变化 ------------------------------------
    show("⑨ 签约后联系人失效、资源占用、意向撤回均发生，PRJ-001 冻结依据不变",
         {"frozen_resource_in_project": project.frozen_basis["resource"],
          "live_resource_now": shared.to_dict(),
          "project_status": project.status.value})

    # --- 15. 终止项目：原因留痕、容量释放、谱系保留 -------------------------
    svc.terminate_project("PRJ-001", "院校专业调整，合作学期顺延一年后另行立项")
    show("⑩ 项目终止：终止原因可追踪，冻结依据保留，资源容量释放",
         {"project_status": svc.repo.projects["PRJ-001"].status.value,
          "termination_reason": svc.repo.projects["PRJ-001"].termination_reason,
          "resource_available_after_release": shared.available_capacity()})

    # --- 16. 从最初意向追踪到终局 ------------------------------------------
    timeline = svc.timeline("INT-E1")
    show("⑪ INT-E1 全链路追踪：最初意向 → v1/v2 需求 → 候选接替链 → 签约 → 终止",
         {"terminal_state": timeline["terminal_state"],
          "terminal_reason": timeline["terminal_reason"],
          "demand_versions": [v["version"] for v in timeline["intent"]["demand_versions"]],
          "event_kinds": [e["kind"] for e in timeline["lineage"]],
          "candidate_chain": [
              {"candidate_id": c["candidate_id"], "status": c["status"],
               "demand_version": c["demand_demand_version"],
               "superseded_by": c["superseded_by"], "project_id": c["project_id"]}
              for c in timeline["candidates"]]})

    # 通知审计：失效联系人在失效后不再收到任何跟进邮件
    mails = {}
    for m in svc.repo.messages.values():
        mails.setdefault(m.recipient_contact_id, []).append(m.subject)
    later_mails = [s for s in mails.get(c_s2.contact_id, []) if "失效" in s or "撤回" in s]
    show("⑫ 通知审计：每位联系人只收到与自己相关、版本明确的通知；"
         "失效联系人无失效后的跟进邮件",
         {"contact_mail_counts": {k: len(v) for k, v in sorted(mails.items())},
          "revoked_contact_subjects": mails.get(c_s2.contact_id, []),
          "mails_after_revocation_to_revoked_contact": later_mails})

    print("\n演示完成。")


if __name__ == "__main__":
    main()
