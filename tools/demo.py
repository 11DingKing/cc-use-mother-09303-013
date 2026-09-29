"""端到端场景演示：同一批实训资源被多所院校重复承诺的问题如何被系统消除。

运行：``python3 tools/demo.py``
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matchmaking import MatchmakingService, Repository


def main() -> None:
    svc = MatchmakingService(Repository())

    # ---- 登记 --------------------------------------------------------------
    forum = svc.register_organization("forum", "国际论坛秘书处")
    ent = svc.register_organization("enterprise", "华数智能装备有限公司")
    colleges = [svc.register_organization("college", name) for name in (
        "第一职业技术学院", "第二工业职业学院", "第三机电职业学院")]

    ent_contact = svc.add_contact(ent.id, "王经理", "wang@huashu.example")
    college_contacts = [svc.add_contact(c.id, f"{c.name}联系人", f"u{i}@college.example")
                        for i, c in enumerate(colleges)]

    for c in colleges:
        svc.add_capability(c.id, "工业机器人实训中心", "智能制造",
                           ["工业机器人", "PLC"])
    # 论坛共享的同一批实训资源：仅 40 工位，企业需求 30 工位。
    pool = svc.add_resource(forum.id, "论坛共享智能制造实训中心", 40, "工位")

    intent = svc.register_intent(ent.id, "寻求工业机器人产线实训合作")
    demand = svc.create_demand(
        intent.id, title="工业机器人产线实训",
        capability_categories=["智能制造"], capability_tags=["工业机器人"],
        required_qty=30, note="论坛期间达成的初步意向")

    print("== 初始撮合：同一批资源只可能产生一个有效候选 ==")
    for c in svc.list_candidates(demand.id):
        college = next(x for x in colleges if x.id == c.college_org_id)
        print(f"  候选：{college.name}，评分 {c.score}，预留 {c.required_qty} 工位")
        for r in c.reasons:
            print(f"    - [{r.code}] {r.message}")
    rejected = [r for r in svc.repo.rejected_matches.values() if r.demand_id == demand.id]
    for r in rejected:
        college = next(x for x in colleges if x.id == r.college_org_id)
        print(f"  未撮合：{college.name} —— {r.reason_message}")

    # ---- 双方独立确认 -------------------------------------------------------
    cand = svc.list_candidates(demand.id)[0]
    winner = next(x for x in colleges if x.id == cand.college_org_id)
    winner_contact = next(k for k in college_contacts if k.org_id == winner.id)
    svc.confirm_candidate(cand.id, "enterprise", ent_contact.id, note="企业认可方案")
    print("\n== 企业单方确认后候选仍为 pending，避免跟进邮件冲突 ==")
    print(f"  状态：{cand.status}（院校尚未确认）")
    svc.confirm_candidate(cand.id, "college", winner_contact.id, note="院校确认排期")
    print(f"  双方独立确认后：{cand.status}")

    # ---- 资源被占用触发重评估（此处演示联系人失效路径，项目冻结后不受影响）----
    project = svc.convert_to_project(cand.id)
    print("\n== 转正式项目：匹配依据冻结 ==")
    print(f"  项目 {project.id} 状态：{project.status}（洽谈中）")
    svc.invalidate_contact(ent_contact.id, "人事变动邮箱失效")
    print("  企业联系人失效，但项目依据已冻结，状态仍为：", project.status)
    svc.sign_project(project.id)
    print("  签约完成：", project.status)

    print("\n== 转换谱系：从最初意向到签约 ==")
    trace = svc.trace_intent(intent.id)
    for ev in trace["demands"][0]["timeline"]:
        print(f"  {ev['at']}  {ev['event_type']:<24} {ev['summary']}")

    print("\n冻结依据中保存的双边确认与资源占用：")
    basis = trace["demands"][0]["projects"][0]["frozen_basis"]
    print(json.dumps({"confirmations": basis["confirmations"],
                      "allocations": basis["resource_allocations"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
