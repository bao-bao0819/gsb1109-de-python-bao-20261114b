"""环形序号采集文件的缺件归类。

站点只保留最近 RING_SIZE(256) 个序号的采集文件，文件名是两位十六进制
序号(00..ff)，随设备产出循环覆盖。本模块依据：

- 两次目录快照(快照 A、快照 B，均含文件名与采样时间)
- 上次确认的轮次锚点(某个名字在某时刻对应的绝对序号)
- 收件回执(已经确认登记在案的绝对序号)
- 设备声明的最大产出速率(单位时间最多产出多少个文件)

把观察到的缺件分成三类：

- ``overwritten``  已被覆盖的缺件：旧代次的缺号已经滚过保留窗口，
  同名位置被更新代次占据，无法再请求；
- ``requestable``  当前还可请求的缺件：缺号仍在保留窗口内且无回执；
- ``uncertain``    代次不确定的名字：现有证据相容多种代次组合，
  不能唯一归类。

两次快照之间设备可能绕多圈，因此不能只按两位名字的短路径补号：
速率上限给出快照间产出数量的上界，本模块枚举所有与证据相容的
(快照 B 头序号, 快照间产出数)组合，再对每个缺件取这些组合下的
代次交集/分歧。请求计划落实前，必须用当前目录再核对一次代次，
避免把新产出的同序号文件误登记成旧缺件(见 :func:`reconcile`)。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence

RING_SIZE = 256
HEX_WIDTH = 2

OVERWRITTEN = "overwritten"
REQUESTABLE = "requestable"
UNCERTAIN = "uncertain"
FULFILLED = "fulfilled"
CONFIRMED = "confirmed"


def name_of(slot: int) -> str:
    """把槽位号(0..255)格式化为两位十六进制文件名。"""
    if not 0 <= slot < RING_SIZE:
        raise ValueError(f"slot out of range: {slot}")
    return f"{slot:0{HEX_WIDTH}x}"


def slot_of(name: str) -> int:
    """把两位十六进制文件名解析为槽位号。"""
    if not isinstance(name, str) or len(name) != HEX_WIDTH:
        raise ValueError(f"expected a {HEX_WIDTH}-char hex name, got {name!r}")
    try:
        value = int(name, 16)
    except ValueError as exc:
        raise ValueError(f"name is not hexadecimal: {name!r}") from exc
    if not 0 <= value < RING_SIZE:
        raise ValueError(f"slot out of range: {name!r}")
    return value


def generation_of(sequence: int) -> int:
    """绝对序号所在的代次(圈号)，第 0..255 号为第 0 代。"""
    if sequence < 0:
        raise ValueError(f"sequence must be non-negative: {sequence}")
    return sequence // RING_SIZE


def representative(slot: int, head: int) -> int:
    """在头序号为 ``head`` 的保留窗口内，``slot`` 对应的绝对序号。

    保留窗口永远是 ``[head-255, head]`` 这 256 个连续序号，每个槽位
    恰有一个代表序号落在窗口内。若该槽位缺件，缺的就是这个序号。
    """
    if head < 0:
        raise ValueError(f"head must be non-negative: {head}")
    turns = (head - slot) // RING_SIZE
    return slot + RING_SIZE * turns


def _to_epoch(value) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


@dataclass(frozen=True)
class Snapshot:
    """一次目录快照。

    ``files`` 为槽位号到该文件时间戳的映射；时间戳当前仅做留存，
    归类以快照采样时间 ``time`` 为准。
    """

    time: float
    files: Mapping[int, float]

    def __post_init__(self) -> None:
        normalized = {
            slot_of(key) if isinstance(key, str) else int(key): _to_epoch(value)
            for key, value in self.files.items()
        }
        object.__setattr__(self, "files", normalized)
        object.__setattr__(self, "time", _to_epoch(self.time))

    @classmethod
    def from_mapping(cls, time, files: Mapping[str, object]) -> "Snapshot":
        return cls(time=_to_epoch(time), files=dict(files))

    @property
    def present_slots(self) -> frozenset[int]:
        return frozenset(self.files)

    @property
    def missing_slots(self) -> frozenset[int]:
        return frozenset(set(range(RING_SIZE)) - set(self.files))


@dataclass(frozen=True)
class Anchor:
    """轮次锚点：``time`` 时刻槽位 ``slot`` 的绝对序号为 ``sequence``。"""

    slot: int
    sequence: int
    time: float

    @classmethod
    def build(cls, name: str, sequence: int, time) -> "Anchor":
        slot = slot_of(name)
        if sequence < 0 or sequence % RING_SIZE != slot:
            raise ValueError(
                f"anchor {name!r} cannot carry sequence {sequence}: "
                f"sequence mod {RING_SIZE} must equal its slot"
            )
        return cls(slot, int(sequence), _to_epoch(time))


@dataclass(frozen=True)
class GapEntry:
    """单个缺件名字的归类结论。"""

    name: str
    slot: int
    generation: Optional[int]
    absolute_sequence: Optional[int]
    possible_sequences: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slot": self.slot,
            "generation": self.generation,
            "absolute_sequence": self.absolute_sequence,
            "possible_sequences": list(self.possible_sequences),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Classification:
    """两次快照对比后的缺件分桶结果。"""

    overwritten: tuple[GapEntry, ...]
    requestable: tuple[GapEntry, ...]
    uncertain: tuple[GapEntry, ...]
    confirmed: tuple[GapEntry, ...] = ()
    fulfilled: tuple[GapEntry, ...] = ()
    feasible_heads_b: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "overwritten": [entry.to_dict() for entry in self.overwritten],
            "requestable": [entry.to_dict() for entry in self.requestable],
            "uncertain": [entry.to_dict() for entry in self.uncertain],
            "confirmed": [entry.to_dict() for entry in self.confirmed],
            "fulfilled": [entry.to_dict() for entry in self.fulfilled],
            "feasible_heads_b": list(self.feasible_heads_b),
        }


@dataclass(frozen=True)
class RequestItem:
    """一条可落实的补件请求。"""

    name: str
    slot: int
    generation: int
    absolute_sequence: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slot": self.slot,
            "generation": self.generation,
            "absolute_sequence": self.absolute_sequence,
        }


@dataclass(frozen=True)
class RequestPlan:
    """依据归类结果生成的请求计划，落实前必须经 :func:`reconcile` 复核。"""

    created_at: float
    items: tuple[RequestItem, ...]

    def to_dict(self) -> dict:
        return {
            "created_at": self.created_at,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ReconcileResult:
    """请求计划与当前目录复核后的结果。"""

    keep: tuple[RequestItem, ...]
    fulfilled: tuple[RequestItem, ...]
    overwritten: tuple[RequestItem, ...]
    uncertain: tuple[RequestItem, ...]

    def to_dict(self) -> dict:
        return {
            "keep": [item.to_dict() for item in self.keep],
            "fulfilled": [item.to_dict() for item in self.fulfilled],
            "overwritten": [item.to_dict() for item in self.overwritten],
            "uncertain": [item.to_dict() for item in self.uncertain],
        }


def window_floor(head: int) -> int:
    """头序号为 ``head`` 时保留窗口的最小序号；未满环前为 0。"""
    if head < 0:
        raise ValueError(f"head must be non-negative: {head}")
    return max(0, head - RING_SIZE + 1)


def in_window(slot: int, head: int) -> bool:
    """槽位在头序号 ``head`` 下是否已进入保留窗口(未满环时可能尚未产出)。"""
    sequence = representative(slot, head)
    return window_floor(head) <= sequence <= head


def _min_head_for_presence(snapshot: Snapshot) -> int:
    """满足保留窗口约束的最小头序号。

    头序号 >= 255 后窗口覆盖全部槽位，任何现存文件都合法；首个满环
    之前，头序号至少要等于现存文件中的最大槽位号。
    """
    if not snapshot.present_slots:
        return 0
    return min(RING_SIZE - 1, max(snapshot.present_slots))


def _vanish_distance(head_a: int, vanished: Sequence[int]) -> int:
    """让所有 ``vanished`` 槽位的代表序号换代，头序号至少要推进多少。"""
    if not vanished:
        return 0
    return max(RING_SIZE - ((head_a - slot) % RING_SIZE) for slot in vanished)


def _anchor_head_bounds(
    anchors: Sequence[Anchor], receipts: Sequence[int], at_time: float, rate: float
) -> tuple[int, Optional[int]]:
    """返回 ``at_time`` 时刻头序号的 ``(下界, 上界)``。

    过去的锚点给出上界(锚点之后至多产出 rate*Δt 个)；未来的锚点
    反过来给出下界(从该时刻往回最多产出 rate*Δt 个)。
    """
    lower = max([0] + [int(seq) for seq in receipts])
    upper: Optional[int] = None
    for anchor in anchors:
        delta = at_time - anchor.time
        allowance = math.floor(rate * abs(delta) + 1e-9)
        if delta >= -1e-9:
            # 过去(或当前)的锚点：头序号不小于锚点序号，且至多再涨 allowance
            lower = max(lower, anchor.sequence)
            bound = anchor.sequence + allowance
            upper = bound if upper is None else min(upper, bound)
        else:
            # 未来的锚点：从该时刻往回最多 allowance 个序号
            lower = max(lower, anchor.sequence - allowance)
    return lower, upper


def enumerate_states(
    snapshot_a: Snapshot,
    snapshot_b: Snapshot,
    rate: float,
    anchors: Sequence[Anchor] = (),
    receipts: Sequence[int] = (),
) -> tuple[tuple[tuple[int, int], ...], tuple[int, ...]]:
    """枚举所有与证据相容的 ``(头序号 B, 头序号 A)`` 组合。

    速率是声明的上限，因此快照间产出数 ``d`` 取在
    ``[0, floor(rate * 间隔)]`` 闭区间(下界为 0 是因为新出现的名字
    可能只是旧缺口补传)，允许 ``d >= 256`` 的多圈情形。
    返回 ``(状态集合, 去重排序后的头序号 B)``。
    """
    if rate < 0:
        raise ValueError("rate must be non-negative")
    if snapshot_b.time < snapshot_a.time:
        raise ValueError("snapshot_b is older than snapshot_a")

    elapsed = snapshot_b.time - snapshot_a.time
    max_produced = math.floor(rate * elapsed + 1e-9)

    lower_b, upper_b = _anchor_head_bounds(
        anchors, receipts, snapshot_b.time, rate
    )
    lower_a, upper_a = _anchor_head_bounds(
        anchors, receipts, snapshot_a.time, rate
    )
    if upper_b is None:
        raise ValueError(
            "no anchor available: absolute generation cannot be pinned, "
            "every observed gap must stay uncertain"
        )

    head_a_floor = max(lower_a, _min_head_for_presence(snapshot_a))
    head_b_floor = max(lower_b, _min_head_for_presence(snapshot_b))
    head_a_ceiling = upper_a if upper_a is not None else upper_b
    vanished = tuple(sorted(snapshot_a.present_slots - snapshot_b.present_slots))

    states: set[tuple[int, int]] = set()
    for head_a in range(head_a_floor, head_a_ceiling + 1):
        distance_min = _vanish_distance(head_a, vanished)
        low_b = max(head_b_floor, head_a + distance_min)
        high_b = min(upper_b, head_a + max_produced)
        for head_b in range(low_b, high_b + 1):
            states.add((head_b, head_a))

    heads_b = tuple(sorted({head_b for head_b, _ in states}))
    return tuple(sorted(states)), heads_b


def _head_ranges(heads: Sequence[int]) -> list[tuple[int, int]]:
    """把有序头序号压缩成连续闭区间列表。"""
    if not heads:
        return []
    ranges = []
    start = previous = heads[0]
    for head in heads[1:]:
        if head == previous + 1:
            previous = head
            continue
        ranges.append((start, previous))
        start = previous = head
    ranges.append((start, previous))
    return ranges


def _representatives_in_ranges(slot: int, ranges: Sequence[tuple[int, int]]) -> set[int]:
    """槽位在一组头序号区间内能取到的全部代表绝对序号。"""
    sequences: set[int] = set()
    for low, high in ranges:
        low_value = representative(slot, low)
        high_value = representative(slot, high)
        # 代表序号随头序号单调不减，每跨过一个整环增加 256
        sequences.update(range(low_value, high_value + 1, RING_SIZE))
    return sequences


def _aggregate_verdicts(
    snapshot_a: Snapshot,
    snapshot_b: Snapshot,
    states: Sequence[tuple[int, int]],
) -> dict[int, dict]:
    """汇总每个观察槽位在所有相容状态下的结论与稳定性。

    稳定性通过单趟扫描(对已分歧槽位短路)判定；候选绝对序号则从可行
    头序号的连续区间直接枚举，避免短路丢失后续代次的候选。
    """
    missing_a = snapshot_a.missing_slots
    missing_b = snapshot_b.missing_slots
    observed = sorted(missing_a | missing_b)
    verdict_sets: dict[int, frozenset[tuple[str, int]]] = {}
    unstable: set[int] = set()
    produced_any: set[int] = set()
    heads_a = sorted({head_a for _, head_a in states})
    heads_b = sorted({head_b for head_b, _ in states})
    ranges_a = _head_ranges(heads_a)
    ranges_b = _head_ranges(heads_b)

    for head_b, head_a in states:
        for slot in observed:
            if slot in unstable:
                continue
            rep_b = representative(slot, head_b)
            rep_a = representative(slot, head_a)
            in_a = in_window(slot, head_a)
            in_b = in_window(slot, head_b)
            verdicts: set[tuple[str, int]] = set()
            if slot in missing_a and slot in missing_b:
                if in_b:
                    produced_any.add(slot)
                    if rep_b == rep_a:
                        verdicts.add((REQUESTABLE, rep_b))
                    else:
                        if rep_a >= 0:
                            verdicts.add((OVERWRITTEN, rep_a))
                        verdicts.add((REQUESTABLE, rep_b))
            elif slot in missing_a and in_a:
                produced_any.add(slot)
                if rep_b == rep_a:
                    verdicts.add((FULFILLED, rep_a))
                else:
                    verdicts.add((OVERWRITTEN, rep_a))
            elif slot in missing_b and in_b:
                produced_any.add(slot)
                verdicts.add((REQUESTABLE, rep_b))

            frozen = frozenset(verdicts)
            if slot not in verdict_sets:
                verdict_sets[slot] = frozen
            elif verdict_sets[slot] != frozen:
                unstable.add(slot)

    result = {}
    for slot in observed:
        rep_b_values = _representatives_in_ranges(slot, ranges_b)
        rep_a_values = _representatives_in_ranges(slot, ranges_a)
        floor_b = window_floor(heads_b[0]) if heads_b else 0
        produced_b = {seq for seq in rep_b_values if seq >= floor_b}
        overwritten: set[int] = set()
        requestable: set[int] = set()
        fulfilled: set[int] = set()

        if slot in missing_a and slot in missing_b:
            requestable = set(produced_b)
            overwritten = {
                seq for seq in rep_a_values if 0 <= seq < floor_b
            }
        elif slot in missing_a:
            overlap = rep_a_values & rep_b_values
            fulfilled = {seq for seq in overlap if seq >= 0}
            overwritten = {
                seq for seq in rep_a_values
                if seq >= 0 and seq not in rep_b_values
            }
        elif slot in missing_b:
            requestable = set(produced_b)

        result[slot] = {
            "produced_any": slot in produced_any,
            "stable": slot in produced_any and slot not in unstable,
            OVERWRITTEN: overwritten,
            REQUESTABLE: requestable,
            FULFILLED: fulfilled,
        }
    return result


def _entry(slot: int, kind: str, sequences: set[int], reason: str) -> GapEntry:
    ordered = tuple(sorted(sequences))
    if len(ordered) == 1:
        sequence = ordered[0]
        return GapEntry(
            name_of(slot), slot, generation_of(sequence), sequence, ordered, (reason,)
        )
    return GapEntry(name_of(slot), slot, None, None, ordered, (reason,))


def classify(
    snapshot_a: Snapshot,
    snapshot_b: Snapshot,
    rate: float,
    anchors: Sequence[Anchor] = (),
    receipts: Iterable[int] = (),
) -> Classification:
    """对比两次快照，返回缺件的三分类(外加已确认/已补齐)结果。"""
    receipt_set = frozenset(int(seq) for seq in receipts)
    anchors = tuple(anchors)

    if not anchors:
        # 没有轮次锚点就无法钉住绝对代次，回执只能确认历史序号，
        # 不能推断当前名字属于哪一代，一律标记为代次不确定。
        return _unpinned_classification(snapshot_a, snapshot_b)

    states, heads_b = enumerate_states(snapshot_a, snapshot_b, rate, anchors, receipt_set)
    if not states:
        raise ValueError("no feasible head sequence: snapshots contradict rate/anchors")

    overwritten: list[GapEntry] = []
    requestable: list[GapEntry] = []
    uncertain: list[GapEntry] = []
    confirmed: list[GapEntry] = []
    fulfilled: list[GapEntry] = []

    aggregated = _aggregate_verdicts(snapshot_a, snapshot_b, states)
    for slot in sorted(aggregated):
        candidates = aggregated[slot]
        if not candidates["produced_any"]:
            continue
        requestable_seqs = candidates[REQUESTABLE]
        overwritten_seqs = candidates[OVERWRITTEN]
        fulfilled_seqs = candidates[FULFILLED]

        if not candidates["stable"]:
            possible = (
                requestable_seqs | overwritten_seqs | fulfilled_seqs
            )
            uncertain.append(
                _entry(
                    slot,
                    UNCERTAIN,
                    possible,
                    "evidence is consistent with multiple generations or head counts",
                )
            )
            continue

        if requestable_seqs:
            if requestable_seqs <= receipt_set:
                confirmed.append(
                    _entry(
                        slot,
                        CONFIRMED,
                        requestable_seqs,
                        "missing sequence already covered by a receipt",
                    )
                )
            else:
                requestable.append(
                    _entry(
                        slot,
                        REQUESTABLE,
                        requestable_seqs,
                        _reason_for(REQUESTABLE, snapshot_a, snapshot_b, slot),
                    )
                )

        if overwritten_seqs:
            if overwritten_seqs <= receipt_set:
                confirmed.append(
                    _entry(
                        slot,
                        CONFIRMED,
                        overwritten_seqs,
                        "overwritten gap is already receipt-confirmed",
                    )
                )
            else:
                overwritten.append(
                    _entry(
                        slot,
                        OVERWRITTEN,
                        overwritten_seqs,
                        _reason_for(OVERWRITTEN, snapshot_a, snapshot_b, slot),
                    )
                )

        if fulfilled_seqs:
            if fulfilled_seqs <= receipt_set:
                confirmed.append(
                    _entry(
                        slot,
                        CONFIRMED,
                        fulfilled_seqs,
                        "backfilled sequence is receipt-confirmed",
                    )
                )
            else:
                fulfilled.append(
                    _entry(
                        slot,
                        FULFILLED,
                        fulfilled_seqs,
                        _reason_for(FULFILLED, snapshot_a, snapshot_b, slot),
                    )
                )

    return Classification(
        overwritten=tuple(overwritten),
        requestable=tuple(requestable),
        uncertain=tuple(uncertain),
        confirmed=tuple(confirmed),
        fulfilled=tuple(fulfilled),
        feasible_heads_b=heads_b,
    )


def _reason_for(
    kind: str, snapshot_a: Snapshot, snapshot_b: Snapshot, slot: int
) -> str:
    if kind == OVERWRITTEN:
        if slot in snapshot_a.missing_slots and slot not in snapshot_b.missing_slots:
            return "missing generation aged out; name now held by a newer generation"
        return "older missing generation rotated out of the retention window"
    if kind == REQUESTABLE:
        return "missing sequence still inside the 256-file retention window"
    if kind == FULFILLED:
        return "previously missing sequence has since been produced"
    return kind


def _unpinned_classification(
    snapshot_a: Snapshot, snapshot_b: Snapshot
) -> Classification:
    entries = [
        GapEntry(
            name_of(slot),
            slot,
            None,
            None,
            possible_sequences=(),
            reasons=("no anchor to pin the absolute generation",),
        )
        for slot in sorted(snapshot_a.missing_slots | snapshot_b.missing_slots)
    ]
    return Classification(
        overwritten=(),
        requestable=(),
        uncertain=tuple(entries),
    )


def build_request_plan(classification: Classification, created_at: float) -> RequestPlan:
    """把 ``requestable`` 桶落成请求计划；代次不确定的名字不进入计划。"""
    items = tuple(
        RequestItem(
            name=entry.name,
            slot=entry.slot,
            generation=entry.generation,
            absolute_sequence=entry.absolute_sequence,
        )
        for entry in classification.requestable
    )
    return RequestPlan(created_at=_to_epoch(created_at), items=items)


def reconcile(
    plan: RequestPlan,
    snapshot_b: Snapshot,
    snapshot_current: Snapshot,
    rate: float,
    anchors: Sequence[Anchor] = (),
    receipts: Iterable[int] = (),
) -> ReconcileResult:
    """请求落实前用当前目录复核代次。

    对每条计划项，在快照 B 与当前快照之间重新枚举相容状态：

    - 同名文件已出现且代次与计划一致 -> 已补齐；
    - 同名文件已出现但代次更新 -> 旧缺件已被覆盖，绝不登记成旧件；
    - 名字仍缺且旧序号仍在窗口内 -> 保留请求；
    - 各种相容状态结论不一致 -> 代次不确定，转人工核对。
    """
    receipt_set = frozenset(int(seq) for seq in receipts)
    states, _ = enumerate_states(snapshot_b, snapshot_current, rate, anchors, receipt_set)
    if not states:
        raise ValueError("no feasible head sequence for the current snapshot")

    keep: list[RequestItem] = []
    fulfilled: list[RequestItem] = []
    overwritten: list[RequestItem] = []
    uncertain: list[RequestItem] = []

    present_now = snapshot_current.present_slots
    head_now_values = sorted({head_now for head_now, _ in states})
    floor_min = window_floor(head_now_values[0])
    floor_max = window_floor(head_now_values[-1])
    for item in plan.items:
        if item.absolute_sequence in receipt_set:
            fulfilled.append(item)
        elif item.absolute_sequence < floor_min:
            # 目标序号在所有相容状态下都已滚出保留窗口，同名文件即便
            # 存在也是新代次，绝不能把新件登记成旧缺件。
            overwritten.append(item)
        elif item.absolute_sequence < floor_max:
            # 一部分相容状态下还在窗口内，另一部分已经滚出，代次存疑
            uncertain.append(item)
        elif item.slot in present_now:
            fulfilled.append(item)
        else:
            keep.append(item)

    return ReconcileResult(
        keep=tuple(keep),
        fulfilled=tuple(fulfilled),
        overwritten=tuple(overwritten),
        uncertain=tuple(uncertain),
    )


def _load_snapshot(payload: Optional[Mapping]) -> Optional[Snapshot]:
    if payload is None:
        return None
    return Snapshot.from_mapping(payload["time"], payload["files"])


def report_from_dict(payload: Mapping) -> dict:
    """供命令行使用的 JSON 入口，字段格式见模块 README。"""
    rate = float(payload["rate_per_sec"])
    snapshot_a = _load_snapshot(payload["snapshot_a"])
    snapshot_b = _load_snapshot(payload["snapshot_b"])
    anchors = [
        Anchor.build(item["name"], item["sequence"], item["time"])
        for item in payload.get("anchors", ())
    ]
    receipts = [int(seq) for seq in payload.get("receipts", ())]
    classification = classify(snapshot_a, snapshot_b, rate, anchors, receipts)
    result: dict = classification.to_dict()

    current = _load_snapshot(payload.get("current"))
    if current is not None:
        plan = build_request_plan(classification, current.time)
        result["request_plan"] = plan.to_dict()
        result["reconcile"] = reconcile(
            plan, snapshot_b, current, rate, anchors, receipts
        ).to_dict()
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="JSON 输入文件，缺省读标准输入")
    args = parser.parse_args(argv)
    if args.input:
        with open(args.input, encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = json.load(__import__("sys").stdin)
    print(json.dumps(report_from_dict(payload), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
