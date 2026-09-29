"""撮合引擎：纯函数式、确定性地生成候选关系与重评估结论。

引擎不落库、不发通知，只读取仓储快照并给出结论；持久化、谱系与
通知由服务层负责。相同输入永远得到相同的评分与排序，便于重放。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    Candidate,
    Intent,
    OrgKind,
    ResourceBatch,
)
from .repository import Repository


@dataclass
class MatchOutcome:
    """一对供需意向在某个资源类型上的撮合结论。"""

    supply_intent_id: str
    demand_intent_id: str
    resource_type: str
    eligible: bool
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    resource_id: str | None = None
    required_scale: int = 0
    offered_scale: int = 0


# ---------------------------------------------------------------------------
# 单项校验
# ---------------------------------------------------------------------------


def _authorized_contact(repo: Repository, org_id: str) -> bool:
    return any(c.is_authorized() for c in repo.contacts_of(org_id))


def _exclusion_reasons(repo: Repository, a: str, b: str) -> list[str]:
    reasons: list[str] = []
    for ex in repo.exclusions_of(a):
        if ex.blocks(b):
            reasons.append(f"{a} 的排除条件命中 {b}：{ex.reason}")
    for ex in repo.exclusions_of(b):
        if ex.blocks(a):
            reasons.append(f"{b} 的排除条件命中 {a}：{ex.reason}")
    return reasons


def _shared_owner_ids(repo: Repository) -> list[str]:
    """秘书处登记的共享资源批次可被任意供给方在撮合时提供。"""
    return [oid for oid, org in repo.organizations.items() if org.kind is OrgKind.SECRETARIAT]


def _choose_resource(
    repo: Repository, owner_id: str, resource_type: str, need: int
) -> tuple[ResourceBatch | None, list[str]]:
    """挑选剩余容量最大的可用批次；容量相同取 ID 较小者。

    候选池包括供给方自有资源与秘书处的共享实训资源——三所院校
    承诺同一批共享工位时，它们竞争的是同一份容量，不会被重复承诺。
    """
    allowed = {owner_id, *_shared_owner_ids(repo)}
    pool = [
        r
        for r in repo.resources.values()
        if r.owner_id in allowed
        and r.resource_type == resource_type
        and r.status.value == "available"
    ]
    pool.sort(key=lambda r: (-r.available_capacity(), r.resource_id))
    if not pool:
        return None, [f"供给方没有类型为 {resource_type} 的可用资源批次"]
    best = pool[0]
    if best.available_capacity() < need:
        return None, [
            f"资源 {best.name} 剩余容量 {best.available_capacity()} 小于需求规模 {need}"
        ]
    return best, []


# ---------------------------------------------------------------------------
# 配对评估
# ---------------------------------------------------------------------------


def evaluate_pair(repo: Repository, supply: Intent, demand: Intent) -> list[MatchOutcome]:
    """评估一对供需意向，对需求中的每个资源类型给出结论。"""
    outcomes: list[MatchOutcome] = []
    supply_dv = supply.current_demand()
    demand_dv = demand.current_demand()

    base_blocks: list[str] = []
    if not supply.is_open():
        base_blocks.append(f"供给意向 {supply.intent_id} 已{supply.status.label}")
    if not demand.is_open():
        base_blocks.append(f"需求意向 {demand.intent_id} 已{demand.status.label}")
    if not _authorized_contact(repo, supply.org_id):
        base_blocks.append(f"供给方 {supply.org_id} 没有有效授权联系人")
    if not _authorized_contact(repo, demand.org_id):
        base_blocks.append(f"需求方 {demand.org_id} 没有有效授权联系人")
    base_blocks.extend(_exclusion_reasons(repo, supply.org_id, demand.org_id))

    for resource_type in demand_dv.resource_types:
        outcome = MatchOutcome(
            supply_intent_id=supply.intent_id,
            demand_intent_id=demand.intent_id,
            resource_type=resource_type,
            eligible=False,
            required_scale=demand_dv.scale,
        )
        outcome.block_reasons.extend(base_blocks)

        if resource_type not in supply_dv.resource_types:
            outcome.block_reasons.append(
                f"供给意向版本 {supply_dv.version} 不提供 {resource_type}"
            )
            outcomes.append(outcome)
            continue

        if base_blocks:
            outcomes.append(outcome)
            continue

        resource, cap_blocks = _choose_resource(
            repo, supply.org_id, resource_type, demand_dv.scale
        )
        if cap_blocks:
            outcome.block_reasons.extend(cap_blocks)
            outcomes.append(outcome)
            continue

        assert resource is not None
        outcome.eligible = True
        outcome.resource_id = resource.resource_id
        outcome.offered_scale = resource.available_capacity()

        # 确定性评分与人类可读理由
        outcome.score += 40
        outcome.reasons.append(f"资源类型精确匹配：{resource_type}（+40）")
        if supply_dv.region and demand_dv.region and supply_dv.region == demand_dv.region:
            outcome.score += 20
            outcome.reasons.append(f"地域一致：{demand_dv.region}（+20）")
        if resource.available_capacity() >= demand_dv.scale:
            outcome.score += 20
            outcome.reasons.append(
                f"容量充足：剩余 {resource.available_capacity()} ≥ 需求 {demand_dv.scale}（+20）"
            )
        if supply_dv.term and demand_dv.term and supply_dv.term == demand_dv.term:
            outcome.score += 10
            outcome.reasons.append(f"合作档期一致：{demand_dv.term}（+10）")
        caps = [
            c
            for c in repo.capabilities_of(supply.org_id)
            if c.resource_type == resource_type and c.scale >= demand_dv.scale
        ]
        if caps:
            outcome.score += 10
            outcome.reasons.append(
                f"供给方能力声明覆盖该类型与规模：{caps[0].capability_id}（+10）"
            )
        outcomes.append(outcome)

    return outcomes


def enumerate_matches(repo: Repository) -> list[MatchOutcome]:
    """枚举全部供给×需求配对，按分数降序返回确定排序的结论列表。"""
    supplies = [i for i in repo.intents.values() if i.direction == "supply"]
    demands = [i for i in repo.intents.values() if i.direction == "demand"]
    outcomes: list[MatchOutcome] = []
    for supply in sorted(supplies, key=lambda i: i.intent_id):
        for demand in sorted(demands, key=lambda i: i.intent_id):
            outcomes.extend(evaluate_pair(repo, supply, demand))
    outcomes.sort(
        key=lambda o: (
            -o.score if o.eligible else 1,
            not o.eligible,
            o.supply_intent_id,
            o.demand_intent_id,
            o.resource_type,
        )
    )
    return outcomes


# ---------------------------------------------------------------------------
# 候选重评估
# ---------------------------------------------------------------------------


def reevaluate_candidate(repo: Repository, candidate: Candidate) -> list[str]:
    """对已存在的候选按当前事实重新评估，返回使其失效的原因列表。

    空列表表示候选仍然成立。需求版本是否滞后由服务层单独判断
    （滞后不是“失效”而是“被新版本接替 SUPERSEDED”）。
    """
    reasons: list[str] = []
    supply = repo.intents.get(candidate.supply_intent_id)
    demand = repo.intents.get(candidate.demand_intent_id)

    if supply is None or demand is None:
        return ["候选引用的意向已不存在"]
    if not supply.is_open():
        reasons.append(f"供给意向 {supply.intent_id} 已{supply.status.label}")
    if not demand.is_open():
        reasons.append(f"需求意向 {demand.intent_id} 已{demand.status.label}")

    if not any(c.is_authorized() for c in repo.contacts_of(candidate.school_id)):
        reasons.append(f"院校 {candidate.school_id} 已无有效授权联系人")
    if not any(c.is_authorized() for c in repo.contacts_of(candidate.enterprise_id)):
        reasons.append(f"企业 {candidate.enterprise_id} 已无有效授权联系人")

    reasons.extend(_exclusion_reasons(repo, candidate.school_id, candidate.enterprise_id))

    resource = repo.resources.get(candidate.resource_id)
    if resource is None:
        reasons.append(f"资源 {candidate.resource_id} 已不存在")
    else:
        from .models import ResourceStatus

        if resource.status is ResourceStatus.OCCUPIED:
            reasons.append(
                f"资源 {resource.name} 已被正式项目 {resource.occupied_by_project_id} 占用"
            )
        elif resource.available_capacity() < candidate.required_scale:
            reasons.append(
                f"资源 {resource.name} 剩余容量 {resource.available_capacity()} "
                f"不足以覆盖需求规模 {candidate.required_scale}"
            )

    dv = demand.current_demand()
    if candidate.resource_type not in dv.resource_types:
        reasons.append(f"最新需求版本 {dv.version} 已移除资源类型 {candidate.resource_type}")

    return reasons
