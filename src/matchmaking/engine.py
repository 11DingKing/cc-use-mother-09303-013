"""撮合引擎：硬性条件过滤、带理由评分、容量预留与候选存活重评估。

引擎只操作仓储数据，不写时间线；时间线与运行批次由服务层负责。
所有函数须在 ``repo.lock`` 内调用。
"""
from __future__ import annotations

from typing import Any

from .models import (
    ALLOC_HELD, ALLOC_RELEASED, CAND_CONFIRMED, CAND_PENDING, CONTACT_ACTIVE,
    DEMAND_ACTIVE, INTENT_ACTIVE,
    Allocation, Candidate, Reason,
)
from .utils import new_id

# 硬条件失败码（未生成候选的原因）
FAIL_INTENT_WITHDRAWN = "intent_withdrawn"
FAIL_DEMAND_WITHDRAWN = "demand_withdrawn"
FAIL_EXCLUSION = "exclusion"
FAIL_ENTERPRISE_CONTACT = "enterprise_contact_unauthorized"
FAIL_COLLEGE_CONTACT = "college_contact_unauthorized"
FAIL_CAPABILITY = "capability_mismatch"
FAIL_CAPACITY = "insufficient_capacity"

# 候选失效原因码
EXPIRE_INTENT_WITHDRAWN = "intent_withdrawn"
EXPIRE_DEMAND_WITHDRAWN = "demand_withdrawn"
EXPIRE_DEMAND_REVISED = "demand_revised"
EXPIRE_ENTERPRISE_CONTACT = "enterprise_contact_invalid"
EXPIRE_COLLEGE_CONTACT = "college_contact_invalid"
EXPIRE_EXCLUSION = "exclusion_added"
EXPIRE_CAPACITY = "resource_occupied"


# ---- 基础查询 -------------------------------------------------------------

def enterprise_contact_active(repo, demand) -> bool:
    for c in repo.contacts.values():
        if c.org_id == demand.org_id and c.covers(demand.id):
            return True
    return False


def college_contact_active(repo, college_org_id: str) -> bool:
    return any(c.org_id == college_org_id and c.status == CONTACT_ACTIVE
               for c in repo.contacts.values())


def exclusion_between(repo, a: str, b: str) -> str | None:
    """返回双方互斥原因，无互斥返回 None。"""
    for ex in repo.exclusions.values():
        if ex.active and ex.involves(a, b):
            return ex.reason
    return None


def occupied_qty(repo, resource_id: str, exclude_candidate_id: str | None = None) -> int:
    total = 0
    for al in repo.allocations.values():
        if al.resource_id != resource_id or not al.occupies_capacity():
            continue
        if exclude_candidate_id and al.candidate_id == exclude_candidate_id:
            continue
        total += al.qty
    return total


def available_qty(repo, resource_id: str, exclude_candidate_id: str | None = None) -> int:
    res = repo.resources[resource_id]
    return res.total_qty - occupied_qty(repo, resource_id, exclude_candidate_id)


def match_capabilities(repo, college_org_id: str, version) -> list:
    """该院校能力中命中需求类别的能力；标签交集附加分。"""
    wanted_categories = set(version.capability_categories)
    return [cap for cap in repo.capabilities.values()
            if cap.org_id == college_org_id and cap.category in wanted_categories]


def plan_resources(repo, college_org_id: str, matched_cap_ids: list[str],
                   required_qty: int, exclude_candidate_id: str | None = None
                   ) -> tuple[list[tuple[str, int]], int]:
    """贪心选取可用资源（先大后小），返回 [(resource_id, qty), ...] 与可满足总量。

    可选资源包括院校自有资源，以及论坛秘书处持有的共享实训资源池；
    共享池正是多所院校可能重复承诺的那"同一批资源"，靠候选预留防止超卖。
    """
    forum_ids = {o.id for o in repo.organizations.values() if o.kind == "forum"}
    eligible = [r for r in repo.resources.values()
                if (r.owner_org_id == college_org_id or r.owner_org_id in forum_ids)
                and (r.capability_id is None or r.capability_id in matched_cap_ids)]
    ranked = sorted(eligible, key=lambda r: available_qty(repo, r.id, exclude_candidate_id),
                    reverse=True)
    plan: list[tuple[str, int]] = []
    remaining = required_qty
    for res in ranked:
        avail = available_qty(repo, res.id, exclude_candidate_id)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        plan.append((res.id, take))
        remaining -= take
        if remaining <= 0:
            break
    return plan, max(0, required_qty - remaining)


# ---- 硬性条件评估 ----------------------------------------------------------

def evaluate_pair(repo, demand, version, college_org_id: str,
                  exclude_candidate_id: str | None = None
                  ) -> dict[str, Any]:
    """评估一个需求-院校配对，返回成功计划或带失败码的结论。"""
    if exclusion_between(repo, demand.org_id, college_org_id) is not None:
        return {"ok": False, "code": FAIL_EXCLUSION,
                "message": f"双方存在生效中的排除条件：{exclusion_between(repo, demand.org_id, college_org_id)}"}

    if not enterprise_contact_active(repo, demand):
        return {"ok": False, "code": FAIL_ENTERPRISE_CONTACT,
                "message": "企业侧没有覆盖该需求的有效授权联系人"}

    if not college_contact_active(repo, college_org_id):
        return {"ok": False, "code": FAIL_COLLEGE_CONTACT,
                "message": "院校侧没有有效授权联系人"}

    caps = match_capabilities(repo, college_org_id, version)
    if not caps:
        return {"ok": False, "code": FAIL_CAPABILITY,
                "message": f"院校能力不覆盖需求类别：{'、'.join(version.capability_categories)}"}

    cap_ids = [c.id for c in caps]
    plan, fulfilled = plan_resources(repo, college_org_id, cap_ids,
                                     version.required_qty, exclude_candidate_id)
    if fulfilled < version.required_qty:
        return {"ok": False, "code": FAIL_CAPACITY,
                "message": f"实训资源容量不足：需要 {version.required_qty}，仅可协调 {fulfilled}",
                "shortfall": version.required_qty - fulfilled}

    # ---- 评分与理由 ----
    reasons: list[Reason] = []
    score = 0
    categories_hit = sorted({c.category for c in caps})
    reasons.append(Reason("capability_category",
                          f"能力类别命中：{'、'.join(categories_hit)}", 30,
                          {"capability_ids": cap_ids, "categories": categories_hit}))
    score += 30

    wanted_tags = set(version.capability_tags)
    tag_hits = sorted({t for c in caps for t in c.tags if t in wanted_tags})
    if tag_hits:
        tag_score = min(20, 5 * len(tag_hits))
        reasons.append(Reason("capability_tags",
                              f"标签契合 {len(tag_hits)} 项：{'、'.join(tag_hits)}", tag_score,
                              {"tags": tag_hits}))
        score += tag_score

    total_avail = sum(available_qty(repo, rid, exclude_candidate_id) for rid, _ in plan)
    if total_avail >= version.required_qty * 2:
        reasons.append(Reason("capacity_slack", "可协调资源充裕（≥需求两倍）", 10,
                              {"available": total_avail, "required": version.required_qty}))
        score += 10
    else:
        reasons.append(Reason("capacity_met", "资源容量恰好满足需求", 5,
                              {"available": total_avail, "required": version.required_qty}))
        score += 5

    reasons.append(Reason("contact_authorized", "双方均有有效授权联系人", 5, {}))
    score += 5

    if not exclusion_between(repo, demand.org_id, college_org_id):
        reasons.append(Reason("no_exclusion", "双方不存在排除条件", 0, {}))

    return {"ok": True, "capability_ids": cap_ids, "plan": plan,
            "reasons": reasons, "score": score}


# ---- 容量预留 -------------------------------------------------------------

def hold_resources(repo, candidate_id: str, plan: list[tuple[str, int]], at: str) -> list[Allocation]:
    allocs: list[Allocation] = []
    for resource_id, qty in plan:
        al = Allocation(id=new_id("alloc"), resource_id=resource_id, qty=qty,
                        status=ALLOC_HELD, candidate_id=candidate_id, created_at=at)
        repo.allocations[al.id] = al
        allocs.append(al)
    return allocs


def release_holds(repo, candidate_id: str, at: str, reason: str) -> None:
    for al in repo.allocations.values():
        if al.candidate_id == candidate_id and al.status == ALLOC_HELD:
            al.status = ALLOC_RELEASED
            al.released_at = at
            al.release_reason = reason


# ---- 候选存活重评估 --------------------------------------------------------

def candidate_still_valid(repo, cand: Candidate) -> str | None:
    """返回 None 表示候选仍成立；否则返回失效原因码。"""
    demand = repo.demands.get(cand.demand_id)
    intent = repo.intents.get(demand.intent_id) if demand else None
    if intent is None or intent.status != INTENT_ACTIVE:
        return EXPIRE_INTENT_WITHDRAWN
    if demand is None or demand.status != DEMAND_ACTIVE:
        return EXPIRE_DEMAND_WITHDRAWN
    if demand.current_version_no != cand.demand_version_no:
        return EXPIRE_DEMAND_REVISED
    if exclusion_between(repo, cand.enterprise_org_id, cand.college_org_id) is not None:
        return EXPIRE_EXCLUSION
    if not enterprise_contact_active(repo, demand):
        return EXPIRE_ENTERPRISE_CONTACT
    if not college_contact_active(repo, cand.college_org_id):
        return EXPIRE_COLLEGE_CONTACT

    version = next((v for v in repo.demand_versions.values()
                    if v.demand_id == demand.id and v.version_no == cand.demand_version_no), None)
    if version is None:
        return EXPIRE_DEMAND_REVISED
    # 资源被其他候选/项目占用后，本候选预留之外的容量仍须满足需求。
    result = evaluate_pair(repo, demand, version, cand.college_org_id,
                           exclude_candidate_id=cand.id)
    if not result["ok"] and result["code"] == FAIL_CAPACITY:
        return EXPIRE_CAPACITY
    return None


def open_pair_candidates(repo, demand_id: str) -> set[str]:
    """该需求当前仍开放的院校配对，避免重复生成候选。"""
    return {c.college_org_id for c in repo.candidates.values()
            if c.demand_id == demand_id and c.status in (CAND_PENDING, CAND_CONFIRMED)}


def blocked_pair_colleges(repo, demand_id: str, version_no: int) -> set[str]:
    """在当前需求版本上已拒绝过的院校；需求改版后封锁解除，可重新评估。"""
    return {c.college_org_id for c in repo.candidates.values()
            if c.demand_id == demand_id and c.status == "rejected"
            and c.demand_version_no == version_no}


def build_candidate(repo, *, run_id: str, demand, version, college_org_id: str,
                    result: dict[str, Any], at: str) -> Candidate:
    cand = Candidate(
        id=new_id("cand"), run_id=run_id, demand_id=demand.id,
        enterprise_org_id=demand.org_id, college_org_id=college_org_id,
        demand_version_no=version.version_no, required_qty=version.required_qty,
        matched_capability_ids=result["capability_ids"],
        matched_resource_ids=[rid for rid, _ in result["plan"]],
        reasons=result["reasons"], score=result["score"],
        status=CAND_PENDING, created_at=at, updated_at=at,
    )
    hold_resources(repo, cand.id, result["plan"], at)
    repo.candidates[cand.id] = cand
    return cand
