"""最早可交付分批排期。

约束：
- 每晚只有一个入库时段；同晚入库的条目，下一晚才能被其他条目引用。
- 先决条目全部必须可用；每组可选替代条目里任意一个可用即可。
- 替代组形成循环时，只要另一路已经可用，循环不应堵死排期。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import EntryCatalog


@dataclass(frozen=True)
class BatchPlan:
    """一晚一个批次。"""

    night: int
    entries: tuple[str, ...]
    total_hours: float


@dataclass(frozen=True)
class Blocker:
    """不可发布时的根因：某项必须依赖经所有替代路径都无法获得。"""

    missing_entry_id: str
    required_by: str
    detail: str


@dataclass
class PlanResult:
    target: str
    feasible: bool
    batches: tuple[BatchPlan, ...] = ()
    total_hours: float = 0.0
    blockers: tuple[Blocker, ...] = ()
    chosen_alternatives: dict[str, str] = field(default_factory=dict)

    def earliest_delivery_night(self) -> int | None:
        return self.batches[-1].night if self.batches else None


def build_plan(
    catalog: EntryCatalog,
    target: str,
    shift_capacity_hours: float | None = None,
) -> PlanResult:
    if target not in catalog.entries:
        return PlanResult(
            target=target,
            feasible=False,
            blockers=(Blocker(target, target, "专题条目本身不在条目目录中"),),
        )

    feasible_ids = _feasibility_fixpoint(catalog)
    if target not in feasible_ids:
        return PlanResult(
            target=target,
            feasible=False,
            blockers=tuple(_root_blockers(catalog, feasible_ids, target)),
        )

    earliest = _earliest_nights(catalog, feasible_ids)
    chosen, edges, chosen_alternatives = _choose_closure(
        catalog, feasible_ids, target, earliest
    )
    nights = _apply_capacity(catalog, chosen, edges, earliest, shift_capacity_hours)

    by_night: dict[int, list[str]] = {}
    for entry_id in chosen:
        by_night.setdefault(nights[entry_id], []).append(entry_id)

    batches: list[BatchPlan] = []
    total_hours = 0.0
    for night in sorted(by_night):
        ids = tuple(sorted(by_night[night]))
        hours = sum(catalog.entries[entry_id].estimate_hours for entry_id in ids)
        total_hours += hours
        batches.append(BatchPlan(night=night, entries=ids, total_hours=hours))

    return PlanResult(
        target=target,
        feasible=True,
        batches=tuple(batches),
        total_hours=total_hours,
        chosen_alternatives=dict(sorted(chosen_alternatives.items())),
    )


def _feasibility_fixpoint(catalog: EntryCatalog) -> set[str]:
    """求"可发布条目"不动点。

    条目可发布当且仅当它的每个需求组至少有一个成员可发布。
    组内 OR 语义保证替代环上的条目能借另一路进入不动点；
    只剩纯先决环或全部成员缺失的条目永远进不来。
    """
    feasible: set[str] = set()
    changed = True
    while changed:
        changed = False
        for entry_id, entry in catalog.entries.items():
            if entry_id in feasible:
                continue
            if all(
                any(member in feasible for member in group)
                for group in entry.requirement_groups()
            ):
                feasible.add(entry_id)
                changed = True
    return feasible


def _earliest_nights(catalog: EntryCatalog, feasible_ids: set[str]) -> dict[str, int]:
    """每个可发布条目的最早夜班：所有需求组都有成员已排，且隔夜才能引用。"""
    nights: dict[str, int] = {}
    remaining = set(feasible_ids)
    while remaining:
        progressed: list[tuple[str, int]] = []
        for entry_id in sorted(remaining):
            entry = catalog.entries[entry_id]
            lower_bound = 1
            ready = True
            for group in entry.requirement_groups():
                assigned = [member for member in group if member in nights]
                if not assigned:
                    ready = False
                    break
                lower_bound = max(
                    lower_bound, 1 + min(nights[member] for member in assigned)
                )
            if ready:
                progressed.append((entry_id, lower_bound))
        if not progressed:
            raise RuntimeError("可发布集合内出现无法排期的循环，这不应发生")
        for entry_id, night in progressed:
            nights[entry_id] = night
            remaining.discard(entry_id)
    return nights


def _choose_closure(
    catalog: EntryCatalog,
    feasible_ids: set[str],
    target: str,
    earliest: dict[str, int],
) -> tuple[set[str], dict[str, set[str]], dict[str, str]]:
    """每个需求组选最早夜班最小的成员（id 兜底排序），收敛出必要闭包。

    被选成员的夜班严格小于当前条目，因此依赖边天然无环。
    """
    chosen: set[str] = set()
    edges: dict[str, set[str]] = {}
    alternatives: dict[str, str] = {}
    pending = [target]
    while pending:
        entry_id = pending.pop()
        if entry_id in chosen:
            continue
        chosen.add(entry_id)
        entry = catalog.entries[entry_id]
        picked_deps: set[str] = set()
        for index, group in enumerate(entry.requirement_groups()):
            usable = [member for member in group if member in feasible_ids]
            picked = min(usable, key=lambda member: (earliest[member], member))
            picked_deps.add(picked)
            if len(group) > 1:
                alternatives[f"{entry_id}#group{index}"] = picked
            if picked not in chosen:
                pending.append(picked)
        edges[entry_id] = picked_deps
    return chosen, edges, alternatives


def _apply_capacity(
    catalog: EntryCatalog,
    chosen: set[str],
    edges: dict[str, set[str]],
    earliest: dict[str, int],
    capacity: float | None,
) -> dict[str, int]:
    """无容量限制时直接用最早夜班；有时段容量时按拓扑下界顺延超载条目。"""
    if capacity is None:
        return {entry_id: earliest[entry_id] for entry_id in chosen}

    indegree = {entry_id: len(edges[entry_id]) for entry_id in chosen}
    dependents: dict[str, list[str]] = {entry_id: [] for entry_id in chosen}
    for entry_id, deps in edges.items():
        for dep in deps:
            dependents[dep].append(entry_id)

    nights: dict[str, int] = {}
    loads: dict[int, float] = {}
    ready = sorted(entry_id for entry_id, degree in indegree.items() if degree == 0)
    while ready:
        entry_id = ready.pop(0)
        lower_bound = max(
            (nights[dep] + 1 for dep in edges[entry_id]),
            default=earliest[entry_id],
        )
        night = lower_bound
        hours = catalog.entries[entry_id].estimate_hours
        while loads.get(night, 0.0) + hours > capacity + 1e-9:
            night += 1
        loads[night] = loads.get(night, 0.0) + hours
        nights[entry_id] = night
        for dependent in dependents[entry_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort()
    return nights


def _root_blockers(
    catalog: EntryCatalog, feasible_ids: set[str], target: str
) -> list[Blocker]:
    """沿不可发布条目向下追，定位所有替代路径都拿不到的具体依赖。"""
    blockers: list[Blocker] = []
    seen: set[tuple[str, str]] = set()
    visiting: set[str] = set()

    def walk(entry_id: str, required_by: str) -> None:
        entry = catalog.entries.get(entry_id)
        if entry is None:
            key = (entry_id, required_by)
            if key not in seen:
                seen.add(key)
                blockers.append(
                    Blocker(
                        missing_entry_id=entry_id,
                        required_by=required_by,
                        detail="目录中不存在该条目，且没有任何可用的替代路径",
                    )
                )
            return
        if entry_id in feasible_ids:
            return
        if entry_id in visiting:
            key = ("cycle", entry_id)
            if key not in seen:
                seen.add(key)
                blockers.append(
                    Blocker(
                        missing_entry_id=entry_id,
                        required_by=required_by,
                        detail="依赖形成闭环，且环上没有任何一条外部可用路径可以打破",
                    )
                )
            return
        visiting.add(entry_id)
        for group in entry.requirement_groups():
            if any(member in feasible_ids for member in group):
                continue
            for member in group:
                walk(member, entry_id)
        visiting.discard(entry_id)

    walk(target, target)
    return blockers
